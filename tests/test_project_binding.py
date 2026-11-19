from __future__ import annotations

import json

from app.flow.persistence import FlowRepository
from app.flow.project_repository import ProjectRepository
from app.flow.project_service import ProjectService
from app.flow.projects import StudioBindingStatus
from app.flow.run_repository import FlowRunRepository
from app.flow.runs import FlowRunStatus
from app.flow.schema import FlowDefinition, FlowDocument


def _document(name="Template") -> FlowDocument:
    return FlowDocument.model_validate(
        {
            "name": name,
            "inputs": {"song": {"type": "audio", "semantic_type": "mixture"}},
            "nodes": {},
            "outputs": {"vocals": {"source": "inputs.song", "publish": True}},
            "editor": {"viewport": {"scroll_left": 42}},
        }
    )


def _definition(flow_id: str, revision: int = 1) -> FlowDefinition:
    return FlowDefinition.model_validate(
        {
            "flow_id": flow_id,
            "name": "Project flow",
            "revision": revision,
            "inputs": {"song": {"type": "audio", "semantic_type": "mixture"}},
            "nodes": {},
            "outputs": {
                "vocals": {"source": "inputs.song", "publish": True},
                "private": {"source": "inputs.song", "publish": False},
            },
        }
    )


def _write_output(runs_root, run_id: str, output_id: str, *, artifact_id="a1", duration=10.0):
    output_dir = runs_root / run_id / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    audio = output_dir / f"{output_id}.wav"
    audio.write_bytes(b"audio")
    artifact = {
        "id": artifact_id,
        "path": str(audio),
        "semantic_type": "vocals",
        "sha256": "abc",
        "artifact_type": "audio",
        "duration_seconds": duration,
    }
    (output_dir / f"{output_id}.artifact.json").write_text(json.dumps(artifact))
    return artifact


def test_project_flow_is_independent_snapshot(tmp_path):
    db = tmp_path / "flow.sqlite3"
    flows = FlowRepository(db)
    projects = ProjectRepository(db)
    service = ProjectService(
        flow_repository=flows,
        run_repository=FlowRunRepository(db),
        project_repository=projects,
        runs_root=tmp_path / "runs",
    )
    source = flows.create(_document())
    project = service.create_project("Song A")

    ownership = service.copy_flow_to_project(project.project_id, source.flow_id)
    project_flow = flows.get(ownership.flow_id)

    assert project_flow.kind == "project"
    assert project_flow.flow_id != source.flow_id
    assert project_flow.revision == 1
    assert ownership.source_flow_id == source.flow_id
    assert ownership.source_flow_revision == source.revision
    assert project_flow.document.editor.viewport["scroll_left"] == 42

    changed = source.document.model_copy(update={"name": "Template changed"}, deep=True)
    flows.update(source.flow_id, changed, expected_revision=source.revision)
    assert flows.get(project_flow.flow_id).name == "Template"


def test_apply_creates_stable_binding_only_for_published_available_audio(tmp_path):
    db = tmp_path / "flow.sqlite3"
    runs_root = tmp_path / "runs"
    flows = FlowRepository(db)
    runs = FlowRunRepository(db)
    projects = ProjectRepository(db)
    service = ProjectService(
        flow_repository=flows,
        run_repository=runs,
        project_repository=projects,
        runs_root=runs_root,
    )
    source = flows.create(_document())
    project = service.create_project("Song A")
    ownership = service.copy_flow_to_project(project.project_id, source.flow_id)

    definition = _definition(ownership.flow_id)
    run = runs.create_run(definition, run_id="run-1")
    runs.transition_run(run.run_id, FlowRunStatus.RUNNING)
    runs.transition_run(run.run_id, FlowRunStatus.SUCCEEDED)
    _write_output(runs_root, run.run_id, "vocals")
    _write_output(runs_root, run.run_id, "private")

    result = service.apply_run(project.project_id, run.run_id)
    assert [item.flow_output_id for item in result.created] == ["vocals"]
    assert result.created[0].status == StudioBindingStatus.MANAGED
    assert result.created[0].flow_id == ownership.flow_id
    assert result.created[0].current_run_id == "run-1"
    assert {item["reason"] for item in result.skipped} == {"not_published"}

    # Reapplying the exact same immutable Run is harmless and preserves identity.
    same = service.apply_run(project.project_id, run.run_id)
    assert same.created == ()
    assert len(same.existing) == 1
    assert same.existing[0].track_id == result.created[0].track_id

    retry = runs.create_run(definition, run_id="run-2", retry_of_run_id="run-1")
    runs.transition_run(retry.run_id, FlowRunStatus.RUNNING)
    runs.transition_run(retry.run_id, FlowRunStatus.SUCCEEDED)
    _write_output(runs_root, retry.run_id, "vocals", artifact_id="a2", duration=10.02)
    rerun = service.apply_run(project.project_id, retry.run_id)
    assert rerun.created == ()
    assert rerun.existing == ()
    assert len(rerun.updated) == 1
    update = rerun.updated[0]
    assert update["binding"].binding_id == result.created[0].binding_id
    assert update["binding"].track_id == result.created[0].track_id
    assert update["binding"].current_run_id == "run-2"
    assert update["binding"].artifact["id"] == "a2"
    assert update["temporal_compatibility"]["compatible"] is True
    assert projects.get_binding(project.project_id, ownership.flow_id, "vocals").current_run_id == "run-2"


def test_binding_can_be_detached_without_deleting_track_or_artifact_reference(tmp_path):
    db = tmp_path / "flow.sqlite3"
    runs_root = tmp_path / "runs"
    flows = FlowRepository(db)
    runs = FlowRunRepository(db)
    projects = ProjectRepository(db)
    service = ProjectService(
        flow_repository=flows,
        run_repository=runs,
        project_repository=projects,
        runs_root=runs_root,
    )
    source = flows.create(_document())
    project = service.create_project("Song A")
    ownership = service.copy_flow_to_project(project.project_id, source.flow_id)
    run = runs.create_run(_definition(ownership.flow_id), run_id="run-1")
    runs.transition_run(run.run_id, FlowRunStatus.RUNNING)
    runs.transition_run(run.run_id, FlowRunStatus.SUCCEEDED)
    artifact = _write_output(runs_root, run.run_id, "vocals")
    binding = service.apply_run(project.project_id, run.run_id).created[0]

    detached = service.detach_binding(project.project_id, binding.binding_id)
    assert detached.status == StudioBindingStatus.DETACHED
    assert detached.track_id == binding.track_id
    assert detached.artifact["path"] == artifact["path"]


def test_project_delete_removes_owned_project_flows(tmp_path):
    db = tmp_path / "flow.sqlite3"
    flows = FlowRepository(db)
    projects = ProjectRepository(db)
    service = ProjectService(
        flow_repository=flows,
        run_repository=FlowRunRepository(db),
        project_repository=projects,
        runs_root=tmp_path / "runs",
    )
    source = flows.create(_document())
    project = service.create_project("Disposable")
    ownership = service.copy_flow_to_project(project.project_id, source.flow_id)

    service.delete_project(project.project_id)

    assert all(item.project_id != project.project_id for item in projects.list_projects())
    try:
        flows.get(ownership.flow_id)
    except Exception:
        pass
    else:
        raise AssertionError("Project Flow row survived Project deletion")


def test_binding_detach_is_scoped_to_project(tmp_path):
    db = tmp_path / "flow.sqlite3"
    runs_root = tmp_path / "runs"
    flows = FlowRepository(db)
    runs = FlowRunRepository(db)
    projects = ProjectRepository(db)
    service = ProjectService(
        flow_repository=flows,
        run_repository=runs,
        project_repository=projects,
        runs_root=runs_root,
    )
    source = flows.create(_document())
    project = service.create_project("A")
    other = service.create_project("B")
    ownership = service.copy_flow_to_project(project.project_id, source.flow_id)
    run = runs.create_run(_definition(ownership.flow_id), run_id="run-1")
    runs.transition_run(run.run_id, FlowRunStatus.RUNNING)
    runs.transition_run(run.run_id, FlowRunStatus.SUCCEEDED)
    _write_output(runs_root, run.run_id, "vocals")
    binding = service.apply_run(project.project_id, run.run_id).created[0]

    try:
        service.detach_binding(other.project_id, binding.binding_id)
    except Exception:
        pass
    else:
        raise AssertionError("Binding from another Project was detached")
    assert projects.get_binding(project.project_id, ownership.flow_id, "vocals").status == StudioBindingStatus.MANAGED


def test_rerun_marks_duration_change_as_temporally_incompatible_but_updates_media(tmp_path):
    db = tmp_path / "flow.sqlite3"
    runs_root = tmp_path / "runs"
    flows = FlowRepository(db)
    runs = FlowRunRepository(db)
    projects = ProjectRepository(db)
    service = ProjectService(flow_repository=flows, run_repository=runs, project_repository=projects, runs_root=runs_root)
    source = flows.create(_document())
    project = service.create_project("Song A")
    ownership = service.copy_flow_to_project(project.project_id, source.flow_id)
    definition = _definition(ownership.flow_id)

    first = runs.create_run(definition, run_id="run-a")
    runs.transition_run(first.run_id, FlowRunStatus.RUNNING)
    runs.transition_run(first.run_id, FlowRunStatus.SUCCEEDED)
    _write_output(runs_root, first.run_id, "vocals", artifact_id="old", duration=10.0)
    original = service.apply_run(project.project_id, first.run_id).created[0]

    second = runs.create_run(definition, run_id="run-b", retry_of_run_id=first.run_id)
    runs.transition_run(second.run_id, FlowRunStatus.RUNNING)
    runs.transition_run(second.run_id, FlowRunStatus.SUCCEEDED)
    _write_output(runs_root, second.run_id, "vocals", artifact_id="new", duration=11.0)
    result = service.apply_run(project.project_id, second.run_id)

    assert result.updated[0]["binding"].track_id == original.track_id
    assert result.updated[0]["binding"].artifact["id"] == "new"
    assert result.updated[0]["temporal_compatibility"]["compatible"] is False
    assert result.updated[0]["temporal_compatibility"]["reason"] == "duration_changed"


def test_removed_or_unpublished_output_is_detached_not_deleted(tmp_path):
    db = tmp_path / "flow.sqlite3"
    runs_root = tmp_path / "runs"
    flows = FlowRepository(db)
    runs = FlowRunRepository(db)
    projects = ProjectRepository(db)
    service = ProjectService(flow_repository=flows, run_repository=runs, project_repository=projects, runs_root=runs_root)
    source = flows.create(_document())
    project = service.create_project("Song A")
    ownership = service.copy_flow_to_project(project.project_id, source.flow_id)

    first_def = _definition(ownership.flow_id)
    first = runs.create_run(first_def, run_id="run-a")
    runs.transition_run(first.run_id, FlowRunStatus.RUNNING)
    runs.transition_run(first.run_id, FlowRunStatus.SUCCEEDED)
    _write_output(runs_root, first.run_id, "vocals")
    binding = service.apply_run(project.project_id, first.run_id).created[0]

    without_vocals = first_def.model_copy(update={"revision": 2, "outputs": {}})
    second = runs.create_run(without_vocals, run_id="run-b")
    runs.transition_run(second.run_id, FlowRunStatus.RUNNING)
    runs.transition_run(second.run_id, FlowRunStatus.SUCCEEDED)
    result = service.apply_run(project.project_id, second.run_id)

    assert len(result.detached) == 1
    assert result.detached[0].binding_id == binding.binding_id
    assert result.detached[0].track_id == binding.track_id
    persisted = projects.get_binding(project.project_id, ownership.flow_id, "vocals")
    assert persisted is not None
    assert persisted.status == StudioBindingStatus.DETACHED
    assert persisted.current_run_id == first.run_id


def test_partial_rerun_does_not_detach_published_output_when_artifact_is_missing(tmp_path):
    db = tmp_path / "flow.sqlite3"
    runs_root = tmp_path / "runs"
    flows = FlowRepository(db)
    runs = FlowRunRepository(db)
    projects = ProjectRepository(db)
    service = ProjectService(flow_repository=flows, run_repository=runs, project_repository=projects, runs_root=runs_root)
    source = flows.create(_document())
    project = service.create_project("Song A")
    ownership = service.copy_flow_to_project(project.project_id, source.flow_id)
    definition = _definition(ownership.flow_id)

    first = runs.create_run(definition, run_id="run-a")
    runs.transition_run(first.run_id, FlowRunStatus.RUNNING)
    runs.transition_run(first.run_id, FlowRunStatus.SUCCEEDED)
    _write_output(runs_root, first.run_id, "vocals")
    old = service.apply_run(project.project_id, first.run_id).created[0]

    partial = runs.create_run(definition, run_id="run-b")
    runs.transition_run(partial.run_id, FlowRunStatus.RUNNING)
    runs.transition_run(partial.run_id, FlowRunStatus.PARTIALLY_FAILED)
    result = service.apply_run(project.project_id, partial.run_id)

    assert result.detached == ()
    assert any(item["output_id"] == "vocals" and item["reason"] == "artifact_unavailable" for item in result.skipped)
    persisted = projects.get_binding(project.project_id, ownership.flow_id, "vocals")
    assert persisted.status == StudioBindingStatus.MANAGED
    assert persisted.current_run_id == old.current_run_id
