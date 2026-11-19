from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.core.config import FLOW_DB_PATH, FLOW_RUNS_DIR
from .persistence import FlowNotFoundError, FlowRepository
from .project_repository import ProjectFlowNotFoundError, ProjectRepository
from .projects import ProjectFlowRecord, ProjectRecord, StudioBindingRecord, StudioBindingStatus
from .run_repository import FlowRunNotFoundError, FlowRunRepository
from .runs import FlowRunStatus


class ProjectServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ApplyResult:
    project_id: str
    flow_id: str
    run_id: str
    created: tuple[StudioBindingRecord, ...]
    existing: tuple[StudioBindingRecord, ...]
    updated: tuple[dict, ...]
    detached: tuple[StudioBindingRecord, ...]
    skipped: tuple[dict, ...]


class ProjectService:
    def __init__(
        self,
        *,
        flow_repository: FlowRepository | None = None,
        run_repository: FlowRunRepository | None = None,
        project_repository: ProjectRepository | None = None,
        runs_root: Path = FLOW_RUNS_DIR,
    ) -> None:
        self.flow_repository = flow_repository or FlowRepository(FLOW_DB_PATH)
        self.run_repository = run_repository or FlowRunRepository(FLOW_DB_PATH)
        self.project_repository = project_repository or ProjectRepository(FLOW_DB_PATH)
        self.runs_root = Path(runs_root)

    def create_project(self, name: str) -> ProjectRecord:
        return self.project_repository.create_project(name)

    def list_projects(self) -> list[ProjectRecord]:
        return self.project_repository.list_projects()

    def get_project(self, project_id: str) -> ProjectRecord:
        return self.project_repository.get_project(project_id)

    def rename_project(self, project_id: str, name: str) -> ProjectRecord:
        return self.project_repository.rename_project(project_id, name)

    def delete_project(self, project_id: str) -> None:
        flow_ids = [item.flow_id for item in self.project_repository.list_project_flows(project_id)]
        # Project deletion owns Project Flow lifecycle. Remove ownership/bindings
        # first, then delete the now-unreferenced project-kind Flow rows.
        self.project_repository.delete_project(project_id)
        for flow_id in flow_ids:
            try:
                self.flow_repository.delete(flow_id)
            except FlowNotFoundError:
                # Concurrent deletion is equivalent to the desired final state.
                continue

    def copy_flow_to_project(
        self, project_id: str, source_flow_id: str, *, name: str | None = None
    ) -> ProjectFlowRecord:
        self.project_repository.get_project(project_id)
        source = self.flow_repository.get(source_flow_id)
        clone = source.document.model_copy(
            update={
                "flow_id": None,
                "revision": 1,
                "name": name or source.name,
            },
            deep=True,
        )
        project_flow = self.flow_repository.create(clone, kind="project")
        try:
            return self.project_repository.attach_project_flow(
                project_id,
                project_flow.flow_id,
                source_flow_id=source.flow_id,
                source_flow_revision=source.revision,
            )
        except Exception:
            # Avoid leaving a project-kind Flow with no Project owner if the
            # ownership write fails after the Flow row has been created.
            self.flow_repository.delete(project_flow.flow_id)
            raise

    def list_project_flows(self, project_id: str) -> list[dict]:
        records = self.project_repository.list_project_flows(project_id)
        result: list[dict] = []
        for ownership in records:
            flow = self.flow_repository.get(ownership.flow_id)
            result.append(
                {
                    "ownership": ownership,
                    "flow": flow,
                }
            )
        return result

    def list_bindings(self, project_id: str) -> list[StudioBindingRecord]:
        return self.project_repository.list_bindings(project_id)

    def detach_binding(self, project_id: str, binding_id: str) -> StudioBindingRecord:
        binding = self.project_repository.get_binding_by_id(binding_id)
        if binding.project_id != project_id:
            raise ProjectServiceError(
                "binding_not_in_project",
                f"Studio binding '{binding_id}' does not belong to Project '{project_id}'",
            )
        return self.project_repository.set_binding_status(
            binding_id, StudioBindingStatus.DETACHED
        )

    def _artifact_for_output(self, run_id: str, output_id: str) -> dict | None:
        output_dir = (self.runs_root / run_id / "outputs").resolve()
        manifest = (output_dir / f"{output_id}.artifact.json").resolve()
        if not manifest.is_relative_to(output_dir) or not manifest.is_file():
            return None
        try:
            artifact = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        path_value = artifact.get("path")
        if not isinstance(path_value, str):
            return None
        path = Path(path_value).resolve()
        if not path.is_relative_to(output_dir) or not path.is_file():
            return None
        return artifact

    @staticmethod
    def _temporal_compatibility(previous: dict, current: dict) -> dict:
        """MVP whole-file temporal compatibility for Studio reconciliation.

        Timeline origin is fixed at zero by the current Flow MVP. Duration is
        therefore the only temporal extent that can change. ffprobe/container
        duration values can differ by tiny rounding amounts, so <= 50 ms is
        treated as the same timeline. Unknown durations are conservatively not
        compatible.
        """
        previous_duration = previous.get("duration_seconds")
        current_duration = current.get("duration_seconds")
        try:
            previous_duration = float(previous_duration)
            current_duration = float(current_duration)
        except (TypeError, ValueError):
            return {
                "compatible": False,
                "reason": "duration_unknown",
                "origin_seconds": 0.0,
                "tolerance_seconds": 0.05,
            }
        delta = abs(previous_duration - current_duration)
        return {
            "compatible": delta <= 0.05,
            "reason": "compatible" if delta <= 0.05 else "duration_changed",
            "origin_seconds": 0.0,
            "previous_duration_seconds": previous_duration,
            "current_duration_seconds": current_duration,
            "duration_delta_seconds": delta,
            "tolerance_seconds": 0.05,
        }

    def apply_run(self, project_id: str, run_id: str) -> ApplyResult:
        self.project_repository.get_project(project_id)
        try:
            run = self.run_repository.get_run(run_id)
        except FlowRunNotFoundError as exc:
            raise ProjectServiceError("run_not_found", str(exc)) from exc

        if run.status not in {
            FlowRunStatus.SUCCEEDED,
            FlowRunStatus.PARTIALLY_FAILED,
            FlowRunStatus.FAILED,
            FlowRunStatus.CANCELLED,
        }:
            raise ProjectServiceError(
                "run_not_terminal", "Apply to Studio requires a terminal FlowRun"
            )
        if not run.flow_id:
            raise ProjectServiceError(
                "run_has_no_flow", "FlowRun snapshot has no stable flow identity"
            )
        try:
            self.project_repository.get_project_flow(project_id, run.flow_id)
        except ProjectFlowNotFoundError as exc:
            raise ProjectServiceError(
                "run_not_project_flow",
                f"Run '{run_id}' does not belong to a Project Flow in Project '{project_id}'",
            ) from exc

        created: list[StudioBindingRecord] = []
        existing: list[StudioBindingRecord] = []
        updated: list[dict] = []
        skipped: list[dict] = []

        published_output_ids = {
            output_id for output_id, output in run.definition.outputs.items() if output.publish
        }
        detached = self.project_repository.detach_outputs_not_in(
            project_id, run.flow_id, published_output_ids
        )

        for output_id, output in run.definition.outputs.items():
            if not output.publish:
                skipped.append({"output_id": output_id, "reason": "not_published"})
                continue
            artifact = self._artifact_for_output(run_id, output_id)
            if artifact is not None:
                artifact = {**artifact, "label": output.label or output_id}
            if artifact is None:
                skipped.append({"output_id": output_id, "reason": "artifact_unavailable"})
                continue
            if artifact.get("artifact_type", "audio") != "audio":
                skipped.append({
                    "output_id": output_id,
                    "reason": "unsupported_artifact_type",
                    "artifact_type": artifact.get("artifact_type"),
                })
                continue

            before = self.project_repository.get_binding(project_id, run.flow_id, output_id)
            if before is None:
                created.append(
                    self.project_repository.create_binding(
                        project_id=project_id,
                        flow_id=run.flow_id,
                        output_id=output_id,
                        run_id=run_id,
                        artifact=artifact,
                    )
                )
                continue

            if before.current_run_id == run_id and before.status == StudioBindingStatus.MANAGED:
                existing.append(before)
                continue

            temporal = self._temporal_compatibility(before.artifact, artifact)
            reconciled = self.project_repository.reconcile_binding(
                before.binding_id,
                run_id=run_id,
                artifact=artifact,
                status=StudioBindingStatus.MANAGED,
            )
            updated.append({
                "binding": reconciled,
                "previous_run_id": before.current_run_id,
                "temporal_compatibility": temporal,
            })

        if not created and not existing and not updated and not detached and not skipped:
            raise ProjectServiceError(
                "no_outputs", "FlowRun has no Flow Outputs to apply"
            )
        return ApplyResult(
            project_id=project_id,
            flow_id=run.flow_id,
            run_id=run_id,
            created=tuple(created),
            existing=tuple(existing),
            updated=tuple(updated),
            detached=tuple(detached),
            skipped=tuple(skipped),
        )

