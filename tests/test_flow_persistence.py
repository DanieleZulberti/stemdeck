from __future__ import annotations

import pytest

from app.flow.persistence import FlowRepository, FlowRevisionConflictError
from app.flow.schema import FlowDocument


def _draft(name: str = "Draft") -> FlowDocument:
    return FlowDocument.model_validate(
        {
            "name": name,
            "nodes": {"unfinished": {"inputs": {"audio": None}}},
            "outputs": {"preview": {}},
            "editor": {"nodes": {"unfinished": {"x": 25, "y": 50}}},
        }
    )


def test_repository_persists_invalid_editor_document(tmp_path):
    repository = FlowRepository(tmp_path / "flows.sqlite3")

    created = repository.create(_draft())
    loaded = repository.get(created.flow_id)

    assert loaded.revision == 1
    assert loaded.document.flow_id == created.flow_id
    assert loaded.document.nodes["unfinished"].definition_id is None
    assert loaded.document.outputs["preview"].source is None
    assert loaded.document.editor.nodes["unfinished"].x == 25


def test_update_owns_revision_and_detects_stale_writes(tmp_path):
    repository = FlowRepository(tmp_path / "flows.sqlite3")
    created = repository.create(_draft())

    incoming = created.document.model_copy(update={"name": "Renamed", "revision": 999})
    updated = repository.update(
        created.flow_id,
        incoming,
        expected_revision=created.revision,
    )

    assert updated.revision == 2
    assert updated.document.revision == 2
    assert updated.name == "Renamed"

    with pytest.raises(FlowRevisionConflictError):
        repository.update(
            created.flow_id,
            incoming,
            expected_revision=created.revision,
        )


def test_duplicate_gets_independent_identity_and_revision(tmp_path):
    repository = FlowRepository(tmp_path / "flows.sqlite3")
    created = repository.create(_draft("Original"))

    duplicate = repository.duplicate(created.flow_id)

    assert duplicate.flow_id != created.flow_id
    assert duplicate.revision == 1
    assert duplicate.name == "Original Copy"
    assert duplicate.document.nodes == created.document.nodes
