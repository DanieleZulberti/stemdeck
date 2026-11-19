from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import shutil
import uuid
from typing import Callable, Any

from .adapters import AdapterExecutionRequest, AdapterRegistry
from .artifacts import AudioArtifact, hash_file, save_artifact_manifest
from .cache import ArtifactCache, CacheKeyInput
from .capabilities import CapabilityChecker
from .planner import ExecutionPlan, FlowPlanner
from .provenance import probe_audio, runtime_environment
from .registry import ModelRegistry
from .schema import FlowDefinition, OutputPolicy, RetentionPolicy
from .validation import FlowValidator
from .run_repository import FlowRunRepository, RunCancellationToken
from .runs import FlowRunCancelledError, FlowRunStatus, NodeRunStatus
from .scheduler import ResourceScheduler


ProgressListener = Callable[[str, float | None, str], None]


@dataclass
class RunResult:
    run_id: str
    run_dir: Path
    artifacts: dict[str, AudioArtifact]
    outputs: dict[str, AudioArtifact]
    node_states: dict[str, str]
    plan: ExecutionPlan
    cache_hits: set[str] = field(default_factory=set)


class FlowExecutionError(RuntimeError):
    pass


@dataclass
class _NodeOutcome:
    node_id: str
    state: str
    artifacts: dict[str, AudioArtifact] = field(default_factory=dict)
    record: dict[str, Any] = field(default_factory=dict)
    cache_hit: bool = False
    error: Exception | None = None


class FlowExecutor:
    def __init__(
        self,
        model_registry: ModelRegistry,
        adapter_registry: AdapterRegistry,
        cache: ArtifactCache,
        runs_root: Path,
        progress_listener: ProgressListener | None = None,
        run_repository: FlowRunRepository | None = None,
        max_parallel_nodes: int = 4,
    ) -> None:
        self.model_registry = model_registry
        self.adapter_registry = adapter_registry
        self.cache = cache
        self.runs_root = runs_root
        self.progress_listener = progress_listener
        self.run_repository = run_repository
        self.max_parallel_nodes = max_parallel_nodes

    def execute(
        self,
        flow: FlowDefinition,
        inputs: dict[str, Path],
        run_id: str | None = None,
        context: dict | None = None,
        retry_of_run_id: str | None = None,
        run_precreated: bool = False,
    ) -> RunResult:
        """Execute a compiled flow with persisted lifecycle when configured.

        Independent nodes in the same planner stage are submitted concurrently.
        Adapter resource claims constrain actual concurrency. A node failure only
        blocks descendants that need its missing artifacts; sibling branches keep
        running and any successfully produced Flow Outputs remain available.
        """
        context = context or {}
        validation = FlowValidator(self.model_registry).validate(flow)
        if not validation.valid:
            messages = "; ".join(issue.message for issue in validation.errors)
            raise FlowExecutionError(f"Flow validation failed: {messages}")

        capability = CapabilityChecker(self.model_registry, self.adapter_registry).check(flow)
        if not capability.runnable:
            messages = "; ".join(
                f"{issue.node}: {issue.message}"
                for issue in capability.issues
                if issue.level == "error"
            )
            raise FlowExecutionError(f"Flow capability check failed: {messages}")

        self._validate_input_bindings(flow, inputs)
        plan = FlowPlanner().plan(flow)
        repo_root = Path(__file__).resolve().parents[2]
        run_environment = runtime_environment(repo_root)

        run_id = run_id or self._new_run_id()
        run_dir = self.runs_root / run_id
        if run_dir.exists():
            raise FlowExecutionError(f"Run directory already exists: {run_dir}")

        if self.run_repository is not None:
            if run_precreated:
                existing = self.run_repository.get_run(run_id)
                if existing.definition != flow:
                    raise FlowExecutionError(
                        f"Pre-created FlowRun '{run_id}' snapshot does not match execution definition"
                    )
                if existing.status != FlowRunStatus.QUEUED:
                    raise FlowExecutionError(
                        f"Pre-created FlowRun '{run_id}' is not queued"
                    )
                if existing.retry_of_run_id != retry_of_run_id:
                    raise FlowExecutionError(
                        f"Pre-created FlowRun '{run_id}' retry ancestry does not match"
                    )
            else:
                self.run_repository.create_run(
                    flow, retry_of_run_id=retry_of_run_id, run_id=run_id
                )
            cancellation = RunCancellationToken(self.run_repository, run_id)
        else:
            cancellation = None

        (run_dir / "inputs").mkdir(parents=True)
        (run_dir / "nodes").mkdir(parents=True)
        (run_dir / "outputs").mkdir(parents=True)

        artifacts: dict[str, AudioArtifact] = {}
        states: dict[str, str] = {name: "PENDING" for name in flow.nodes}
        cache_hits: set[str] = set()
        node_records: dict[str, dict] = {}

        try:
            if self.run_repository is not None:
                self.run_repository.transition_run(run_id, FlowRunStatus.RUNNING)

            for input_name, source_path in inputs.items():
                self._raise_if_cancelled(cancellation)
                source_path = source_path.resolve()
                if not source_path.is_file():
                    raise FlowExecutionError(
                        f"Flow input '{input_name}' does not exist: {source_path}"
                    )
                suffix = source_path.suffix or ".audio"
                input_path = run_dir / "inputs" / f"{input_name}{suffix}"
                self._link_or_copy(source_path, input_path)
                artifact = self._make_artifact(
                    input_path,
                    semantic_type=flow.inputs[input_name].semantic_type,
                    producer=None,
                    producer_port=input_name,
                )
                artifacts[f"inputs.{input_name}"] = artifact
                save_artifact_manifest(
                    artifact, run_dir / "inputs" / f"{input_name}.artifact.json"
                )

            with ResourceScheduler(self.max_parallel_nodes) as scheduler:
                for stage in plan.stages:
                    self._raise_if_cancelled(cancellation)
                    futures = []
                    for node_id in stage.nodes:
                        missing = [
                            ref
                            for ref in flow.nodes[node_id].inputs.values()
                            if ref not in artifacts
                        ]
                        if missing:
                            states[node_id] = "BLOCKED"
                            diagnostic = self._execution_diagnostic(
                                "blocked_by_upstream",
                                f"Required upstream artifacts are unavailable: {sorted(missing)}",
                                node_id=node_id,
                            )
                            node_records[node_id] = {
                                "state": "BLOCKED",
                                "diagnostics": [diagnostic],
                            }
                            if self.run_repository is not None:
                                self.run_repository.transition_node(
                                    run_id,
                                    node_id,
                                    NodeRunStatus.BLOCKED,
                                    message="blocked by upstream failure",
                                    diagnostics=[diagnostic],
                                )
                            continue

                        node = flow.nodes[node_id]
                        model = self.model_registry.get(node.model)
                        adapter = self.adapter_registry.get(model.adapter)
                        params = self._effective_parameters(node_id, flow)
                        claim = adapter.resource_claim(model, params, context)
                        snapshot = dict(artifacts)
                        futures.append(
                            scheduler.submit(
                                claim,
                                lambda nid=node_id, snap=snapshot: self._execute_node(
                                    run_id=run_id,
                                    run_dir=run_dir,
                                    node_id=nid,
                                    flow=flow,
                                    artifacts=snap,
                                    context=context,
                                    environment=run_environment,
                                    cancellation=cancellation,
                                ),
                            )
                        )

                    # Consume all stage outcomes. A failure is recorded rather than
                    # raised so independent branches can finish.
                    for future in futures:
                        outcome = future.result()
                        states[outcome.node_id] = outcome.state
                        node_records[outcome.node_id] = outcome.record
                        artifacts.update(outcome.artifacts)
                        if outcome.cache_hit:
                            cache_hits.add(outcome.node_id)
                    self._raise_if_cancelled(cancellation)

            final_outputs: dict[str, AudioArtifact] = {}
            for name, definition in flow.outputs.items():
                artifact = artifacts.get(definition.source)
                if artifact is None:
                    continue
                target = run_dir / "outputs" / f"{name}{artifact.path.suffix}"
                self._link_or_copy(artifact.path, target)
                final_artifact = self._make_artifact(
                    target,
                    semantic_type=artifact.semantic_type,
                    producer=artifact.producer,
                    producer_port=artifact.producer_port,
                )
                final_outputs[name] = final_artifact
                save_artifact_manifest(
                    final_artifact, run_dir / "outputs" / f"{name}.artifact.json"
                )

            self._write_run_manifest(
                run_dir=run_dir,
                run_id=run_id,
                flow=flow,
                inputs=artifacts,
                nodes=node_records,
                outputs=final_outputs,
                plan=plan,
                environment=run_environment,
            )

            failed = {n for n, state in states.items() if state == "FAILED"}
            blocked = {n for n, state in states.items() if state == "BLOCKED"}
            if self.run_repository is not None:
                if failed or blocked:
                    target = (
                        FlowRunStatus.PARTIALLY_FAILED
                        if final_outputs
                        else FlowRunStatus.FAILED
                    )
                else:
                    target = FlowRunStatus.SUCCEEDED
                self.run_repository.transition_run(run_id, target)

            return RunResult(
                run_id=run_id,
                run_dir=run_dir,
                artifacts=artifacts,
                outputs=final_outputs,
                node_states=states,
                plan=plan,
                cache_hits=cache_hits,
            )
        except FlowRunCancelledError:
            self._settle_cancelled_nodes(run_id, states)
            if self.run_repository is not None:
                current = self.run_repository.get_run(run_id)
                if current.status == FlowRunStatus.RUNNING:
                    self.run_repository.transition_run(run_id, FlowRunStatus.CANCELLED)
            raise
        except Exception as exc:
            if self.run_repository is not None:
                current = self.run_repository.get_run(run_id)
                if current.status == FlowRunStatus.RUNNING:
                    self.run_repository.add_run_diagnostic(
                        run_id,
                        self._execution_diagnostic(
                            "run_execution_failed", str(exc) or exc.__class__.__name__
                        ),
                    )
                    self.run_repository.transition_run(run_id, FlowRunStatus.FAILED)
            raise

    def retry(
        self,
        run_id: str,
        inputs: dict[str, Path],
        *,
        context: dict | None = None,
    ) -> RunResult:
        """Create a new run from an immutable prior snapshot and reuse cache."""
        if self.run_repository is None:
            raise FlowExecutionError("Retry requires a FlowRunRepository")
        previous = self.run_repository.get_run(run_id)
        return self.execute(
            previous.definition,
            inputs,
            context=context,
            retry_of_run_id=run_id,
        )

    def _execute_node(
        self,
        *,
        run_id: str,
        run_dir: Path,
        node_id: str,
        flow: FlowDefinition,
        artifacts: dict[str, AudioArtifact],
        context: dict,
        environment: dict,
        cancellation: RunCancellationToken | None,
    ) -> _NodeOutcome:
        node = flow.nodes[node_id]
        model = self.model_registry.get(node.model)
        adapter = self.adapter_registry.get(model.adapter)
        try:
            self._raise_if_cancelled(cancellation)
            available, reason = adapter.available()
            if not available:
                raise FlowExecutionError(
                    f"Node '{node_id}' cannot run with adapter '{model.adapter}': {reason}"
                )

            resolved_inputs = {
                port: self._resolve_artifact(ref, artifacts)
                for port, ref in node.inputs.items()
            }
            effective_params = self._effective_parameters(node_id, flow)
            runtime_identity = adapter.cache_identity(model, effective_params, context)
            cache_key = CacheKeyInput(
                model_id=model.id,
                model_hash=model.model_sha256,
                adapter=model.adapter,
                adapter_version=adapter.version,
                parameters=effective_params,
                input_hashes={k: v.sha256 for k, v in sorted(resolved_inputs.items())},
                runtime_identity=runtime_identity,
            ).digest()

            node_dir = run_dir / "nodes" / node_id
            output_dir = node_dir / "outputs"
            output_dir.mkdir(parents=True, exist_ok=True)
            cache_metadata: dict = {}
            model_artifacts = adapter.model_artifacts(model)
            cache_hit = False
            cache_allowed = self._node_cache_allowed(flow, node_id, set(model.outputs))
            if cache_allowed and self.cache.has(cache_key):
                cache_metadata = self.cache.metadata(cache_key)
                output_paths = self.cache.materialize(cache_key, output_dir)
                cache_hit = True
                self._emit(node_id, 1.0, "cache hit")
            else:
                if self.run_repository is not None:
                    self.run_repository.transition_node(
                        run_id, node_id, NodeRunStatus.RUNNING, message="starting"
                    )
                self._emit(node_id, 0.0, "starting")
                output_paths = adapter.execute(
                    AdapterExecutionRequest(
                        node_id=node_id,
                        model=model,
                        inputs=resolved_inputs,
                        parameters=effective_params,
                        output_dir=output_dir,
                        progress=lambda fraction, message, nid=node_id: self._node_progress(
                            run_id, nid, fraction, message
                        ),
                        context=context,
                        cancellation_requested=(
                            (lambda: cancellation.is_cancel_requested)
                            if cancellation is not None
                            else None
                        ),
                    )
                )
                self._raise_if_cancelled(cancellation)
                expected = set(model.outputs)
                actual = set(output_paths)
                if expected != actual:
                    raise FlowExecutionError(
                        f"Node '{node_id}' adapter returned output ports {sorted(actual)}, "
                        f"expected {sorted(expected)}"
                    )
                model_artifacts = adapter.model_artifacts(model)
                cache_metadata = {
                    "node": node_id,
                    "model_id": model.id,
                    "model_hash": model.model_sha256,
                    "adapter": model.adapter,
                    "adapter_version": adapter.version,
                    "parameters": effective_params,
                    "input_hashes": {
                        k: v.sha256 for k, v in sorted(resolved_inputs.items())
                    },
                    "runtime_identity": runtime_identity,
                    "model_artifacts": model_artifacts,
                    "environment": environment,
                }
                if cache_allowed:
                    self.cache.store(cache_key, output_paths, metadata=cache_metadata)
                self._emit(node_id, 1.0, "completed")

            node_artifacts: dict[str, AudioArtifact] = {}
            artifact_refs: dict[str, AudioArtifact] = {}
            for port, path in output_paths.items():
                artifact = self._make_artifact(
                    path,
                    semantic_type=model.outputs[port].semantic_type,
                    producer=node_id,
                    producer_port=port,
                )
                artifact_refs[f"{node_id}.{port}"] = artifact
                node_artifacts[port] = artifact
                save_artifact_manifest(artifact, node_dir / f"{port}.artifact.json")

            state = "CACHED" if cache_hit else "COMPLETED"
            if self.run_repository is not None:
                self.run_repository.transition_node(
                    run_id,
                    node_id,
                    NodeRunStatus.CACHED if cache_hit else NodeRunStatus.SUCCEEDED,
                    message="cache hit" if cache_hit else "completed",
                )
            record = {
                "state": state,
                "cache_key": cache_key if cache_allowed else None,
                "cache_allowed": cache_allowed,
                "model": model.id,
                "adapter": model.adapter,
                "adapter_version": adapter.version,
                "parameters": effective_params,
                "runtime_identity": runtime_identity,
                "model_artifacts": cache_metadata.get("model_artifacts", model_artifacts),
                "produced_environment": cache_metadata.get("environment", environment),
                "provenance_origin": (
                    "cache_manifest"
                    if cache_hit and "environment" in cache_metadata
                    else "current_run"
                ),
                "inputs": {k: v.id for k, v in resolved_inputs.items()},
                "outputs": {k: v.id for k, v in node_artifacts.items()},
            }
            return _NodeOutcome(
                node_id=node_id,
                state=state,
                artifacts=artifact_refs,
                record=record,
                cache_hit=cache_hit,
            )
        except FlowRunCancelledError as exc:
            if self.run_repository is not None:
                current = self.run_repository.get_node_run(run_id, node_id)
                if current.status in {NodeRunStatus.PENDING, NodeRunStatus.RUNNING}:
                    self.run_repository.transition_node(
                        run_id, node_id, NodeRunStatus.CANCELLED, message="cancelled"
                    )
            return _NodeOutcome(node_id=node_id, state="CANCELLED", error=exc)
        except Exception as exc:
            diagnostic = self._execution_diagnostic(
                "node_execution_failed", str(exc), node_id=node_id
            )
            if self.run_repository is not None:
                current = self.run_repository.get_node_run(run_id, node_id)
                if current.status == NodeRunStatus.PENDING:
                    self.run_repository.transition_node(
                        run_id, node_id, NodeRunStatus.RUNNING, message="starting"
                    )
                current = self.run_repository.get_node_run(run_id, node_id)
                if current.status == NodeRunStatus.RUNNING:
                    self.run_repository.transition_node(
                        run_id,
                        node_id,
                        NodeRunStatus.FAILED,
                        message=str(exc),
                        diagnostics=[diagnostic],
                    )
            return _NodeOutcome(
                node_id=node_id,
                state="FAILED",
                record={"state": "FAILED", "diagnostics": [diagnostic]},
                error=exc,
            )

    def _node_progress(
        self, run_id: str, node_id: str, fraction: float | None, message: str
    ) -> None:
        self._emit(node_id, fraction, message)
        if self.run_repository is not None:
            try:
                self.run_repository.update_node_progress(run_id, node_id, fraction, message)
            except Exception:
                # Progress must never turn an otherwise valid node execution into
                # a processing failure. Lifecycle transitions remain authoritative.
                pass

    @staticmethod
    def _execution_diagnostic(code: str, message: str, *, node_id: str | None = None) -> dict:
        return {
            "source": "executor",
            "severity": "error",
            "code": code,
            "message": message,
            "node_id": node_id,
            "port_id": None,
            "output_id": None,
        }

    @staticmethod
    def _raise_if_cancelled(cancellation: RunCancellationToken | None) -> None:
        if cancellation is not None:
            cancellation.raise_if_cancelled()

    def _settle_cancelled_nodes(self, run_id: str, states: dict[str, str]) -> None:
        if self.run_repository is None:
            return
        for node_id, state in states.items():
            if state != "PENDING":
                continue
            current = self.run_repository.get_node_run(run_id, node_id)
            if current.status == NodeRunStatus.PENDING:
                self.run_repository.transition_node(
                    run_id, node_id, NodeRunStatus.CANCELLED, message="cancelled"
                )

    def _validate_input_bindings(self, flow: FlowDefinition, inputs: dict[str, Path]) -> None:
        expected = set(flow.inputs)
        actual = set(inputs)
        if expected != actual:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise FlowExecutionError(f"Input bindings mismatch. Missing={missing}, extra={extra}")

    @staticmethod
    def _node_cache_allowed(
        flow: FlowDefinition, node_id: str, output_ports: set[str]
    ) -> bool:
        """Return whether the node result may enter/use the content cache.

        ArtifactCache entries are node-atomic in the current runtime. If any
        output port is `retain=transient`, caching the complete node result
        would silently retain that transient artifact. The safe MVP rule is
        therefore to disable cache use/store for the whole node in mixed or
        fully transient cases.
        """
        node = flow.nodes[node_id]
        return all(
            node.outputs.get(port, OutputPolicy()).retain != RetentionPolicy.TRANSIENT
            for port in output_ports
        )

    def _effective_parameters(self, node_id: str, flow: FlowDefinition) -> dict:
        node = flow.nodes[node_id]
        model = self.model_registry.get(node.model)
        values: dict = {}
        for name, definition in model.parameters.items():
            if definition.default is not None:
                values[name] = definition.default
        values.update(node.params)
        return values

    def _resolve_artifact(self, ref: str, artifacts: dict[str, AudioArtifact]) -> AudioArtifact:
        try:
            return artifacts[ref]
        except KeyError as exc:
            raise FlowExecutionError(f"Artifact '{ref}' has not been produced") from exc

    def _make_artifact(
        self,
        path: Path,
        semantic_type: str,
        producer: str | None,
        producer_port: str | None,
    ) -> AudioArtifact:
        digest = hash_file(path)
        artifact_id = f"sha256:{digest}"
        audio = probe_audio(path)
        return AudioArtifact(
            id=artifact_id,
            path=path.resolve(),
            semantic_type=semantic_type,
            sha256=digest,
            producer=producer,
            producer_port=producer_port,
            sample_rate=audio.get("sample_rate"),
            channels=audio.get("channels"),
            duration_seconds=audio.get("duration_seconds"),
            codec_name=audio.get("codec_name"),
            sample_format=audio.get("sample_format"),
            bit_depth=audio.get("bit_depth"),
            bit_rate=audio.get("bit_rate"),
            container_format=audio.get("container_format"),
        )

    def _write_run_manifest(
        self,
        run_dir: Path,
        run_id: str,
        flow: FlowDefinition,
        inputs: dict[str, AudioArtifact],
        nodes: dict[str, dict],
        outputs: dict[str, AudioArtifact],
        plan: ExecutionPlan,
        environment: dict,
    ) -> None:
        manifest = {
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "environment": environment,
            "flow": flow.model_dump(mode="json"),
            "execution_plan": [list(stage.nodes) for stage in plan.stages],
            "inputs": {
                ref: artifact.to_json_dict()
                for ref, artifact in inputs.items()
                if ref.startswith("inputs.")
            },
            "nodes": nodes,
            "outputs": {name: artifact.to_json_dict() for name, artifact in outputs.items()},
        }
        (run_dir / "run.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def _emit(self, node_id: str, fraction: float | None, message: str) -> None:
        if self.progress_listener:
            self.progress_listener(node_id, fraction, message)

    def _new_run_id(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _link_or_copy(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
