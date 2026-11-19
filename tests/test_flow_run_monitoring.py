from __future__ import annotations

import shutil
import time
from pathlib import Path

from app.flow.adapters import AdapterExecutionRequest, AdapterRegistry, ModelAdapter
from app.flow.persistence import FlowRepository
from app.flow.registry import ModelRegistry
from app.flow.run_repository import FlowRunRepository
from app.flow.run_service import FlowRunService
from app.flow.runs import FlowRunStatus, NodeRunStatus
from app.flow.scheduler import ResourceClaim
from app.flow.schema import FlowDefinition, ModelDefinition, PortDefinition


class CopyAdapter(ModelAdapter):
    id = "copy"

    @property
    def version(self):
        return "1"

    def available(self):
        return True, None

    def resource_claim(self, model, parameters, context):
        return ResourceClaim("copy-test", 2)

    def execute(self, request: AdapterExecutionRequest):
        request.output_dir.mkdir(parents=True, exist_ok=True)
        target = request.output_dir / "audio.wav"
        shutil.copy2(next(iter(request.inputs.values())).path, target)
        if request.progress:
            request.progress(0.5, "half")
        return {"audio": target}


def _runtime():
    models = ModelRegistry()
    models.register(
        ModelDefinition(
            id="copy-model",
            adapter="copy",
            inputs={"audio": PortDefinition(semantic_type="mixture")},
            outputs={"audio": PortDefinition(semantic_type="mixture")},
        )
    )
    adapters = AdapterRegistry()
    adapters.register(CopyAdapter())
    definition = FlowDefinition.model_validate(
        {
            "flow_id": "flow-1",
            "name": "monitoring",
            "revision": 4,
            "inputs": {"song": {"semantic_type": "mixture"}},
            "nodes": {
                "copy": {
                    "model": "copy-model",
                    "inputs": {"audio": "inputs.song"},
                }
            },
            "outputs": {"result": {"source": "copy.audio", "publish": True}},
        }
    )
    return definition, models, adapters


def _wait(repo: FlowRunRepository, run_id: str, timeout: float = 5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = repo.get_run(run_id)
        if run.status in {
            FlowRunStatus.SUCCEEDED,
            FlowRunStatus.PARTIALLY_FAILED,
            FlowRunStatus.FAILED,
            FlowRunStatus.CANCELLED,
        }:
            return run
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def test_service_precreates_monitorable_run_and_retry_reuses_snapshot(tmp_path, monkeypatch):
    definition, models, adapters = _runtime()
    db = tmp_path / "flow.sqlite3"
    flow_repo = FlowRepository(db)
    run_repo = FlowRunRepository(db)
    service = FlowRunService(
        flow_repository=flow_repo,
        run_repository=run_repo,
        runs_root=tmp_path / "runs",
        cache_root=tmp_path / "cache",
    )
    monkeypatch.setattr(service, "_prepare_definition", lambda *a, **k: (definition, models, adapters))
    monkeypatch.setattr("app.flow.run_service.build_default_node_definition_registry", lambda: models)
    monkeypatch.setattr("app.flow.run_service.build_default_adapter_registry", lambda: adapters)
    source = tmp_path / "song.wav"
    source.write_bytes(b"audio")

    queued = service.start("flow-1", {"song": source})
    assert queued.status == FlowRunStatus.QUEUED
    assert run_repo.get_run(queued.run_id).status in {FlowRunStatus.QUEUED, FlowRunStatus.RUNNING, FlowRunStatus.SUCCEEDED}
    first = _wait(run_repo, queued.run_id)
    assert first.status == FlowRunStatus.SUCCEEDED
    assert service.outputs(first)["result"]["available"] is True

    retry = service.retry(first.run_id)
    second = _wait(run_repo, retry.run_id)
    assert second.retry_of_run_id == first.run_id
    node = run_repo.get_node_run(second.run_id, "copy")
    assert node.status == NodeRunStatus.CACHED
    assert node.cache_hit is True
    service.close()


def test_run_level_diagnostic_is_versioned(tmp_path):
    _, _, _ = _runtime()
    repo = FlowRunRepository(tmp_path / "flow.sqlite3")
    definition, _, _ = _runtime()
    run = repo.create_run(definition, run_id="r1")
    before = run.version
    updated = repo.add_run_diagnostic(
        "r1",
        {
            "source": "executor",
            "severity": "error",
            "code": "scheduler_failed",
            "message": "boom",
            "node_id": None,
            "port_id": None,
            "output_id": None,
        },
    )
    assert updated.version == before + 1
    assert updated.diagnostics[0]["code"] == "scheduler_failed"


def test_queued_run_can_fail_with_dispatch_diagnostic(tmp_path):
    definition, _, _ = _runtime()
    repo = FlowRunRepository(tmp_path / "flow.sqlite3")
    repo.create_run(definition, run_id="dispatch-fail")
    repo.add_run_diagnostic(
        "dispatch-fail",
        {
            "source": "executor",
            "severity": "error",
            "code": "run_dispatch_failed",
            "message": "runtime disappeared",
            "node_id": None,
            "port_id": None,
            "output_id": None,
        },
    )
    failed = repo.transition_run("dispatch-fail", FlowRunStatus.FAILED)
    assert failed.status == FlowRunStatus.FAILED
    assert failed.diagnostics[0]["code"] == "run_dispatch_failed"
