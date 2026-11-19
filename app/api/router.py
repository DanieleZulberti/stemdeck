from __future__ import annotations

from fastapi import APIRouter

from app.api.config import router as config_router
from app.api.discogs import router as discogs_router
from app.api.events import router as events_router
from app.api.flows import router as flows_router
from app.api.flow_runs import router as flow_runs_router
from app.api.flow_input_assets import router as flow_input_assets_router
from app.api.flow_artifacts import router as flow_artifacts_router
from app.api.flow_studio import router as flow_studio_router
from app.api.jobs import router as jobs_router
from app.api.playlist import router as playlist_router
from app.api.projects import router as projects_router
from app.api.qr import router as qr_router
from app.api.queue import router as queue_router
from app.api.search import router as search_router
from app.api.stems import router as stems_router

router = APIRouter()
router.include_router(config_router, tags=["config"])
router.include_router(flows_router, tags=["flows"])
router.include_router(flow_runs_router, tags=["flow-runs"])
router.include_router(flow_input_assets_router, tags=["flow-input-assets"])
router.include_router(flow_artifacts_router, tags=["flow-artifacts"])
router.include_router(flow_studio_router)
router.include_router(jobs_router, prefix="/jobs", tags=["jobs"])
router.include_router(events_router, tags=["events"])
router.include_router(stems_router, tags=["stems"])
router.include_router(qr_router, tags=["qr"])
router.include_router(queue_router, prefix="/queue", tags=["queue"])
router.include_router(playlist_router, prefix="/playlist", tags=["playlist"])
router.include_router(search_router, prefix="/search", tags=["search"])
router.include_router(discogs_router, prefix="/discogs", tags=["discogs"])

router.include_router(projects_router)
