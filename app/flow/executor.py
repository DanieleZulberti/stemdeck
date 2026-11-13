from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import shutil
import uuid
from typing import Callable

from .adapters import AdapterExecutionRequest, AdapterRegistry
from .artifacts import AudioArtifact, hash_file, save_artifact_manifest
from .cache import ArtifactCache, CacheKeyInput
from .capabilities import CapabilityChecker
from .planner import ExecutionPlan, FlowPlanner
from .provenance import probe_audio, runtime_environment
from .registry import ModelRegistry
from .schema import FlowDefinition, RetentionPolicy
from .validation import FlowValidator


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


class FlowExecutor:
    def __init__(
        self,
        model_registry: ModelRegistry,
        adapter_registry: AdapterRegistry,
        cache: ArtifactCache,
        runs_root: Path,
        progress_listener: ProgressListener | None = None,
    ) -> None:
        self.model_registry = model_registry
        self.adapter_registry = adapter_registry
        self.cache = cache
        self.runs_root = runs_root
        self.progress_listener = progress_listener

    def execute(
        self,
        flow: FlowDefinition,
        inputs: dict[str, Path],
        run_id: str | None = None,
        context: dict | None = None,
    ) -> RunResult:
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
        (run_dir / "inputs").mkdir(parents=True)
        (run_dir / "nodes").mkdir(parents=True)
        (run_dir / "outputs").mkdir(parents=True)

        artifacts: dict[str, AudioArtifact] = {}
        states: dict[str, str] = {name: "PENDING" for name in flow.nodes}
        cache_hits: set[str] = set()

        for input_name, source_path in inputs.items():
            source_path = source_path.resolve()
            if not source_path.is_file():
                raise FlowExecutionError(f"Flow input '{input_name}' does not exist: {source_path}")
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
            save_artifact_manifest(artifact, run_dir / "inputs" / f"{input_name}.artifact.json")

        node_records: dict[str, dict] = {}
        for stage in plan.stages:
            # The planner exposes parallelism now. The PoC executor intentionally runs
            # stage members serially until resource-aware scheduling is added.
            for node_id in stage.nodes:
                node = flow.nodes[node_id]
                model = self.model_registry.get(node.model)
                adapter = self.adapter_registry.get(model.adapter)
                available, reason = adapter.available()
                if not available:
                    states[node_id] = "FAILED"
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
                states[node_id] = "RUNNING"
                self._emit(node_id, 0.0, "starting")

                cache_metadata: dict = {}
                model_artifacts = adapter.model_artifacts(model)
                if self.cache.has(cache_key):
                    cache_metadata = self.cache.metadata(cache_key)
                    output_paths = self.cache.materialize(cache_key, output_dir)
                    states[node_id] = "CACHED"
                    cache_hits.add(node_id)
                    self._emit(node_id, 1.0, "cache hit")
                else:
                    output_paths = adapter.execute(
                        AdapterExecutionRequest(
                            node_id=node_id,
                            model=model,
                            inputs=resolved_inputs,
                            parameters=effective_params,
                            output_dir=output_dir,
                            progress=lambda fraction, message, nid=node_id: self._emit(
                                nid, fraction, message
                            ),
                            context=context,
                        )
                    )
                    expected = set(model.outputs)
                    actual = set(output_paths)
                    if expected != actual:
                        raise FlowExecutionError(
                            f"Node '{node_id}' adapter returned output ports {sorted(actual)}, "
                            f"expected {sorted(expected)}"
                        )
                    # Resolve after execution too: a first run may have downloaded the model.
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
                        "environment": run_environment,
                    }
                    self.cache.store(cache_key, output_paths, metadata=cache_metadata)
                    states[node_id] = "COMPLETED"
                    self._emit(node_id, 1.0, "completed")

                node_artifacts: dict[str, AudioArtifact] = {}
                for port, path in output_paths.items():
                    policy = node.outputs.get(port)
                    artifact = self._make_artifact(
                        path,
                        semantic_type=model.outputs[port].semantic_type,
                        producer=node_id,
                        producer_port=port,
                    )
                    artifacts[f"{node_id}.{port}"] = artifact
                    node_artifacts[port] = artifact
                    save_artifact_manifest(artifact, node_dir / f"{port}.artifact.json")

                    # Transient means the run may remove this artifact once all consumers
                    # have finished. Deletion is deferred until the graph is complete in
                    # this PoC to keep debugging straightforward.
                    if policy and policy.retain == RetentionPolicy.PROJECT:
                        pass

                node_records[node_id] = {
                    "state": states[node_id],
                    "cache_key": cache_key,
                    "model": model.id,
                    "adapter": model.adapter,
                    "adapter_version": adapter.version,
                    "parameters": effective_params,
                    "runtime_identity": runtime_identity,
                    "model_artifacts": cache_metadata.get("model_artifacts", model_artifacts),
                    "produced_environment": cache_metadata.get("environment", run_environment),
                    "provenance_origin": (
                        "cache_manifest"
                        if states[node_id] == "CACHED" and "environment" in cache_metadata
                        else "current_run"
                    ),
                    "inputs": {k: v.id for k, v in resolved_inputs.items()},
                    "outputs": {k: v.id for k, v in node_artifacts.items()},
                }

        final_outputs: dict[str, AudioArtifact] = {}
        for name, definition in flow.outputs.items():
            artifact = self._resolve_artifact(definition.source, artifacts)
            target = run_dir / "outputs" / f"{name}{artifact.path.suffix}"
            self._link_or_copy(artifact.path, target)
            final_artifact = self._make_artifact(
                target,
                semantic_type=artifact.semantic_type,
                producer=artifact.producer,
                producer_port=artifact.producer_port,
            )
            final_outputs[name] = final_artifact
            save_artifact_manifest(final_artifact, run_dir / "outputs" / f"{name}.artifact.json")

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
        return RunResult(
            run_id=run_id,
            run_dir=run_dir,
            artifacts=artifacts,
            outputs=final_outputs,
            node_states=states,
            plan=plan,
            cache_hits=cache_hits,
        )

    def _validate_input_bindings(self, flow: FlowDefinition, inputs: dict[str, Path]) -> None:
        expected = set(flow.inputs)
        actual = set(inputs)
        if expected != actual:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise FlowExecutionError(f"Input bindings mismatch. Missing={missing}, extra={extra}")

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
