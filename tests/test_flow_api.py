from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import flows as flows_api
from app.flow.persistence import FlowRepository
from app.flow.schema import FlowDocument


def _client(tmp_path, monkeypatch) -> TestClient:
    repository = FlowRepository(tmp_path / "flows.sqlite3")
    monkeypatch.setattr(flows_api, "_flow_repository", lambda: repository)
    app = FastAPI()
    app.include_router(flows_api.router, prefix="/api")
    return TestClient(app)


def _draft() -> FlowDocument:
    return FlowDocument.model_validate(
        {
            "name": "Draft",
            "nodes": {"unfinished": {"inputs": {"audio": None}}},
            "outputs": {"preview": {}},
            "editor": {"nodes": {"unfinished": {"x": 1, "y": 2}}},
        }
    )


def test_flow_crud_accepts_invalid_drafts_and_owns_revision(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post(
        "/api/flows",
        json={"document": _draft().model_dump(mode="json")},
    )
    assert response.status_code == 201
    created = response.json()
    flow_id = created["flow_id"]
    assert created["revision"] == 1

    document = created["document"]
    document["name"] = "Renamed"
    response = client.put(
        f"/api/flows/{flow_id}",
        json={"document": document, "expected_revision": 1},
    )
    assert response.status_code == 200
    assert response.json()["revision"] == 2

    response = client.put(
        f"/api/flows/{flow_id}",
        json={"document": document, "expected_revision": 1},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "flow_revision_conflict"


def test_yaml_export_import_creates_independent_identity(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    created = client.post(
        "/api/flows",
        json={"document": _draft().model_dump(mode="json")},
    ).json()

    exported = client.get(f"/api/flows/{created['flow_id']}/export")
    assert exported.status_code == 200

    imported = client.post(
        "/api/flows/import",
        json={"yaml": exported.text, "name": "Imported"},
    )
    assert imported.status_code == 201
    payload = imported.json()
    assert payload["flow_id"] != created["flow_id"]
    assert payload["revision"] == 1
    assert payload["name"] == "Imported"
