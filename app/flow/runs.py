from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .schema import FlowDefinition


class FlowRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIALLY_FAILED = "partially_failed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeRunStatus(str, Enum):
    PENDING = "pending"
    BLOCKED = "blocked"
    RUNNING = "running"
    CACHED = "cached"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


FLOW_RUN_TRANSITIONS: dict[FlowRunStatus, frozenset[FlowRunStatus]] = {
    FlowRunStatus.QUEUED: frozenset({FlowRunStatus.RUNNING, FlowRunStatus.FAILED, FlowRunStatus.CANCELLED}),
    FlowRunStatus.RUNNING: frozenset(
        {
            FlowRunStatus.SUCCEEDED,
            FlowRunStatus.PARTIALLY_FAILED,
            FlowRunStatus.FAILED,
            FlowRunStatus.CANCELLED,
        }
    ),
    FlowRunStatus.SUCCEEDED: frozenset(),
    FlowRunStatus.PARTIALLY_FAILED: frozenset(),
    FlowRunStatus.FAILED: frozenset(),
    FlowRunStatus.CANCELLED: frozenset(),
}

NODE_RUN_TRANSITIONS: dict[NodeRunStatus, frozenset[NodeRunStatus]] = {
    NodeRunStatus.PENDING: frozenset(
        {
            NodeRunStatus.RUNNING,
            NodeRunStatus.CACHED,
            NodeRunStatus.BLOCKED,
            NodeRunStatus.CANCELLED,
            NodeRunStatus.SKIPPED,
        }
    ),
    NodeRunStatus.RUNNING: frozenset(
        {NodeRunStatus.SUCCEEDED, NodeRunStatus.FAILED, NodeRunStatus.CANCELLED}
    ),
    NodeRunStatus.BLOCKED: frozenset(),
    NodeRunStatus.CACHED: frozenset(),
    NodeRunStatus.SUCCEEDED: frozenset(),
    NodeRunStatus.FAILED: frozenset(),
    NodeRunStatus.CANCELLED: frozenset(),
    NodeRunStatus.SKIPPED: frozenset(),
}


class InvalidRunTransitionError(RuntimeError):
    pass


class FlowRunCancelledError(RuntimeError):
    pass


@dataclass(frozen=True)
class FlowRunRecord:
    run_id: str
    flow_id: str | None
    flow_revision: int
    status: FlowRunStatus
    version: int
    cancel_requested: bool
    retry_of_run_id: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    definition: FlowDefinition
    diagnostics: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class NodeRunRecord:
    run_id: str
    node_id: str
    definition_id: str
    status: NodeRunStatus
    progress: float | None
    message: str | None
    cache_hit: bool
    started_at: str | None
    finished_at: str | None
    diagnostics: tuple[dict[str, Any], ...]
