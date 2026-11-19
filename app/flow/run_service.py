from __future__ import annotations

import json
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import RLock
from typing import Any

from app.core.config import FLOW_CACHE_DIR, FLOW_DB_PATH, FLOW_RUNS_DIR

from .cache import ArtifactCache
from .capabilities import CapabilityChecker
from .compiler import FlowCompiler
from .defaults import build_default_adapter_registry, build_default_node_definition_registry
from .executor import FlowExecutionError, FlowExecutor
from .persistence import FlowRepository, FlowRevisionConflictError
from .run_repository import FlowRunNotFoundError, FlowRunRepository
from .runs import FlowRunCancelledError, FlowRunRecord, FlowRunStatus, NodeRunRecord, NodeRunStatus
from .validation import FlowValidator

log = logging.getLogger(__name__)


class FlowRunStartError(RuntimeError):
    def __init__(self, code: str, message: str, *, diagnostics: list[dict] | None = None) -> None:
        self.code = code
        self.diagnostics = diagnostics or []
        super().__init__(message)


class FlowRunService:
    """In-process orchestration facade used by the monitoring API.

    Run state itself is durable in SQLite. Execution dispatch is deliberately
    process-local for M5: after a process restart a previously queued/running
    run remains visible but is not silently resumed. Recovery is a later
    scheduler concern and must not mutate an immutable run snapshot implicitly.

    Only one FlowRun is dispatched at a time. Node-level parallelism still
    happens inside FlowExecutor. This also preserves process-wide safety for
    legacy singleton workers until resource claims are promoted to a scheduler
    shared across multiple FlowRuns.
    """

    def __init__(
        self,
        *,
        flow_repository: FlowRepository | None = None,
        run_repository: FlowRunRepository | None = None,
        runs_root: Path = FLOW_RUNS_DIR,
        cache_root: Path = FLOW_CACHE_DIR,
    ) -> None:
        self.flow_repository = flow_repository or FlowRepository(FLOW_DB_PATH)
        self.run_repository = run_repository or FlowRunRepository(FLOW_DB_PATH)
        self.runs_root = Path(runs_root)
        self.cache_root = Path(cache_root)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._dispatch = ThreadPoolExecutor(max_workers=1, thread_name_prefix="flow-run")
        self._lock = RLock()
        self._futures: dict[str, Future] = {}

    @staticmethod
    def _diagnostics(items) -> list[dict]:
        return [item.to_dict() for item in items]

    def _prepare_definition(self, flow_id: str, *, expected_revision: int | None = None):
        record = self.flow_repository.get(flow_id)
        if expected_revision is not None and record.revision != expected_revision:
            raise FlowRevisionConflictError(flow_id, expected_revision, record.revision)

        compiled = FlowCompiler().compile(record.document)
        if compiled.definition is None:
            raise FlowRunStartError(
                "compile_failed",
                "Flow document cannot be compiled",
                diagnostics=self._diagnostics(compiled.diagnostics),
            )

        definitions = build_default_node_definition_registry()
        validation = FlowValidator(definitions).validate(compiled.definition)
        if not validation.valid:
            raise FlowRunStartError(
                "validation_failed",
                "Flow definition is invalid",
                diagnostics=self._diagnostics(validation.issues),
            )

        try:
            adapters = build_default_adapter_registry()
        except ImportError as exc:
            raise FlowRunStartError(
                "runtime_unavailable",
                f"Flow runtime dependencies are unavailable: {exc}",
            ) from exc
        capability = CapabilityChecker(definitions, adapters).check(compiled.definition)
        if not capability.runnable:
            raise FlowRunStartError(
                "capability_failed",
                "Flow is valid but not runnable in this environment",
                diagnostics=self._diagnostics(capability.issues),
            )
        return compiled.definition, definitions, adapters

    @staticmethod
    def _validate_input_paths(definition, inputs: dict[str, Path]) -> dict[str, Path]:
        expected = set(definition.inputs)
        actual = set(inputs)
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing or extra:
            detail = []
            if missing:
                detail.append(f"missing inputs: {missing}")
            if extra:
                detail.append(f"unknown inputs: {extra}")
            raise FlowRunStartError("input_binding_invalid", "; ".join(detail))

        resolved: dict[str, Path] = {}
        for name, path in inputs.items():
            value = Path(path).expanduser().resolve()
            if not value.is_file():
                raise FlowRunStartError(
                    "input_not_found", f"Flow input '{name}' does not exist: {value}"
                )
            resolved[name] = value
        return resolved

    def start(
        self,
        flow_id: str,
        inputs: dict[str, Path],
        *,
        expected_revision: int | None = None,
    ) -> FlowRunRecord:
        definition, definitions, adapters = self._prepare_definition(
            flow_id, expected_revision=expected_revision
        )
        bound_inputs = self._validate_input_paths(definition, inputs)
        run = self.run_repository.create_run(definition)
        self._submit(run, bound_inputs, definitions, adapters)
        return run

    def retry(self, run_id: str) -> FlowRunRecord:
        previous = self.run_repository.get_run(run_id)
        if previous.status not in {
            FlowRunStatus.SUCCEEDED,
            FlowRunStatus.PARTIALLY_FAILED,
            FlowRunStatus.FAILED,
            FlowRunStatus.CANCELLED,
        }:
            raise FlowRunStartError(
                "run_not_terminal", "Only a terminal FlowRun can be retried"
            )

        definitions = build_default_node_definition_registry()
        validation = FlowValidator(definitions).validate(previous.definition)
        if not validation.valid:
            raise FlowRunStartError(
                "snapshot_invalid",
                "Stored FlowRun snapshot is no longer valid",
                diagnostics=self._diagnostics(validation.issues),
            )
        try:
            adapters = build_default_adapter_registry()
        except ImportError as exc:
            raise FlowRunStartError(
                "runtime_unavailable",
                f"Flow runtime dependencies are unavailable: {exc}",
            ) from exc
        capability = CapabilityChecker(definitions, adapters).check(previous.definition)
        if not capability.runnable:
            raise FlowRunStartError(
                "capability_failed",
                "Stored FlowRun snapshot is not runnable in this environment",
                diagnostics=self._diagnostics(capability.issues),
            )

        inputs = self._snapshot_inputs(previous)
        run = self.run_repository.create_run(
            previous.definition, retry_of_run_id=previous.run_id
        )
        self._submit(run, inputs, definitions, adapters)
        return run

    def _snapshot_inputs(self, run: FlowRunRecord) -> dict[str, Path]:
        input_dir = self.runs_root / run.run_id / "inputs"
        resolved: dict[str, Path] = {}
        for name in run.definition.inputs:
            manifest = input_dir / f"{name}.artifact.json"
            if not manifest.is_file():
                raise FlowRunStartError(
                    "retry_input_unavailable",
                    f"Input snapshot '{name}' is unavailable for Run '{run.run_id}'",
                )
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                path = Path(data["path"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise FlowRunStartError(
                    "retry_input_unavailable",
                    f"Input snapshot '{name}' is unreadable for Run '{run.run_id}'",
                ) from exc
            if not path.is_file():
                raise FlowRunStartError(
                    "retry_input_unavailable",
                    f"Input snapshot '{name}' no longer exists for Run '{run.run_id}'",
                )
            resolved[name] = path
        return resolved

    def _submit(self, run, inputs, definitions, adapters) -> None:
        executor = FlowExecutor(
            model_registry=definitions,
            adapter_registry=adapters,
            cache=ArtifactCache(self.cache_root),
            runs_root=self.runs_root,
            run_repository=self.run_repository,
        )

        def execute() -> None:
            try:
                executor.execute(
                    run.definition,
                    inputs,
                    run_id=run.run_id,
                    retry_of_run_id=run.retry_of_run_id,
                    run_precreated=True,
                )
            except FlowRunCancelledError:
                # Cancellation is an expected terminal outcome and lifecycle is
                # already persisted by FlowExecutor.
                pass
            except Exception as exc:
                # FlowExecutor persists ordinary running failures itself. This
                # fallback covers dispatch/pre-execution failures that happen
                # before the executor has entered its lifecycle try-block.
                current = self.run_repository.get_run(run.run_id)
                if current.status in {FlowRunStatus.QUEUED, FlowRunStatus.RUNNING}:
                    self.run_repository.add_run_diagnostic(
                        run.run_id,
                        {
                            "source": "executor",
                            "severity": "error",
                            "code": "run_dispatch_failed",
                            "message": str(exc) or exc.__class__.__name__,
                            "node_id": None,
                            "port_id": None,
                            "output_id": None,
                        },
                    )
                    current = self.run_repository.get_run(run.run_id)
                    if current.status in {FlowRunStatus.QUEUED, FlowRunStatus.RUNNING}:
                        self.run_repository.transition_run(run.run_id, FlowRunStatus.FAILED)
                log.exception("FlowRun %s failed", run.run_id)
            finally:
                with self._lock:
                    self._futures.pop(run.run_id, None)

        future = self._dispatch.submit(execute)
        with self._lock:
            self._futures[run.run_id] = future

    def request_cancel(self, run_id: str) -> FlowRunRecord:
        run = self.run_repository.request_cancel(run_id)
        if run.status == FlowRunStatus.QUEUED:
            with self._lock:
                future = self._futures.get(run_id)
            if future is not None and future.cancel():
                for node in self.run_repository.list_node_runs(run_id):
                    if node.status.value == "pending":
                        self.run_repository.transition_node(
                            run_id, node.node_id, NodeRunStatus.CANCELLED, message="cancelled before start"
                        )
                return self.run_repository.transition_run(run_id, FlowRunStatus.CANCELLED)
        return self.run_repository.get_run(run_id)

    def get(self, run_id: str) -> FlowRunRecord:
        return self.run_repository.get_run(run_id)

    def list(self, *, flow_id: str | None = None) -> list[FlowRunRecord]:
        return self.run_repository.list_runs(flow_id=flow_id)

    def nodes(self, run_id: str) -> list[NodeRunRecord]:
        return self.run_repository.list_node_runs(run_id)

    def outputs(self, run: FlowRunRecord) -> dict[str, dict[str, Any]]:
        output_dir = self.runs_root / run.run_id / "outputs"
        payload: dict[str, dict[str, Any]] = {}
        for output_id, definition in run.definition.outputs.items():
            manifest = output_dir / f"{output_id}.artifact.json"
            artifact = None
            if manifest.is_file():
                try:
                    artifact = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    artifact = None
            payload[output_id] = {
                "output_id": output_id,
                "source": definition.source,
                "publish": definition.publish,
                "available": artifact is not None,
                "artifact": artifact,
            }
        return payload

    def close(self) -> None:
        self._dispatch.shutdown(wait=False, cancel_futures=False)
