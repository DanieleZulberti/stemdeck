from pathlib import Path

ROOT = Path.cwd() if (Path.cwd() / "app").exists() else Path(__file__).resolve().parents[1]


def test_m97_direct_flow_studio_api_is_registered_without_project_ui_dependency():
    router = (ROOT / "app/api/router.py").read_text(encoding="utf-8")
    api = (ROOT / "app/api/flow_studio.py").read_text(encoding="utf-8")
    service = (ROOT / "app/flow/flow_studio_service.py").read_text(encoding="utf-8")

    assert "flow_studio_router" in router
    assert 'prefix="/flow-studio"' in api
    assert '@router.post("/apply"' in api
    assert '@router.get("/bindings")' in api
    assert '_FLOW_LIBRARY_SCOPE_ID = "__flow_library__"' in service
    assert "attach_project_flow" in service


def test_m97_project_storage_is_compatibility_detail_not_editor_concept():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    editor = (ROOT / "static/js/flowEditor.js").read_text(encoding="utf-8")

    assert 'id="flowProjectSelect"' not in html
    assert 'id="flowProjectList"' not in html
    assert 'id="flowCopyToProjectBtn"' not in html
    assert '/flow-studio/apply' in editor
