from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request, status

from app.flow.input_assets import FlowInputAssetError, list_flow_input_assets, store_flow_input_upload

router = APIRouter(prefix="/flow-input-assets", tags=["flow-input-assets"])


@router.get("")
def list_assets() -> dict:
    """Opaque audio assets that can be bound to Flow Inputs from the UI."""
    return {"assets": list_flow_input_assets()}


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_asset(request: Request) -> dict:
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename") or not hasattr(upload, "file"):
        raise HTTPException(status_code=422, detail={"code": "asset_upload_missing", "message": "No audio file provided"})
    try:
        asset = await asyncio.to_thread(store_flow_input_upload, getattr(upload, "filename", "") or "audio", upload.file)
    except FlowInputAssetError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc
    return {"asset": asset}
