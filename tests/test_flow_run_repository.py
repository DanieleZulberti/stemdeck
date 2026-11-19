from __future__ import annotations

import pytest

from app.flow.run_repository import FlowRunRepository, RunCancellationToken
from app.flow.runs import (
    FlowRunCancelledError,
    FlowRunStatus,
    InvalidRunTransitionError,
    NodeRunStatus,
)
from app.flow.schema import FlowDefinition


def _definition(revision: int = 4) -> FlowDefinition:
    return FlowDefinition.model_validate(
        {
            "flow_id": "flow-1",
            "name": "Run me",
            "revision": revision,
            "inputs": {"song": {"semantic_type": "mixture"}},
            "nodes": {
                "a": {"definition_id": "split", "inputs": {"audio": "inputs.song"}},
                "b": {"definition_id": "split", "inputs": {"audio": "a.vocals"}},
            },
            "outputs": {"vocals": {"source": "b.vocals"}},
        }
    )


def test_run_snapshot_is_immutable_and_node_runs_are_created(tmp_path):
    repository = FlowRunRepository(tmp_path / "flows.sqlite3")
    source = _definition()
    created = repository.create_run(source)

    changed = source.model_copy(update={"name": "Edited later", "revision": 5})

    loaded = repository.get_run(created.run_id)
    assert loaded.definition.name == "Run me"
    assert loaded.definition.revision == 4
    assert changed.revision == 5
    assert [node.node_id for node in repository.list_node_runs(created.run_id)] == ["a", "b"]


def test_lifecycle_progress_cache_and_version(tmp_path):
    repository = FlowRunRepository(tmp_path / "flows.sqlite3")
    run = repository.create_run(_definition())
    initial_version = run.version

    running = repository.transition_run(run.run_id, FlowRunStatus.RUNNING)
    assert running.version == initial_version + 1

    node = repository.transition_node(run.run_id, "a", NodeRunStatus.RUNNING)
    assert node.started_at is not None
    node = repository.update_node_progress(run.run_id, "a", 0.5, "halfway")
    assert node.progress == 0.5
    node = repository.transition_node(run.run_id, "a", NodeRunStatus.SUCCEEDED)
    assert node.progress == 1.0

    cached = repository.transition_node(run.run_id, "b", NodeRunStatus.CACHED)
    assert cached.cache_hit
    assert cached.progress == 1.0

    done = repository.transition_run(run.run_id, FlowRunStatus.SUCCEEDED)
    assert done.finished_at is not None
    assert done.version > running.version

    with pytest.raises(InvalidRunTransitionError):
        repository.transition_run(run.run_id, FlowRunStatus.RUNNING)


def test_graph_native_cancellation_token(tmp_path):
    repository = FlowRunRepository(tmp_path / "flows.sqlite3")
    run = repository.create_run(_definition())
    token = RunCancellationToken(repository, run.run_id)

    assert not token.is_cancel_requested
    repository.request_cancel(run.run_id)
    assert token.is_cancel_requested
    with pytest.raises(FlowRunCancelledError):
        token.raise_if_cancelled()
