from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path

from .cache import ArtifactCache
from .project_repository import ProjectRepository
from .run_repository import FlowRunRepository
from .runs import FlowRunStatus
from .schema import OutputPolicy, RetentionPolicy


_TERMINAL_RUN_STATUSES = {
    FlowRunStatus.SUCCEEDED,
    FlowRunStatus.PARTIALLY_FAILED,
    FlowRunStatus.FAILED,
    FlowRunStatus.CANCELLED,
}


@dataclass(frozen=True)
class ArtifactGcItem:
    run_id: str
    scope: str
    artifact_id: str
    path: str
    manifest_path: str
    retention: str
    action: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ArtifactGcReport:
    dry_run: bool
    min_age_seconds: float
    scanned_runs: int
    deleted_files: int
    reclaimed_bytes: int
    items: tuple[ArtifactGcItem, ...]
    cache: dict

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "min_age_seconds": self.min_age_seconds,
            "scanned_runs": self.scanned_runs,
            "deleted_files": self.deleted_files,
            "reclaimed_bytes": self.reclaimed_bytes,
            "items": [item.to_dict() for item in self.items],
            "cache": self.cache,
        }


class ArtifactLifecycleService:
    """Reference-aware lifecycle/garbage collection for Flow artifacts.

    Run metadata and input snapshots are deliberately not garbage-collected here.
    Input snapshots are part of retry reproducibility (M5), so M9 only collects
    node-output materializations and final Flow Output media/manifests.

    A Studio binding is a live reference regardless of managed/detached status.
    `retain=project` additionally protects a materialized artifact while the
    producing Flow is owned by a Project.
    """

    def __init__(
        self,
        *,
        run_repository: FlowRunRepository,
        project_repository: ProjectRepository,
        runs_root: Path,
        cache: ArtifactCache,
    ) -> None:
        self.run_repository = run_repository
        self.project_repository = project_repository
        self.runs_root = Path(runs_root)
        self.cache = cache

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _age_seconds(created_at: str, now: datetime) -> float:
        try:
            value = datetime.fromisoformat(created_at)
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return max(0.0, (now - value.astimezone(UTC)).total_seconds())
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _policy_for_node_output(run, node_id: str, port_id: str) -> RetentionPolicy:
        node = run.definition.nodes[node_id]
        policy = node.outputs.get(port_id, OutputPolicy())
        return policy.retain

    @classmethod
    def _policy_for_flow_output(cls, run, output_id: str) -> RetentionPolicy:
        output = run.definition.outputs[output_id]
        source = output.source
        if source.startswith("inputs."):
            return RetentionPolicy.CACHE
        if "." not in source:
            return RetentionPolicy.CACHE
        node_id, port_id = source.split(".", 1)
        if node_id not in run.definition.nodes:
            return RetentionPolicy.CACHE
        return cls._policy_for_node_output(run, node_id, port_id)

    @staticmethod
    def _read_manifest(manifest: Path, allowed_root: Path) -> tuple[dict, Path] | None:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            path = Path(data["path"]).resolve()
        except (OSError, ValueError, KeyError, TypeError):
            return None
        root = allowed_root.resolve()
        if not path.is_relative_to(root):
            return None
        return data, path

    @staticmethod
    def _delete_pair(path: Path, manifest: Path) -> tuple[int, int]:
        deleted = 0
        reclaimed = 0
        # The byte count is logical reclaimed file size. Hard-linked cache/output
        # copies may keep the same inode alive, which is intentionally safe.
        for candidate in (path, manifest):
            try:
                if candidate.is_file():
                    reclaimed += candidate.stat().st_size
                    candidate.unlink()
                    deleted += 1
            except OSError:
                # GC is best-effort. A later pass may retry a file that is busy or
                # has been concurrently removed.
                continue
        return deleted, reclaimed

    def _binding_refs(self) -> tuple[set[tuple[str, str]], set[Path]]:
        identities: set[tuple[str, str]] = set()
        paths: set[Path] = set()
        for binding in self.project_repository.list_all_bindings():
            identities.add((binding.current_run_id, binding.flow_output_id))
            value = binding.artifact.get("path")
            if isinstance(value, str):
                try:
                    paths.add(Path(value).resolve())
                except OSError:
                    pass
        return identities, paths

    def collect(
        self,
        *,
        dry_run: bool = True,
        min_age_seconds: float = 0.0,
        cache_max_idle_seconds: float | None = None,
    ) -> ArtifactGcReport:
        if min_age_seconds < 0:
            raise ValueError("min_age_seconds must be >= 0")
        if cache_max_idle_seconds is not None and cache_max_idle_seconds < 0:
            raise ValueError("cache_max_idle_seconds must be >= 0")

        now = self._now()
        binding_refs, binding_paths = self._binding_refs()
        items: list[ArtifactGcItem] = []
        deleted_files = 0
        reclaimed_bytes = 0
        scanned_runs = 0

        for run in self.run_repository.list_runs():
            if run.status not in _TERMINAL_RUN_STATUSES:
                continue
            scanned_runs += 1
            run_dir = (self.runs_root / run.run_id).resolve()
            age = self._age_seconds(run.created_at, now)
            old_enough = age >= min_age_seconds
            project_owned = bool(run.flow_id and self.project_repository.has_project_flow(run.flow_id))

            # Node-output materializations. They are never Studio-bindable
            # directly; project retention is therefore the only persistent live
            # reference beyond the Run itself.
            for node_id, node in run.definition.nodes.items():
                node_dir = run_dir / "nodes" / node_id
                manifest_ports = {
                    path.name[: -len(".artifact.json")]
                    for path in node_dir.glob("*.artifact.json")
                    if path.name.endswith(".artifact.json")
                }
                for port_id in sorted(set(node.outputs) | manifest_ports):
                    manifest = node_dir / f"{port_id}.artifact.json"
                    if not manifest.is_file():
                        continue
                    parsed = self._read_manifest(manifest, run_dir)
                    if parsed is None:
                        items.append(
                            ArtifactGcItem(
                                run_id=run.run_id,
                                scope="node_output",
                                artifact_id=f"{node_id}.{port_id}",
                                path="",
                                manifest_path=str(manifest),
                                retention=self._policy_for_node_output(run, node_id, port_id).value,
                                action="protected",
                                reason="invalid_or_unsafe_manifest",
                            )
                        )
                        continue
                    _, path = parsed
                    retention = self._policy_for_node_output(run, node_id, port_id)
                    if retention == RetentionPolicy.PROJECT and project_owned:
                        action, reason = "protected", "project_flow_retention"
                    elif not old_enough:
                        action, reason = "protected", "younger_than_min_age"
                    else:
                        action = "would_delete" if dry_run else "deleted"
                        reason = f"retention_{retention.value}"
                        if not dry_run:
                            count, size = self._delete_pair(path, manifest)
                            deleted_files += count
                            reclaimed_bytes += size
                    items.append(
                        ArtifactGcItem(
                            run_id=run.run_id,
                            scope="node_output",
                            artifact_id=f"{node_id}.{port_id}",
                            path=str(path),
                            manifest_path=str(manifest),
                            retention=retention.value,
                            action=action,
                            reason=reason,
                        )
                    )

            # Final Flow Outputs are protected by persisted Studio references.
            # `retain=project` is inherited from the source node output.
            for output_id in run.definition.outputs:
                manifest = run_dir / "outputs" / f"{output_id}.artifact.json"
                if not manifest.is_file():
                    continue
                parsed = self._read_manifest(manifest, run_dir)
                if parsed is None:
                    items.append(
                        ArtifactGcItem(
                            run_id=run.run_id,
                            scope="flow_output",
                            artifact_id=output_id,
                            path="",
                            manifest_path=str(manifest),
                            retention=self._policy_for_flow_output(run, output_id).value,
                            action="protected",
                            reason="invalid_or_unsafe_manifest",
                        )
                    )
                    continue
                _, path = parsed
                retention = self._policy_for_flow_output(run, output_id)
                if (run.run_id, output_id) in binding_refs or path in binding_paths:
                    action, reason = "protected", "studio_binding_reference"
                elif retention == RetentionPolicy.PROJECT and project_owned:
                    action, reason = "protected", "project_flow_retention"
                elif not old_enough:
                    action, reason = "protected", "younger_than_min_age"
                else:
                    action = "would_delete" if dry_run else "deleted"
                    reason = f"retention_{retention.value}"
                    if not dry_run:
                        count, size = self._delete_pair(path, manifest)
                        deleted_files += count
                        reclaimed_bytes += size
                items.append(
                    ArtifactGcItem(
                        run_id=run.run_id,
                        scope="flow_output",
                        artifact_id=output_id,
                        path=str(path),
                        manifest_path=str(manifest),
                        retention=retention.value,
                        action=action,
                        reason=reason,
                    )
                )

        cache_report = self.cache.collect(
            dry_run=dry_run,
            max_idle_seconds=cache_max_idle_seconds,
        )
        reclaimed_bytes += int(cache_report.get("reclaimed_bytes", 0))
        deleted_files += int(cache_report.get("deleted_files", 0))
        return ArtifactGcReport(
            dry_run=dry_run,
            min_age_seconds=float(min_age_seconds),
            scanned_runs=scanned_runs,
            deleted_files=deleted_files,
            reclaimed_bytes=reclaimed_bytes,
            items=tuple(items),
            cache=cache_report,
        )

