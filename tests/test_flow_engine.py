from __future__ import annotations

import json
from pathlib import Path

from audio_separator.separator.roformer.roformer_loader import RoformerLoader
from audio_separator.separator.uvr_lib_v5.roformer import mel_band_roformer

from app.core.models import Job
from app.flow.adapters import (
    AdapterExecutionRequest,
    AdapterPreflightIssue,
    AdapterRegistry,
    MockAdapter,
)
from app.flow.artifacts import AudioArtifact, hash_file
from app.flow.cache import ArtifactCache
from app.flow.capabilities import CapabilityChecker
from app.flow.defaults import build_default_model_registry
from app.flow.executor import FlowExecutor
from app.flow.io import load_flow
from app.flow.local_roformer_runner import (
    _install_mel_band_mask_estimator_workaround,
)
from app.flow.planner import FlowPlanner
from app.flow.registry import ModelRegistry
from app.flow.schema import FlowDefinition, ModelDefinition, PortDefinition
from app.flow.semantic_types import is_semantically_compatible
from app.flow.stemdeck_adapters import StemDeckDemucsAdapter, StemDeckVocalSplitAdapter
from app.flow.validation import FlowValidator

ROOT = Path(__file__).parents[1]


def _artifact(path: Path, semantic_type: str) -> AudioArtifact:
    digest = hash_file(path)
    return AudioArtifact(
        id=f"sha256:{digest}",
        path=path,
        semantic_type=semantic_type,
        sha256=digest,
    )


def test_semantic_type_inheritance():
    assert is_semantically_compatible("lead_vocals", "vocals")
    assert is_semantically_compatible("lead_guitar", "guitar")
    assert is_semantically_compatible("piano", "keys")
    assert not is_semantically_compatible("bass", "vocals")


def test_default_demo_flow_is_valid():
    flow = load_flow(ROOT / "docs" / "flows" / "demo-vocals.yaml")
    result = FlowValidator(build_default_model_registry()).validate(flow)

    assert result.valid, [issue.message for issue in result.issues]
    assert FlowPlanner().plan(flow).stages[0].nodes == ("primary",)
    assert FlowPlanner().plan(flow).stages[1].nodes == ("vocal_split",)


def test_default_guitar_branch_flow_is_valid():
    flow = load_flow(ROOT / "docs" / "flows" / "demo-vocals-guitar.yaml")
    result = FlowValidator(build_default_model_registry()).validate(flow)

    assert result.valid, [issue.message for issue in result.issues]
    plan = FlowPlanner().plan(flow)
    assert plan.stages[0].nodes == ("primary",)
    assert plan.stages[1].nodes == ("guitar_split", "vocal_split")


def test_planner_exposes_parallel_branches():
    models = ModelRegistry()
    models.register(
        ModelDefinition(
            id="primary",
            adapter="mock",
            inputs={"audio": PortDefinition(semantic_type="mixture")},
            outputs={
                "vocals": PortDefinition(semantic_type="vocals"),
                "guitar": PortDefinition(semantic_type="guitar"),
            },
        )
    )
    models.register(
        ModelDefinition(
            id="vocal-split",
            adapter="mock",
            inputs={"audio": PortDefinition(semantic_type="vocals")},
            outputs={"lead": PortDefinition(semantic_type="lead_vocals")},
        )
    )
    models.register(
        ModelDefinition(
            id="guitar-split",
            adapter="mock",
            inputs={"audio": PortDefinition(semantic_type="guitar")},
            outputs={"lead": PortDefinition(semantic_type="lead_guitar")},
        )
    )
    flow = FlowDefinition.model_validate(
        {
            "name": "parallel-test",
            "inputs": {"song": {"semantic_type": "mixture"}},
            "nodes": {
                "primary": {"model": "primary", "inputs": {"audio": "inputs.song"}},
                "vocals": {
                    "model": "vocal-split",
                    "inputs": {"audio": "primary.vocals"},
                },
                "guitar": {
                    "model": "guitar-split",
                    "inputs": {"audio": "primary.guitar"},
                },
            },
        }
    )

    validation = FlowValidator(models).validate(flow)
    assert validation.valid, [issue.message for issue in validation.issues]
    plan = FlowPlanner().plan(flow)
    assert plan.stages[0].nodes == ("primary",)
    assert plan.stages[1].nodes == ("guitar", "vocals")


def test_capability_checker_collects_adapter_preflight(monkeypatch):
    models = ModelRegistry()
    models.register(
        ModelDefinition(
            id="mock-model",
            adapter="mock",
            inputs={"audio": PortDefinition(semantic_type="mixture")},
            outputs={"audio": PortDefinition(semantic_type="mixture")},
        )
    )
    flow = FlowDefinition.model_validate(
        {
            "name": "preflight-test",
            "inputs": {"song": {"semantic_type": "mixture"}},
            "nodes": {
                "process": {
                    "model": "mock-model",
                    "inputs": {"audio": "inputs.song"},
                }
            },
        }
    )
    adapters = AdapterRegistry()
    adapters.register(MockAdapter())
    monkeypatch.setattr(
        MockAdapter,
        "preflight",
        lambda self, model: [AdapterPreflightIssue("error", "missing dependency")],
    )

    report = CapabilityChecker(models, adapters).check(flow)

    assert not report.runnable
    assert [(issue.node, issue.message) for issue in report.issues] == [
        ("process", "missing dependency")
    ]


def test_executor_cache_and_multiple_inputs(tmp_path: Path):
    vocals = tmp_path / "track.wav"
    guitar = tmp_path / "other" / "track.wav"
    guitar.parent.mkdir()
    vocals.write_bytes(b"vocals")
    guitar.write_bytes(b"guitar")

    models = ModelRegistry()
    models.register(
        ModelDefinition(
            id="vocal",
            adapter="mock",
            inputs={"audio": PortDefinition(semantic_type="vocals")},
            outputs={"lead": PortDefinition(semantic_type="lead_vocals")},
        )
    )
    models.register(
        ModelDefinition(
            id="guitar",
            adapter="mock",
            inputs={"audio": PortDefinition(semantic_type="guitar")},
            outputs={"lead": PortDefinition(semantic_type="lead_guitar")},
        )
    )
    flow = FlowDefinition.model_validate(
        {
            "name": "multitrack",
            "inputs": {
                "vocals": {"semantic_type": "vocals"},
                "guitar": {"semantic_type": "guitar"},
            },
            "nodes": {
                "vocal_split": {
                    "model": "vocal",
                    "inputs": {"audio": "inputs.vocals"},
                },
                "guitar_split": {
                    "model": "guitar",
                    "inputs": {"audio": "inputs.guitar"},
                },
            },
            "outputs": {
                "lead_vocals": {"source": "vocal_split.lead"},
                "lead_guitar": {"source": "guitar_split.lead"},
            },
        }
    )
    adapters = AdapterRegistry()
    adapters.register(MockAdapter())
    executor = FlowExecutor(
        models,
        adapters,
        ArtifactCache(tmp_path / "cache"),
        tmp_path / "runs",
    )

    first = executor.execute(
        flow,
        {"vocals": vocals, "guitar": guitar},
        run_id="one",
    )
    second = executor.execute(
        flow,
        {"vocals": vocals, "guitar": guitar},
        run_id="two",
    )

    assert (first.run_dir / "inputs" / "vocals.wav").is_file()
    assert (first.run_dir / "inputs" / "guitar.wav").is_file()
    assert second.cache_hits == {"vocal_split", "guitar_split"}

    first_manifest = json.loads(
        (first.run_dir / "run.json").read_text(encoding="utf-8")
    )
    second_manifest = json.loads(
        (second.run_dir / "run.json").read_text(encoding="utf-8")
    )
    assert "environment" in first_manifest
    assert (
        first_manifest["nodes"]["vocal_split"]["provenance_origin"]
        == "current_run"
    )
    assert (
        second_manifest["nodes"]["vocal_split"]["provenance_origin"]
        == "cache_manifest"
    )
    assert "produced_environment" in second_manifest["nodes"]["vocal_split"]


def test_roformer_workaround_propagates_mlp_expansion_factor(monkeypatch):
    captured: dict[str, int] = {}

    class FakeMaskEstimator:
        def __init__(
            self,
            dim,
            dim_inputs,
            depth,
            mlp_expansion_factor=4,
        ):
            captured["mlp_expansion_factor"] = mlp_expansion_factor

    def fake_create(self, config):
        mel_band_roformer.MaskEstimator(
            dim=384,
            dim_inputs=(10, 20),
            depth=2,
        )
        return object()

    monkeypatch.setattr(
        mel_band_roformer,
        "MaskEstimator",
        FakeMaskEstimator,
    )
    monkeypatch.setattr(
        RoformerLoader,
        "_create_mel_band_roformer",
        fake_create,
    )

    original_mask_estimator = mel_band_roformer.MaskEstimator

    _install_mel_band_mask_estimator_workaround()

    loader = object.__new__(RoformerLoader)
    RoformerLoader._create_mel_band_roformer(
        loader,
        {"mlp_expansion_factor": 2},
    )

    assert captured["mlp_expansion_factor"] == 2
    assert mel_band_roformer.MaskEstimator is original_mask_estimator


def test_native_demucs_adapter_wraps_existing_separator(tmp_path: Path, monkeypatch):
    source = tmp_path / "song.wav"
    source.write_bytes(b"RIFF")
    job = Job(id="flowdemucs001")
    model = ModelDefinition(
        id="demucs",
        adapter="stemdeck-demucs",
        inputs={"audio": PortDefinition(semantic_type="mixture")},
        outputs={
            "vocals": PortDefinition(semantic_type="vocals"),
            "guitar": PortDefinition(semantic_type="guitar"),
        },
    )

    def fake_separate(job_arg, source_arg, work_dir):
        assert job_arg is job
        assert source_arg == source
        root = work_dir / "fake"
        root.mkdir(parents=True)
        (root / "vocals.wav").write_bytes(b"vocals")
        (root / "guitar.wav").write_bytes(b"guitar")
        return root

    monkeypatch.setattr("app.flow.stemdeck_adapters.separate", fake_separate)
    adapter = StemDeckDemucsAdapter()
    outputs = adapter.execute(
        AdapterExecutionRequest(
            node_id="primary",
            model=model,
            inputs={"audio": _artifact(source, "mixture")},
            parameters={},
            output_dir=tmp_path / "node" / "outputs",
            context={"job": job},
        )
    )

    assert outputs["vocals"].read_bytes() == b"vocals"
    assert outputs["guitar"].read_bytes() == b"guitar"


def test_native_vocal_adapter_wraps_existing_split(tmp_path: Path, monkeypatch):
    source = tmp_path / "vocals.wav"
    source.write_bytes(b"RIFF")
    job = Job(id="flowvocal001")
    model = ModelDefinition(
        id="vocal",
        adapter="stemdeck-vocal-split",
        inputs={"audio": PortDefinition(semantic_type="vocals")},
        outputs={
            "lead": PortDefinition(semantic_type="lead_vocals"),
            "backing": PortDefinition(semantic_type="backing_vocals"),
        },
    )

    def fake_split(job_arg, stems_dir):
        assert job_arg is job
        assert (stems_dir / "vocals.wav").read_bytes() == b"RIFF"
        (stems_dir / "lead_vocals.wav").write_bytes(b"lead")
        (stems_dir / "backing_vocals.wav").write_bytes(b"backing")
        return ["lead_vocals", "backing_vocals"]

    monkeypatch.setattr("app.flow.stemdeck_adapters.split_vocals", fake_split)
    adapter = StemDeckVocalSplitAdapter()
    outputs = adapter.execute(
        AdapterExecutionRequest(
            node_id="vocal_split",
            model=model,
            inputs={"audio": _artifact(source, "vocals")},
            parameters={},
            output_dir=tmp_path / "node" / "outputs",
            context={"job": job},
        )
    )

    assert outputs["lead"].read_bytes() == b"lead"
    assert outputs["backing"].read_bytes() == b"backing"
