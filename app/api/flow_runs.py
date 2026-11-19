from __future__ import annotations

import asyncio
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.api.events import _MAX_SSE_SECONDS, SseSlot
from app.flow.persistence import FlowNotFoundError, FlowRevisionConflictError
from app.flow.input_assets import FlowInputAssetError, resolve_flow_input_asset
from app.flow.run_repository import FlowRunNotFoundError, NodeRunNotFoundError
from app.flow.run_service import FlowRunService, FlowRunStartError
from app.flow.runs import FlowRunRecord, FlowRunStatus, NodeRunRecord

router = APIRouter(prefix="/flow-runs", tags=["flow-runs"])

_TERMINAL = {
    FlowRunStatus.SUCCEEDED,
    FlowRunStatus.PARTIALLY_FAILED,
    FlowRunStatus.FAILED,
    FlowRunStatus.CANCELLED,
}


class FlowRunStartRequest(BaseModel):
    flow_id: str
    inputs: dict[str, str] = Field(default_factory=dict)
    input_assets: dict[str, str] = Field(default_factory=dict)
    expected_revision: int | None = Field(default=None, ge=1)


@lru_cache(maxsize=1)
def _service() -> FlowRunService:
    return FlowRunService()


def _node_payload(node: NodeRunRecord) -> dict[str, Any]:
    return {
        "run_id": node.run_id,
        "node_id": node.node_id,
        "definition_id": node.definition_id,
        "status": node.status.value,
        "progress": node.progress,
        "message": node.message,
        "cache_hit": node.cache_hit,
        "started_at": node.started_at,
        "finished_at": node.finished_at,
        "diagnostics": list(node.diagnostics),
    }


def _run_payload(run: FlowRunRecord, *, detailed: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": run.run_id,
        "flow_id": run.flow_id,
        "flow_revision": run.flow_revision,
        "status": run.status.value,
        "version": run.version,
        "cancel_requested": run.cancel_requested,
        "retry_of_run_id": run.retry_of_run_id,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "diagnostics": list(run.diagnostics),
    }
    if detailed:
        service = _service()
        payload["nodes"] = [_node_payload(node) for node in service.nodes(run.run_id)]
        payload["outputs"] = service.outputs(run)
        payload["definition"] = run.definition.model_dump(mode="json")
    return payload


def _raise_run_http(exc: Exception) -> None:
    if isinstance(exc, (FlowNotFoundError, FlowRunNotFoundError, NodeRunNotFoundError)):
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
    if isinstance(exc, FlowInputAssetError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, FlowRunStartError):
        if exc.code == "runtime_unavailable":
            http_status = status.HTTP_503_SERVICE_UNAVAILABLE
        elif exc.code in {"run_not_terminal", "capability_failed"}:
            http_status = status.HTTP_409_CONFLICT
        else:
            http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
        raise HTTPException(
            status_code=http_status,
            detail={
                "code": exc.code,
                "message": str(exc),
                "diagnostics": exc.diagnostics,
            },
        ) from exc
    raise exc


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def start_flow_run(request: FlowRunStartRequest) -> dict[str, Any]:
    try:
        if request.inputs and request.input_assets:
            raise FlowRunStartError(
                "input_binding_invalid",
                "Use either path inputs or library input_assets, not both",
            )
        bound_inputs = (
            {name: resolve_flow_input_asset(asset_id) for name, asset_id in request.input_assets.items()}
            if request.input_assets
            else {name: Path(path) for name, path in request.inputs.items()}
        )
        run = _service().start(
            request.flow_id,
            bound_inputs,
            expected_revision=request.expected_revision,
        )
    except Exception as exc:
        _raise_run_http(exc)
    return _run_payload(run, detailed=True)


@router.get("")
def list_flow_runs(flow_id: str | None = Query(default=None)) -> dict[str, Any]:
    return {"runs": [_run_payload(run) for run in _service().list(flow_id=flow_id)]}


@router.get("/{run_id}")
def get_flow_run(run_id: str) -> dict[str, Any]:
    try:
        run = _service().get(run_id)
        return _run_payload(run, detailed=True)
    except Exception as exc:
        _raise_run_http(exc)
        raise AssertionError("unreachable")


@router.get("/{run_id}/nodes")
def get_flow_run_nodes(run_id: str) -> dict[str, Any]:
    try:
        nodes = _service().nodes(run_id)
    except Exception as exc:
        _raise_run_http(exc)
        raise AssertionError("unreachable")
    return {"nodes": [_node_payload(node) for node in nodes]}


@router.post("/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_flow_run(run_id: str) -> dict[str, Any]:
    try:
        run = _service().request_cancel(run_id)
    except Exception as exc:
        _raise_run_http(exc)
        raise AssertionError("unreachable")
    return _run_payload(run, detailed=True)


@router.post("/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_flow_run(run_id: str) -> dict[str, Any]:
    try:
        run = _service().retry(run_id)
    except Exception as exc:
        _raise_run_http(exc)
        raise AssertionError("unreachable")
    return _run_payload(run, detailed=True)


@router.get("/{run_id}/outputs/{output_id}/media")
def get_flow_run_output_media(run_id: str, output_id: str) -> FileResponse:
    try:
        run = _service().get(run_id)
    except Exception as exc:
        _raise_run_http(exc)
        raise AssertionError("unreachable")
    definition = run.definition.outputs.get(output_id)
    if definition is None or not definition.publish:
        raise HTTPException(status_code=404, detail="published Flow Output not found")
    output = _service().outputs(run).get(output_id)
    artifact = output.get("artifact") if output else None
    if not output or not output.get("available") or not isinstance(artifact, dict):
        raise HTTPException(status_code=404, detail="Flow Output artifact is unavailable")
    path_value = artifact.get("path")
    if not isinstance(path_value, str):
        raise HTTPException(status_code=404, detail="Flow Output artifact is unavailable")
    path = Path(path_value).resolve()
    root = (_service().runs_root / run_id / "outputs").resolve()
    if not path.is_file() or not path.is_relative_to(root):
        raise HTTPException(status_code=404, detail="Flow Output artifact is unavailable")
    media_type = "audio/wav" if path.suffix.lower() == ".wav" else None
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})


@router.get("/{run_id}/events")
async def flow_run_events(run_id: str) -> StreamingResponse:
    try:
        _service().get(run_id)
    except Exception as exc:
        _raise_run_http(exc)
    slot = SseSlot()

    async def stream():
        try:
            last_version = -1
            keepalive_at = 0
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _MAX_SSE_SECONDS
            while loop.time() < deadline:
                try:
                    run = _service().get(run_id)
                except FlowRunNotFoundError:
                    return
                if run.version != last_version:
                    # A single SQLite transaction increments version after each
                    # lifecycle write, so re-reading after the payload gives us
                    # a cheap torn-snapshot guard analogous to Job SSE.
                    version = run.version
                    payload = _run_payload(run, detailed=True)
                    if _service().get(run_id).version != version:
                        continue
                    yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                    last_version = version
                    keepalive_at = 0
                    if run.status in _TERMINAL:
                        return
                elif run.status in _TERMINAL:
                    return
                keepalive_at += 1
                if keepalive_at >= 75:
                    yield ": keepalive\n\n"
                    keepalive_at = 0
                await asyncio.sleep(0.2)
        finally:
            slot.release()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
