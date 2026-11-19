from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class StudioBindingStatus(str, Enum):
    MANAGED = "managed"
    DETACHED = "detached"


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    name: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ProjectFlowRecord:
    project_id: str
    flow_id: str
    source_flow_id: str | None
    source_flow_revision: int | None
    created_at: str


@dataclass(frozen=True)
class StudioBindingRecord:
    binding_id: str
    project_id: str
    flow_id: str
    flow_output_id: str
    track_id: str
    status: StudioBindingStatus
    current_run_id: str
    artifact: dict[str, Any]
    created_at: str
    updated_at: str
