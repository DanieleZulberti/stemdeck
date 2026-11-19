from pathlib import Path

ROOT = Path.cwd() if (Path.cwd() / "app").exists() else Path(__file__).resolve().parents[1]


def test_project_and_apply_routes_are_isolated_from_legacy_jobs():
    router = (ROOT / "app" / "api" / "projects.py").read_text(encoding="utf-8")
    aggregate = (ROOT / "app" / "api" / "router.py").read_text(encoding="utf-8")
    assert 'APIRouter(prefix="/projects"' in router
    assert '"/{project_id}/flows"' in router
    assert '"/{project_id}/studio-bindings/apply"' in router
    assert 'router.include_router(projects_router)' in aggregate
    assert "app.core.models" not in router
    assert "Job" not in router


def test_output_media_route_serves_only_published_flow_outputs():
    api = (ROOT / "app" / "api" / "flow_runs.py").read_text(encoding="utf-8")
    assert '"/{run_id}/outputs/{output_id}/media"' in api
    assert "not definition.publish" in api
    assert "is_relative_to(root)" in api
