from __future__ import annotations

from functools import lru_cache
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.flow.persistence import FlowNotFoundError
from app.flow.project_repository import (
    ProjectFlowNotFoundError,
    ProjectNotFoundError,
    ProjectPersistenceError,
    StudioBindingNotFoundError,
)
from app.flow.project_service import ProjectService, ProjectServiceError
from app.flow.projects import ProjectFlowRecord, ProjectRecord, StudioBindingRecord

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProjectRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProjectFlowCreateRequest(BaseModel):
    source_flow_id: str
    name: str | None = Field(default=None, max_length=200)


class ApplyRunRequest(BaseModel):
    run_id: str


@lru_cache(maxsize=1)
def _service() -> ProjectService:
    return ProjectService()


def _project_payload(project: ProjectRecord) -> dict[str, Any]:
    return {
        "project_id": project.project_id,
        "name": project.name,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def _project_flow_payload(record: ProjectFlowRecord, flow=None) -> dict[str, Any]:
    payload = {
        "project_id": record.project_id,
        "flow_id": record.flow_id,
        "source_flow_id": record.source_flow_id,
        "source_flow_revision": record.source_flow_revision,
        "created_at": record.created_at,
    }
    if flow is not None:
        payload.update(
            {
                "name": flow.name,
                "revision": flow.revision,
                "schema_version": flow.schema_version,
                "kind": flow.kind,
            }
        )
    return payload


def _binding_payload(binding: StudioBindingRecord) -> dict[str, Any]:
    return {
        "binding_id": binding.binding_id,
        "project_id": binding.project_id,
        "flow_id": binding.flow_id,
        "flow_output_id": binding.flow_output_id,
        "track_id": binding.track_id,
        "status": binding.status.value,
        "current_run_id": binding.current_run_id,
        "artifact": binding.artifact,
        "media_url": (
            f"/api/flow-runs/{binding.current_run_id}/outputs/"
            f"{binding.flow_output_id}/media"
        ),
        "created_at": binding.created_at,
        "updated_at": binding.updated_at,
    }


def _raise_project_http(exc: Exception) -> None:
    if isinstance(
        exc,
        (
            ProjectNotFoundError,
            ProjectFlowNotFoundError,
            StudioBindingNotFoundError,
            FlowNotFoundError,
        ),
    ):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ProjectServiceError):
        if exc.code in {"run_not_found", "binding_not_in_project"}:
            http_status = status.HTTP_404_NOT_FOUND
        elif exc.code in {"run_not_terminal", "run_not_project_flow"}:
            http_status = status.HTTP_409_CONFLICT
        else:
            http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(
            status_code=http_status,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, (ProjectPersistenceError, ValueError)):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@router.post("", status_code=status.HTTP_201_CREATED)
def create_project(request: ProjectCreateRequest) -> dict[str, Any]:
    try:
        return _project_payload(_service().create_project(request.name))
    except Exception as exc:
        _raise_project_http(exc)
        raise AssertionError("unreachable")


@router.get("")
def list_projects() -> dict[str, Any]:
    return {"projects": [_project_payload(item) for item in _service().list_projects()]}


@router.get("/{project_id}")
def get_project(project_id: str) -> dict[str, Any]:
    try:
        return _project_payload(_service().get_project(project_id))
    except Exception as exc:
        _raise_project_http(exc)
        raise AssertionError("unreachable")


@router.put("/{project_id}")
def rename_project(project_id: str, request: ProjectRenameRequest) -> dict[str, Any]:
    try:
        return _project_payload(
            _service().rename_project(project_id, request.name)
        )
    except Exception as exc:
        _raise_project_http(exc)
        raise AssertionError("unreachable")


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: str) -> None:
    try:
        _service().delete_project(project_id)
    except Exception as exc:
        _raise_project_http(exc)


@router.post("/{project_id}/flows", status_code=status.HTTP_201_CREATED)
def create_project_flow(
    project_id: str, request: ProjectFlowCreateRequest
) -> dict[str, Any]:
    try:
        ownership = _service().copy_flow_to_project(
            project_id, request.source_flow_id, name=request.name
        )
        flow = _service().flow_repository.get(ownership.flow_id)
        return _project_flow_payload(ownership, flow)
    except Exception as exc:
        _raise_project_http(exc)
        raise AssertionError("unreachable")


@router.get("/{project_id}/flows")
def list_project_flows(project_id: str) -> dict[str, Any]:
    try:
        items = _service().list_project_flows(project_id)
        return {
            "flows": [
                _project_flow_payload(item["ownership"], item["flow"])
                for item in items
            ]
        }
    except Exception as exc:
        _raise_project_http(exc)
        raise AssertionError("unreachable")


@router.get("/{project_id}/studio-bindings")
def list_studio_bindings(project_id: str) -> dict[str, Any]:
    try:
        return {
            "bindings": [
                _binding_payload(binding)
                for binding in _service().list_bindings(project_id)
            ]
        }
    except Exception as exc:
        _raise_project_http(exc)
        raise AssertionError("unreachable")


@router.post("/{project_id}/studio-bindings/apply", status_code=status.HTTP_201_CREATED)
def apply_run_to_studio(project_id: str, request: ApplyRunRequest) -> dict[str, Any]:
    try:
        result = _service().apply_run(project_id, request.run_id)
        return {
            "project_id": result.project_id,
            "flow_id": result.flow_id,
            "run_id": result.run_id,
            "created": [_binding_payload(item) for item in result.created],
            "existing": [_binding_payload(item) for item in result.existing],
            "updated": [
                {
                    "binding": _binding_payload(item["binding"]),
                    "previous_run_id": item["previous_run_id"],
                    "temporal_compatibility": item["temporal_compatibility"],
                }
                for item in result.updated
            ],
            "detached": [_binding_payload(item) for item in result.detached],
            "skipped": list(result.skipped),
        }
    except Exception as exc:
        _raise_project_http(exc)
        raise AssertionError("unreachable")


@router.post("/{project_id}/studio-bindings/{binding_id}/detach")
def detach_studio_binding(project_id: str, binding_id: str) -> dict[str, Any]:
    try:
        binding = _service().detach_binding(project_id, binding_id)
        return _binding_payload(binding)
    except Exception as exc:
        _raise_project_http(exc)
        raise AssertionError("unreachable")
