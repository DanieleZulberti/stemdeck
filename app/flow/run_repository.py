from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .runs import (
    FLOW_RUN_TRANSITIONS,
    NODE_RUN_TRANSITIONS,
    FlowRunCancelledError,
    FlowRunRecord,
    FlowRunStatus,
    InvalidRunTransitionError,
    NodeRunRecord,
    NodeRunStatus,
)
from .schema import FlowDefinition


class FlowRunNotFoundError(RuntimeError):
    pass


class NodeRunNotFoundError(RuntimeError):
    pass


class FlowRunRepository:
    """Persist immutable Flow snapshots and mutable run lifecycle state."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _init_schema(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS flow_runs (
                    run_id TEXT PRIMARY KEY,
                    flow_id TEXT,
                    flow_revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    retry_of_run_id TEXT,
                    definition_json TEXT NOT NULL,
                    diagnostics_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_flow_runs_flow_created
                    ON flow_runs(flow_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS node_runs (
                    run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    definition_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL,
                    message TEXT,
                    cache_hit INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT,
                    finished_at TEXT,
                    diagnostics_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (run_id, node_id),
                    FOREIGN KEY (run_id) REFERENCES flow_runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_node_runs_status
                    ON node_runs(run_id, status);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(flow_runs)")}
            if "diagnostics_json" not in columns:
                connection.execute(
                    "ALTER TABLE flow_runs ADD COLUMN diagnostics_json TEXT NOT NULL DEFAULT '[]'"
                )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _decode_run(row: sqlite3.Row) -> FlowRunRecord:
        return FlowRunRecord(
            run_id=row["run_id"],
            flow_id=row["flow_id"],
            flow_revision=int(row["flow_revision"]),
            status=FlowRunStatus(row["status"]),
            version=int(row["version"]),
            cancel_requested=bool(row["cancel_requested"]),
            retry_of_run_id=row["retry_of_run_id"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            definition=FlowDefinition.model_validate(json.loads(row["definition_json"])),
            diagnostics=tuple(json.loads(row["diagnostics_json"] or "[]")),
        )

    @staticmethod
    def _decode_node(row: sqlite3.Row) -> NodeRunRecord:
        return NodeRunRecord(
            run_id=row["run_id"],
            node_id=row["node_id"],
            definition_id=row["definition_id"],
            status=NodeRunStatus(row["status"]),
            progress=float(row["progress"]) if row["progress"] is not None else None,
            message=row["message"],
            cache_hit=bool(row["cache_hit"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            diagnostics=tuple(json.loads(row["diagnostics_json"])),
        )

    def create_run(
        self,
        definition: FlowDefinition,
        *,
        retry_of_run_id: str | None = None,
        run_id: str | None = None,
    ) -> FlowRunRecord:
        stable_run_id = run_id or str(uuid4())
        now = self._now()
        snapshot_json = json.dumps(
            definition.model_dump(mode="json"), separators=(",", ":")
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO flow_runs (
                    run_id, flow_id, flow_revision, status, version,
                    cancel_requested, retry_of_run_id, definition_json, created_at
                ) VALUES (?, ?, ?, ?, 1, 0, ?, ?, ?)
                """,
                (
                    stable_run_id,
                    definition.flow_id,
                    definition.revision,
                    FlowRunStatus.QUEUED.value,
                    retry_of_run_id,
                    snapshot_json,
                    now,
                ),
            )
            for node_id, node in definition.nodes.items():
                connection.execute(
                    """
                    INSERT INTO node_runs (
                        run_id, node_id, definition_id, status, diagnostics_json
                    ) VALUES (?, ?, ?, ?, '[]')
                    """,
                    (
                        stable_run_id,
                        node_id,
                        node.definition_id,
                        NodeRunStatus.PENDING.value,
                    ),
                )
        return self.get_run(stable_run_id)

    def get_run(self, run_id: str) -> FlowRunRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM flow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise FlowRunNotFoundError(f"Flow run '{run_id}' was not found")
        return self._decode_run(row)

    def list_runs(self, *, flow_id: str | None = None) -> list[FlowRunRecord]:
        query = "SELECT * FROM flow_runs"
        params: tuple[str, ...] = ()
        if flow_id is not None:
            query += " WHERE flow_id = ?"
            params = (flow_id,)
        query += " ORDER BY created_at DESC, run_id DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode_run(row) for row in rows]

    def get_node_run(self, run_id: str, node_id: str) -> NodeRunRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM node_runs WHERE run_id = ? AND node_id = ?",
                (run_id, node_id),
            ).fetchone()
        if row is None:
            raise NodeRunNotFoundError(
                f"Node run '{node_id}' for flow run '{run_id}' was not found"
            )
        return self._decode_node(row)

    def list_node_runs(self, run_id: str) -> list[NodeRunRecord]:
        # Prove parent existence so an unknown run is distinguishable from a
        # real run containing zero execution nodes.
        self.get_run(run_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM node_runs WHERE run_id = ? ORDER BY node_id", (run_id,)
            ).fetchall()
        return [self._decode_node(row) for row in rows]

    def transition_run(self, run_id: str, target: FlowRunStatus) -> FlowRunRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM flow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise FlowRunNotFoundError(f"Flow run '{run_id}' was not found")
            current = FlowRunStatus(row["status"])
            if target not in FLOW_RUN_TRANSITIONS[current]:
                raise InvalidRunTransitionError(
                    f"Invalid FlowRun transition {current.value} -> {target.value}"
                )

            now = self._now()
            started_at = now if target == FlowRunStatus.RUNNING else None
            terminal = target in {
                FlowRunStatus.SUCCEEDED,
                FlowRunStatus.PARTIALLY_FAILED,
                FlowRunStatus.FAILED,
                FlowRunStatus.CANCELLED,
            }
            if target == FlowRunStatus.RUNNING:
                connection.execute(
                    """
                    UPDATE flow_runs
                       SET status = ?, started_at = ?, version = version + 1
                     WHERE run_id = ?
                    """,
                    (target.value, started_at, run_id),
                )
            elif terminal:
                connection.execute(
                    """
                    UPDATE flow_runs
                       SET status = ?, finished_at = ?, version = version + 1
                     WHERE run_id = ?
                    """,
                    (target.value, now, run_id),
                )
        return self.get_run(run_id)

    def transition_node(
        self,
        run_id: str,
        node_id: str,
        target: NodeRunStatus,
        *,
        message: str | None = None,
        diagnostics: list[dict] | None = None,
    ) -> NodeRunRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM node_runs WHERE run_id = ? AND node_id = ?",
                (run_id, node_id),
            ).fetchone()
            if row is None:
                raise NodeRunNotFoundError(
                    f"Node run '{node_id}' for flow run '{run_id}' was not found"
                )
            current = NodeRunStatus(row["status"])
            if target not in NODE_RUN_TRANSITIONS[current]:
                raise InvalidRunTransitionError(
                    f"Invalid NodeRun transition {current.value} -> {target.value}"
                )

            now = self._now()
            started_at = now if target == NodeRunStatus.RUNNING else None
            terminal = target in {
                NodeRunStatus.BLOCKED,
                NodeRunStatus.CACHED,
                NodeRunStatus.SUCCEEDED,
                NodeRunStatus.FAILED,
                NodeRunStatus.CANCELLED,
                NodeRunStatus.SKIPPED,
            }
            cache_hit = 1 if target == NodeRunStatus.CACHED else 0
            progress = 1.0 if target in {NodeRunStatus.CACHED, NodeRunStatus.SUCCEEDED} else None
            diagnostics_json = json.dumps(diagnostics or [], separators=(",", ":"))
            connection.execute(
                """
                UPDATE node_runs
                   SET status = ?,
                       message = COALESCE(?, message),
                       diagnostics_json = CASE WHEN ? THEN ? ELSE diagnostics_json END,
                       cache_hit = CASE WHEN ? THEN 1 ELSE cache_hit END,
                       progress = COALESCE(?, progress),
                       started_at = COALESCE(?, started_at),
                       finished_at = CASE WHEN ? THEN ? ELSE finished_at END
                 WHERE run_id = ? AND node_id = ?
                """,
                (
                    target.value,
                    message,
                    1 if diagnostics is not None else 0,
                    diagnostics_json,
                    cache_hit,
                    progress,
                    started_at,
                    1 if terminal else 0,
                    now,
                    run_id,
                    node_id,
                ),
            )
            connection.execute(
                "UPDATE flow_runs SET version = version + 1 WHERE run_id = ?",
                (run_id,),
            )
        return self.get_node_run(run_id, node_id)

    def update_node_progress(
        self,
        run_id: str,
        node_id: str,
        progress: float | None,
        message: str | None = None,
    ) -> NodeRunRecord:
        if progress is not None and not 0.0 <= progress <= 1.0:
            raise ValueError("Node progress must be between 0 and 1")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM node_runs WHERE run_id = ? AND node_id = ?",
                (run_id, node_id),
            ).fetchone()
            if row is None:
                raise NodeRunNotFoundError(
                    f"Node run '{node_id}' for flow run '{run_id}' was not found"
                )
            if NodeRunStatus(row["status"]) != NodeRunStatus.RUNNING:
                raise InvalidRunTransitionError("Progress can only be updated for a running NodeRun")
            connection.execute(
                """
                UPDATE node_runs SET progress = ?, message = COALESCE(?, message)
                 WHERE run_id = ? AND node_id = ?
                """,
                (progress, message, run_id, node_id),
            )
            connection.execute(
                "UPDATE flow_runs SET version = version + 1 WHERE run_id = ?",
                (run_id,),
            )
        return self.get_node_run(run_id, node_id)

    def add_run_diagnostic(self, run_id: str, diagnostic: dict) -> FlowRunRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT diagnostics_json FROM flow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise FlowRunNotFoundError(f"Flow run '{run_id}' was not found")
            diagnostics = json.loads(row["diagnostics_json"] or "[]")
            diagnostics.append(diagnostic)
            connection.execute(
                "UPDATE flow_runs SET diagnostics_json = ?, version = version + 1 WHERE run_id = ?",
                (json.dumps(diagnostics, separators=(",", ":")), run_id),
            )
        return self.get_run(run_id)

    def request_cancel(self, run_id: str) -> FlowRunRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT status, cancel_requested FROM flow_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise FlowRunNotFoundError(f"Flow run '{run_id}' was not found")
            status = FlowRunStatus(row["status"])
            if status in {
                FlowRunStatus.SUCCEEDED,
                FlowRunStatus.PARTIALLY_FAILED,
                FlowRunStatus.FAILED,
                FlowRunStatus.CANCELLED,
            }:
                return self.get_run(run_id)
            if not bool(row["cancel_requested"]):
                connection.execute(
                    """
                    UPDATE flow_runs
                       SET cancel_requested = 1, version = version + 1
                     WHERE run_id = ?
                    """,
                    (run_id,),
                )
        return self.get_run(run_id)

    def cancel_requested(self, run_id: str) -> bool:
        return self.get_run(run_id).cancel_requested


class RunCancellationToken:
    """Graph-native cancellation view, independent of legacy Job objects."""

    def __init__(self, repository: FlowRunRepository, run_id: str) -> None:
        self.repository = repository
        self.run_id = run_id

    @property
    def is_cancel_requested(self) -> bool:
        return self.repository.cancel_requested(self.run_id)

    def raise_if_cancelled(self) -> None:
        if self.is_cancel_requested:
            raise FlowRunCancelledError(f"Flow run '{self.run_id}' was cancelled")
