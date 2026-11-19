from __future__ import annotations

import json
from pathlib import Path

from app.flow.artifacts import AudioArtifact, save_artifact_manifest
from app.flow.cache import ArtifactCache
from app.flow.executor import FlowExecutor
from app.flow.persistence import FlowRepository
from app.flow.project_repository import ProjectRepository
from app.flow.project_service import ProjectService
from app.flow.lifecycle import ArtifactLifecycleService
from app.flow.run_repository import FlowRunRepository
from app.flow.runs import FlowRunStatus
from app.flow.schema import FlowDefinition, FlowDocument, RetentionPolicy


def _document(name="Template") -> FlowDocument:
    return FlowDocument.model_validate({
        "name": name,
        "inputs": {"song": {"type": "audio", "semantic_type": "mixture"}},
        "nodes": {},
        "outputs": {},
    })


def _write_artifact(path: Path, manifest: Path, *, artifact_id: str, semantic="mixture") -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"artifact-bytes")
    artifact = AudioArtifact(
        id=artifact_id,
        path=path.resolve(),
        semantic_type=semantic,
        sha256="abc",
    )
    save_artifact_manifest(artifact, manifest)
    return artifact.to_json_dict()


def _terminal_run(runs: FlowRunRepository, definition: FlowDefinition, run_id: str):
    run = runs.create_run(definition, run_id=run_id)
    runs.transition_run(run_id, FlowRunStatus.RUNNING)
    runs.transition_run(run_id, FlowRunStatus.SUCCEEDED)
    return runs.get_run(run_id)


def test_studio_binding_protects_output_even_when_detached(tmp_path):
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
    project = service.create_project("Song")
    ownership = service.copy_flow_to_project(project.project_id, source.flow_id)
    definition = FlowDefinition.model_validate({
        "flow_id": ownership.flow_id,
        "name": "Project flow",
        "inputs": {"song": {"semantic_type": "mixture"}},
        "nodes": {},
        "outputs": {"mix": {"source": "inputs.song", "publish": True}},
    })
    _terminal_run(runs, definition, "run-1")
    output = runs_root / "run-1" / "outputs" / "mix.wav"
    artifact = _write_artifact(
        output,
        runs_root / "run-1" / "outputs" / "mix.artifact.json",
        artifact_id="flow-output",
    )
    binding = projects.create_binding(
        project_id=project.project_id,
        flow_id=ownership.flow_id,
        output_id="mix",
        run_id="run-1",
        artifact=artifact,
    )
    projects.set_binding_status(binding.binding_id, binding.status.DETACHED)

    lifecycle = ArtifactLifecycleService(
        run_repository=runs,
        project_repository=projects,
        runs_root=runs_root,
        cache=ArtifactCache(tmp_path / "cache"),
    )
    report = lifecycle.collect(dry_run=False, min_age_seconds=0)
    item = next(item for item in report.items if item.scope == "flow_output")
    assert item.action == "protected"
    assert item.reason == "studio_binding_reference"
    assert output.is_file()


def test_project_retention_survives_gc_until_project_is_deleted(tmp_path):
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
    project = service.create_project("Song")
    ownership = service.copy_flow_to_project(project.project_id, source.flow_id)
    definition = FlowDefinition.model_validate({
        "flow_id": ownership.flow_id,
        "name": "Project flow",
        "inputs": {"song": {"semantic_type": "mixture"}},
        "nodes": {
            "keep": {
                "model": "test",
                "inputs": {"audio": "inputs.song"},
                "outputs": {"audio": {"retain": "project"}},
            }
        },
        "outputs": {"result": {"source": "keep.audio", "publish": True}},
    })
    _terminal_run(runs, definition, "run-project")
    node_file = runs_root / "run-project" / "nodes" / "keep" / "outputs" / "audio.wav"
    _write_artifact(
        node_file,
        runs_root / "run-project" / "nodes" / "keep" / "audio.artifact.json",
        artifact_id="node-output",
    )
    flow_file = runs_root / "run-project" / "outputs" / "result.wav"
    _write_artifact(
        flow_file,
        runs_root / "run-project" / "outputs" / "result.artifact.json",
        artifact_id="flow-output",
    )
    lifecycle = ArtifactLifecycleService(
        run_repository=runs,
        project_repository=projects,
        runs_root=runs_root,
        cache=ArtifactCache(tmp_path / "cache"),
    )

    protected = lifecycle.collect(dry_run=False, min_age_seconds=0)
    assert node_file.is_file() and flow_file.is_file()
    assert {item.reason for item in protected.items} == {"project_flow_retention"}

    service.delete_project(project.project_id)
    collected = lifecycle.collect(dry_run=False, min_age_seconds=0)
    assert not node_file.exists() and not flow_file.exists()
    assert any(item.action == "deleted" for item in collected.items)


def test_gc_never_deletes_retry_input_snapshots(tmp_path):
    db = tmp_path / "flow.sqlite3"
    runs_root = tmp_path / "runs"
    runs = FlowRunRepository(db)
    definition = FlowDefinition.model_validate({
        "flow_id": "user-flow",
        "name": "Flow",
        "inputs": {"song": {"semantic_type": "mixture"}},
        "nodes": {},
        "outputs": {"result": {"source": "inputs.song"}},
    })
    _terminal_run(runs, definition, "run-input")
    input_file = runs_root / "run-input" / "inputs" / "song.wav"
    _write_artifact(
        input_file,
        runs_root / "run-input" / "inputs" / "song.artifact.json",
        artifact_id="input",
    )
    output_file = runs_root / "run-input" / "outputs" / "result.wav"
    _write_artifact(
        output_file,
        runs_root / "run-input" / "outputs" / "result.artifact.json",
        artifact_id="output",
    )
    lifecycle = ArtifactLifecycleService(
        run_repository=runs,
        project_repository=ProjectRepository(db),
        runs_root=runs_root,
        cache=ArtifactCache(tmp_path / "cache"),
    )
    lifecycle.collect(dry_run=False, min_age_seconds=0)
    assert input_file.is_file()
    assert (runs_root / "run-input" / "inputs" / "song.artifact.json").is_file()
    assert not output_file.exists()


def test_transient_node_disables_atomic_node_cache():
    flow = FlowDefinition.model_validate({
        "name": "Transient",
        "inputs": {"song": {"semantic_type": "mixture"}},
        "nodes": {
            "split": {
                "model": "test",
                "inputs": {"audio": "inputs.song"},
                "outputs": {
                    "a": {"retain": "cache"},
                    "b": {"retain": "transient"},
                },
            }
        },
    })
    assert FlowExecutor._node_cache_allowed(flow, "split", {"a", "b"}) is False
    assert flow.nodes["split"].outputs["b"].retain == RetentionPolicy.TRANSIENT


def test_cache_gc_requires_explicit_idle_policy(tmp_path):
    cache = ArtifactCache(tmp_path / "cache")
    source = tmp_path / "a.wav"
    source.write_bytes(b"audio")
    cache.store("key", {"audio": source}, metadata={})

    disabled = cache.collect(dry_run=False, max_idle_seconds=None)
    assert disabled["enabled"] is False
    assert cache.entry_dir("key").is_dir()

    preview = cache.collect(dry_run=True, max_idle_seconds=0)
    assert preview["entries"][0]["action"] == "would_delete"
    assert cache.entry_dir("key").is_dir()

    deleted = cache.collect(dry_run=False, max_idle_seconds=0)
    assert deleted["entries"][0]["action"] == "deleted"
    assert not cache.entry_dir("key").exists()
