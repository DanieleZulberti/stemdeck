from __future__ import annotations

from functools import lru_cache
from typing import Literal

import yaml
from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, ValidationError

from app.core.config import FLOW_DB_PATH
from app.flow.adapters import AdapterRegistry
from app.flow.capabilities import CapabilityChecker
from app.flow.compiler import FlowCompiler
from app.flow.defaults import (
    build_default_adapter_registry,
    build_default_node_definition_registry,
)
from app.flow.diagnostics import Diagnostic
from app.flow.persistence import (
    FlowNotFoundError,
    FlowPersistenceError,
    FlowRepository,
    FlowRevisionConflictError,
    ReadOnlyFlowError,
)
from app.flow.schema import FlowDocument, NodeDefinition
from app.flow.validation import FlowValidator

router = APIRouter(tags=["flows"])


class FlowCreateRequest(BaseModel):
    document: FlowDocument


class FlowUpdateRequest(BaseModel):
    document: FlowDocument
    expected_revision: int = Field(ge=1)


class FlowDuplicateRequest(BaseModel):
    name: str | None = None


class FlowImportRequest(BaseModel):
    yaml: str
    name: str | None = None


@lru_cache(maxsize=1)
def _flow_repository() -> FlowRepository:
    return FlowRepository(FLOW_DB_PATH)


def _runtime_adapters() -> AdapterRegistry:
    try:
        return build_default_adapter_registry()
    except ImportError:
        # Capability is environment-dependent. Static flow editing/compilation
        # must remain available even when an execution dependency is absent.
        return AdapterRegistry()


def _diagnostics(items: list[Diagnostic]) -> list[dict[str, str | None]]:
    return [item.to_dict() for item in items]


def _public_node_definition(definition: NodeDefinition, capability: dict) -> dict:
    """Return the backend-owned node contract consumed by a generic editor.

    Adapter IDs, model paths, runtime environment variables and other execution
    implementation details intentionally remain private to the backend.
    """
    return {
        "id": definition.id,
        "label": definition.label or definition.id,
        "description": definition.description,
        "category": definition.category or "processing",
        "inputs": {
            name: port.model_dump(mode="json")
            for name, port in definition.inputs.items()
        },
        "outputs": {
            name: port.model_dump(mode="json")
            for name, port in definition.outputs.items()
        },
        "parameters": {
            name: parameter.model_dump(mode="json")
            for name, parameter in definition.parameters.items()
        },
        "capability": capability,
    }


def _record_payload(record, *, include_document: bool = True) -> dict:
    payload = record.metadata()
    if include_document:
        payload["document"] = record.document.model_dump(mode="json")
    return payload


def _raise_persistence_http(exc: FlowPersistenceError) -> None:
    if isinstance(exc, FlowNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, FlowRevisionConflictError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "flow_revision_conflict",
                "message": str(exc),
                "expected_revision": exc.expected,
                "actual_revision": exc.actual,
            },
        ) from exc
    if isinstance(exc, ReadOnlyFlowError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/node-definitions")
def get_node_definitions() -> dict:
    models = build_default_node_definition_registry()
    adapters = _runtime_adapters()
    checker = CapabilityChecker(models, adapters)

    definitions = []
    for definition in models.all():
        report = checker.check_definition(definition)
        definitions.append(
            _public_node_definition(
                definition,
                {
                    "available": report.available,
                    "diagnostics": _diagnostics(report.issues),
                },
            )
        )
    return {"definitions": definitions}


# ---------------------------------------------------------------------------
# Editor document persistence
# ---------------------------------------------------------------------------


@router.get("/flows")
def list_flows(kind: Literal["user", "project"] | None = Query(default=None)) -> dict:
    records = _flow_repository().list(kind=kind)
    return {"flows": [_record_payload(record, include_document=False) for record in records]}


@router.post("/flows", status_code=status.HTTP_201_CREATED)
def create_flow(request: FlowCreateRequest) -> dict:
    try:
        record = _flow_repository().create(request.document, kind="user")
    except FlowPersistenceError as exc:
        _raise_persistence_http(exc)
    return _record_payload(record)


@router.get("/flows/{flow_id}")
def get_flow(flow_id: str) -> dict:
    try:
        record = _flow_repository().get(flow_id)
    except FlowPersistenceError as exc:
        _raise_persistence_http(exc)
    return _record_payload(record)


@router.put("/flows/{flow_id}")
def update_flow(flow_id: str, request: FlowUpdateRequest) -> dict:
    try:
        record = _flow_repository().update(
            flow_id,
            request.document,
            expected_revision=request.expected_revision,
        )
    except FlowPersistenceError as exc:
        _raise_persistence_http(exc)
    return _record_payload(record)


@router.delete("/flows/{flow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_flow(
    flow_id: str,
    expected_revision: int | None = Query(default=None, ge=1),
) -> Response:
    try:
        _flow_repository().delete(flow_id, expected_revision=expected_revision)
    except FlowPersistenceError as exc:
        _raise_persistence_http(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/flows/{flow_id}/duplicate", status_code=status.HTTP_201_CREATED)
def duplicate_flow(flow_id: str, request: FlowDuplicateRequest) -> dict:
    try:
        record = _flow_repository().duplicate(flow_id, name=request.name)
    except FlowPersistenceError as exc:
        _raise_persistence_http(exc)
    return _record_payload(record)


@router.post("/flows/import", status_code=status.HTTP_201_CREATED)
def import_flow(request: FlowImportRequest) -> dict:
    try:
        raw = yaml.safe_load(request.yaml)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Flow YAML must contain a mapping")
    try:
        document = FlowDocument.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid FlowDocument: {exc}") from exc
    # Import creates a new identity. Source IDs/revisions are interchange
    # metadata and must not overwrite an existing persisted Flow.
    document = document.model_copy(
        update={
            "flow_id": None,
            "revision": 1,
            **({"name": request.name} if request.name is not None else {}),
        },
        deep=True,
    )
    try:
        record = _flow_repository().create(document, kind="user")
    except FlowPersistenceError as exc:
        _raise_persistence_http(exc)
    return _record_payload(record)


@router.get("/flows/{flow_id}/export")
def export_flow(flow_id: str) -> Response:
    try:
        record = _flow_repository().get(flow_id)
    except FlowPersistenceError as exc:
        _raise_persistence_http(exc)
    text = yaml.safe_dump(
        record.document.model_dump(mode="json", exclude_none=True),
        sort_keys=False,
        allow_unicode=True,
    )
    filename = f"{flow_id}.yaml"
    return Response(
        content=text,
        media_type="application/yaml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Stateless compiler/validator/capability operations
# ---------------------------------------------------------------------------


@router.post("/flows/compile")
def compile_flow(document: FlowDocument) -> dict:
    result = FlowCompiler().compile(document)
    return {
        "compiled": result.compiled,
        "definition": (
            result.definition.model_dump(mode="json")
            if result.definition is not None
            else None
        ),
        "diagnostics": _diagnostics(result.diagnostics),
    }


@router.post("/flows/validate")
def validate_flow(document: FlowDocument) -> dict:
    compile_result = FlowCompiler().compile(document)
    if compile_result.definition is None:
        return {
            "compiled": False,
            "valid": False,
            "definition": None,
            "diagnostics": _diagnostics(compile_result.diagnostics),
        }

    validation = FlowValidator(build_default_node_definition_registry()).validate(
        compile_result.definition
    )
    diagnostics = [*compile_result.diagnostics, *validation.issues]
    return {
        "compiled": True,
        "valid": validation.valid,
        "definition": compile_result.definition.model_dump(mode="json"),
        "diagnostics": _diagnostics(diagnostics),
    }


@router.post("/flows/capabilities")
def check_flow_capabilities(document: FlowDocument) -> dict:
    compile_result = FlowCompiler().compile(document)
    if compile_result.definition is None:
        return {
            "compiled": False,
            "valid": False,
            "runnable": False,
            "diagnostics": _diagnostics(compile_result.diagnostics),
        }

    models = build_default_node_definition_registry()
    validation = FlowValidator(models).validate(compile_result.definition)
    if not validation.valid:
        diagnostics = [*compile_result.diagnostics, *validation.issues]
        return {
            "compiled": True,
            "valid": False,
            "runnable": False,
            "diagnostics": _diagnostics(diagnostics),
        }

    capability = CapabilityChecker(models, _runtime_adapters()).check(
        compile_result.definition
    )
    diagnostics = [
        *compile_result.diagnostics,
        *validation.issues,
        *capability.issues,
    ]
    return {
        "compiled": True,
        "valid": True,
        "runnable": capability.runnable,
        "diagnostics": _diagnostics(diagnostics),
    }
