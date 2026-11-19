from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.config import FLOW_CACHE_DIR, FLOW_DB_PATH, FLOW_RUNS_DIR
from app.flow.cache import ArtifactCache
from app.flow.lifecycle import ArtifactLifecycleService
from app.flow.project_repository import ProjectRepository
from app.flow.run_repository import FlowRunRepository


router = APIRouter(prefix="/flow-artifacts", tags=["flow-artifacts"])


class ArtifactGcRequest(BaseModel):
    # Destructive collection must be explicitly requested. The API is safe by
    # default and therefore returns a preview.
    dry_run: bool = True
    min_age_seconds: float = Field(default=86400.0, ge=0)
    # Cache entries have independent content-addressed lifecycle. Omitting this
    # value means "do not prune the cache" rather than inventing an MVP TTL.
    cache_max_idle_seconds: float | None = Field(default=None, ge=0)


@lru_cache(maxsize=1)
def _service() -> ArtifactLifecycleService:
    return ArtifactLifecycleService(
        run_repository=FlowRunRepository(FLOW_DB_PATH),
        project_repository=ProjectRepository(FLOW_DB_PATH),
        runs_root=FLOW_RUNS_DIR,
        cache=ArtifactCache(FLOW_CACHE_DIR),
    )


@router.post("/gc")
def collect_flow_artifacts(request: ArtifactGcRequest) -> dict:
    try:
        return _service().collect(
            dry_run=request.dry_run,
            min_age_seconds=request.min_age_seconds,
            cache_max_idle_seconds=request.cache_max_idle_seconds,
        ).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
