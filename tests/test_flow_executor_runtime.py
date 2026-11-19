from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from app.flow.adapters import AdapterExecutionRequest, AdapterRegistry, ModelAdapter
from app.flow.artifacts import AudioArtifact
from app.flow.cache import ArtifactCache
from app.flow.executor import FlowExecutor
from app.flow.registry import ModelRegistry
from app.flow.run_repository import FlowRunRepository
from app.flow.runs import FlowRunCancelledError, FlowRunStatus, NodeRunStatus
from app.flow.scheduler import ResourceClaim
from app.flow.schema import FlowDefinition, ModelDefinition, PortDefinition


class BranchAdapter(ModelAdapter):
    id = "branch"
    @property
    def version(self): return "1"
    def available(self): return True, None
    def resource_claim(self, model, parameters, context):
        return ResourceClaim("branch-test", 8)
    def execute(self, request: AdapterExecutionRequest):
        if request.model.runtime.get("fail"):
            raise RuntimeError("intentional failure")
        source = next(iter(request.inputs.values())).path
        request.output_dir.mkdir(parents=True, exist_ok=True)
        result = {}
        for port in request.model.outputs:
            target = request.output_dir / f"{port}.wav"
            shutil.copy2(source, target)
            result[port] = target
        if request.progress: request.progress(0.5, "half")
        return result


class SlowAdapter(ModelAdapter):
    id = "slow"
    @property
    def version(self): return "1"
    def available(self): return True, None
    def resource_claim(self, model, parameters, context): return ResourceClaim("slow", 1)
    def execute(self, request: AdapterExecutionRequest):
        for _ in range(200):
            if request.cancellation_requested and request.cancellation_requested():
                raise FlowRunCancelledError("cancelled")
            time.sleep(0.01)
        target = request.output_dir / "audio.wav"
        request.output_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(next(iter(request.inputs.values())).path, target)
        return {"audio": target}


def branch_setup(tmp_path: Path):
    models = ModelRegistry()
    models.register(ModelDefinition(id="good", adapter="branch", inputs={"audio": PortDefinition(semantic_type="mixture")}, outputs={"audio": PortDefinition(semantic_type="mixture")}))
    models.register(ModelDefinition(id="bad", adapter="branch", inputs={"audio": PortDefinition(semantic_type="mixture")}, outputs={"audio": PortDefinition(semantic_type="mixture")}, runtime={"fail": True}))
    models.register(ModelDefinition(id="child", adapter="branch", inputs={"audio": PortDefinition(semantic_type="mixture")}, outputs={"audio": PortDefinition(semantic_type="mixture")}))
    adapters=AdapterRegistry(); adapters.register(BranchAdapter())
    repo=FlowRunRepository(tmp_path/"flow.sqlite3")
    ex=FlowExecutor(models, adapters, ArtifactCache(tmp_path/"cache"), tmp_path/"runs", run_repository=repo, max_parallel_nodes=4)
    flow=FlowDefinition.model_validate({
      "flow_id":"f1", "name":"branches", "revision":3,
      "inputs":{"song":{"semantic_type":"mixture"}},
      "nodes":{
        "good":{"model":"good","inputs":{"audio":"inputs.song"}},
        "bad":{"model":"bad","inputs":{"audio":"inputs.song"}},
        "blocked":{"model":"child","inputs":{"audio":"bad.audio"}},
      },
      "outputs":{"result":{"source":"good.audio"}, "missing":{"source":"blocked.audio"}}
    })
    return ex, repo, flow


def test_failure_isolation_partial_output_and_blocked(tmp_path: Path):
    src=tmp_path/"in.wav"; src.write_bytes(b"audio")
    ex,repo,flow=branch_setup(tmp_path)
    result=ex.execute(flow,{"song":src})
    assert set(result.outputs)=={"result"}
    assert result.node_states=={"good":"COMPLETED","bad":"FAILED","blocked":"BLOCKED"}
    run=repo.get_run(result.run_id)
    assert run.status==FlowRunStatus.PARTIALLY_FAILED
    nodes={n.node_id:n for n in repo.list_node_runs(result.run_id)}
    assert nodes["good"].status==NodeRunStatus.SUCCEEDED
    assert nodes["bad"].status==NodeRunStatus.FAILED
    assert nodes["blocked"].status==NodeRunStatus.BLOCKED
    assert nodes["bad"].diagnostics[0]["source"]=="executor"


def test_retry_reuses_cache_and_links_history(tmp_path: Path):
    src=tmp_path/"in.wav"; src.write_bytes(b"audio")
    ex,repo,flow=branch_setup(tmp_path)
    first=ex.execute(flow,{"song":src})
    second=ex.retry(first.run_id,{"song":src})
    assert second.cache_hits=={"good"}
    second_record=repo.get_run(second.run_id)
    assert second_record.retry_of_run_id==first.run_id
    good=repo.get_node_run(second.run_id,"good")
    assert good.status==NodeRunStatus.CACHED and good.cache_hit


def test_cancellation_is_graph_native(tmp_path: Path):
    models=ModelRegistry(); models.register(ModelDefinition(id="slow-model", adapter="slow", inputs={"audio":PortDefinition(semantic_type="mixture")}, outputs={"audio":PortDefinition(semantic_type="mixture")}))
    adapters=AdapterRegistry(); adapters.register(SlowAdapter())
    repo=FlowRunRepository(tmp_path/"flow.sqlite3")
    ex=FlowExecutor(models, adapters, ArtifactCache(tmp_path/"cache"), tmp_path/"runs", run_repository=repo)
    flow=FlowDefinition.model_validate({"flow_id":"f2","name":"slow","inputs":{"song":{"semantic_type":"mixture"}},"nodes":{"slow":{"model":"slow-model","inputs":{"audio":"inputs.song"}}},"outputs":{"result":{"source":"slow.audio"}}})
    src=tmp_path/"in.wav"; src.write_bytes(b"audio")
    holder={}
    def run():
        try: ex.execute(flow,{"song":src},run_id="cancel-me")
        except Exception as e: holder["error"]=e
    t=threading.Thread(target=run); t.start()
    deadline=time.time()+3
    while time.time()<deadline:
        try:
            if repo.get_run("cancel-me").status==FlowRunStatus.RUNNING: break
        except Exception: pass
        time.sleep(0.01)
    repo.request_cancel("cancel-me")
    t.join(5)
    assert isinstance(holder.get("error"), FlowRunCancelledError)
    assert repo.get_run("cancel-me").status==FlowRunStatus.CANCELLED
    assert repo.get_node_run("cancel-me","slow").status==NodeRunStatus.CANCELLED
