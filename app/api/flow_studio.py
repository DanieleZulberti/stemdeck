from __future__ import annotations

from functools import lru_cache
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from app.flow.flow_studio_service import FlowStudioService
from app.flow.persistence import FlowNotFoundError
from app.flow.project_repository import (
    ProjectPersistenceError,
    StudioBindingNotFoundError,
)
from app.flow.project_service import ProjectServiceError
from app.flow.run_repository import FlowRunNotFoundError

router = APIRouter(prefix="/flow-studio", tags=["flow-studio"])


class ApplyFlowRunRequest(BaseModel):
    run_id: str


@lru_cache(maxsize=1)
def _service() -> FlowStudioService:
    return FlowStudioService()


def _binding_payload(binding) -> dict[str, Any]:
    return {
        "binding_id": binding.binding_id,
        "flow_id": binding.flow_id,
        "flow_output_id": binding.flow_output_id,
        "track_id": binding.track_id,
        "status": binding.status.value,
        "current_run_id": binding.current_run_id,
        "artifact": binding.artifact,
        "media_url": f"/api/flow-runs/{binding.current_run_id}/outputs/{binding.flow_output_id}/media",
        "created_at": binding.created_at,
        "updated_at": binding.updated_at,
    }


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, (FlowRunNotFoundError, FlowNotFoundError, StudioBindingNotFoundError)):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ProjectServiceError):
        if exc.code == "run_not_terminal":
            code = status.HTTP_409_CONFLICT
        else:
            code = status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(status_code=code, detail={"code": exc.code, "message": str(exc)}) from exc
    if isinstance(exc, (ProjectPersistenceError, ValueError)):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@router.get("/bindings")
def list_flow_studio_bindings(flow_id: str | None = Query(default=None)) -> dict[str, Any]:
    try:
        return {"bindings": [_binding_payload(item) for item in _service().list_bindings(flow_id)]}
    except Exception as exc:
        _raise_http(exc)
        raise AssertionError("unreachable")


@router.post("/apply", status_code=status.HTTP_201_CREATED)
def apply_flow_run(request: ApplyFlowRunRequest) -> dict[str, Any]:
    try:
        result = _service().apply_run(request.run_id)
        return {
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
        _raise_http(exc)
        raise AssertionError("unreachable")


@router.post("/bindings/{binding_id}/detach")
def detach_flow_studio_binding(binding_id: str) -> dict[str, Any]:
    try:
        return _binding_payload(_service().detach_binding(binding_id))
    except Exception as exc:
        _raise_http(exc)
        raise AssertionError("unreachable")
