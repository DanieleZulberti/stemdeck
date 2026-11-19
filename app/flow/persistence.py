from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

from .schema import FlowDocument

FlowKind = Literal["user", "builtin", "project"]


class FlowPersistenceError(RuntimeError):
    pass


class FlowNotFoundError(FlowPersistenceError):
    def __init__(self, flow_id: str) -> None:
        self.flow_id = flow_id
        super().__init__(f"Flow '{flow_id}' was not found")


class FlowRevisionConflictError(FlowPersistenceError):
    def __init__(self, flow_id: str, expected: int, actual: int) -> None:
        self.flow_id = flow_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Flow '{flow_id}' revision conflict: expected {expected}, current revision is {actual}"
        )


class ReadOnlyFlowError(FlowPersistenceError):
    pass


@dataclass(frozen=True)
class FlowRecord:
    flow_id: str
    kind: FlowKind
    name: str
    revision: int
    schema_version: int
    created_at: str
    updated_at: str
    document: FlowDocument

    def metadata(self) -> dict[str, str | int]:
        return {
            "flow_id": self.flow_id,
            "kind": self.kind,
            "name": self.name,
            "revision": self.revision,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class FlowRepository:
    """SQLite-backed persistence for editor FlowDocuments.

    Large run artifacts deliberately do not live in this database. The DB owns
    domain metadata and JSON documents; artifact bytes remain filesystem-backed.
    """

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
                CREATE TABLE IF NOT EXISTS flows (
                    flow_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('user', 'builtin', 'project')),
                    name TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
                    document_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_flows_kind_name
                    ON flows(kind, name COLLATE NOCASE);
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _encode(document: FlowDocument) -> str:
        return json.dumps(document.model_dump(mode="json"), separators=(",", ":"))

    @staticmethod
    def _decode(row: sqlite3.Row) -> FlowRecord:
        document = FlowDocument.model_validate(json.loads(row["document_json"]))
        return FlowRecord(
            flow_id=row["flow_id"],
            kind=row["kind"],
            name=row["name"],
            revision=row["revision"],
            schema_version=row["schema_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            document=document,
        )

    def list(self, *, kind: FlowKind | None = None) -> list[FlowRecord]:
        query = "SELECT * FROM flows"
        params: tuple[str, ...] = ()
        if kind is not None:
            query += " WHERE kind = ?"
            params = (kind,)
        query += " ORDER BY name COLLATE NOCASE, flow_id"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode(row) for row in rows]

    def get(self, flow_id: str) -> FlowRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM flows WHERE flow_id = ?", (flow_id,)
            ).fetchone()
        if row is None:
            raise FlowNotFoundError(flow_id)
        return self._decode(row)

    def create(
        self,
        document: FlowDocument,
        *,
        kind: FlowKind = "user",
        flow_id: str | None = None,
    ) -> FlowRecord:
        if kind == "builtin":
            raise ReadOnlyFlowError("Built-in flows are repository-owned and cannot be created here")
        stable_id = flow_id or document.flow_id or str(uuid4())
        now = self._now()
        persisted = document.model_copy(
            update={"flow_id": stable_id, "revision": 1},
            deep=True,
        )
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO flows (
                        flow_id, kind, name, revision, schema_version,
                        document_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stable_id,
                        kind,
                        persisted.name,
                        1,
                        persisted.schema_version,
                        self._encode(persisted),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise FlowPersistenceError(f"Flow '{stable_id}' already exists") from exc
        return self.get(stable_id)

    def update(
        self,
        flow_id: str,
        document: FlowDocument,
        *,
        expected_revision: int,
    ) -> FlowRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT kind, revision FROM flows WHERE flow_id = ?", (flow_id,)
            ).fetchone()
            if row is None:
                raise FlowNotFoundError(flow_id)
            if row["kind"] == "builtin":
                raise ReadOnlyFlowError(f"Built-in flow '{flow_id}' is read-only")
            actual_revision = int(row["revision"])
            if actual_revision != expected_revision:
                raise FlowRevisionConflictError(flow_id, expected_revision, actual_revision)

            next_revision = actual_revision + 1
            now = self._now()
            persisted = document.model_copy(
                update={"flow_id": flow_id, "revision": next_revision},
                deep=True,
            )
            cursor = connection.execute(
                """
                UPDATE flows
                   SET name = ?, revision = ?, schema_version = ?,
                       document_json = ?, updated_at = ?
                 WHERE flow_id = ? AND revision = ?
                """,
                (
                    persisted.name,
                    next_revision,
                    persisted.schema_version,
                    self._encode(persisted),
                    now,
                    flow_id,
                    actual_revision,
                ),
            )
            if cursor.rowcount != 1:
                current = connection.execute(
                    "SELECT revision FROM flows WHERE flow_id = ?", (flow_id,)
                ).fetchone()
                actual = int(current["revision"]) if current is not None else actual_revision
                raise FlowRevisionConflictError(flow_id, expected_revision, actual)
        return self.get(flow_id)

    def delete(self, flow_id: str, *, expected_revision: int | None = None) -> None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT kind, revision FROM flows WHERE flow_id = ?", (flow_id,)
            ).fetchone()
            if row is None:
                raise FlowNotFoundError(flow_id)
            if row["kind"] == "builtin":
                raise ReadOnlyFlowError(f"Built-in flow '{flow_id}' is read-only")
            actual_revision = int(row["revision"])
            if expected_revision is not None and expected_revision != actual_revision:
                raise FlowRevisionConflictError(flow_id, expected_revision, actual_revision)
            connection.execute("DELETE FROM flows WHERE flow_id = ?", (flow_id,))

    def duplicate(self, flow_id: str, *, name: str | None = None) -> FlowRecord:
        source = self.get(flow_id)
        clone = source.document.model_copy(
            update={"flow_id": None, "revision": 1, "name": name or f"{source.name} Copy"},
            deep=True,
        )
        # A Project Flow snapshot remains project-owned only when duplication is
        # explicitly performed by the Project layer (M7). User-facing duplicate
        # creates an independent User Flow.
        return self.create(clone, kind="user")
