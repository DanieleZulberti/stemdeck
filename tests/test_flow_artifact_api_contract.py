from pathlib import Path


def test_artifact_gc_api_is_safe_by_default_and_registered():
    root = Path(__file__).resolve().parents[1]
    api = (root / "app/api/flow_artifacts.py").read_text(encoding="utf-8")
    router = (root / "app/api/router.py").read_text(encoding="utf-8")
    assert 'prefix="/flow-artifacts"' in api
    assert 'dry_run: bool = True' in api
    assert 'min_age_seconds: float = Field(default=86400.0' in api
    assert 'cache_max_idle_seconds' in api
    assert 'flow_artifacts_router' in router


def test_editor_exposes_retention_without_reintroducing_expose_publication():
    root = Path(__file__).resolve().parents[1]
    editor = (root / "static/js/flowEditor.js").read_text(encoding="utf-8")
    assert 'data-node-output-retain' in editor
    assert 'transient' in editor and 'cache' in editor and 'project' in editor
    assert 'data-output-field="publish"' in editor
