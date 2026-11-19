from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .projects import ProjectFlowRecord, ProjectRecord, StudioBindingRecord, StudioBindingStatus


class ProjectPersistenceError(RuntimeError):
    pass


class ProjectNotFoundError(ProjectPersistenceError):
    pass


class ProjectFlowNotFoundError(ProjectPersistenceError):
    pass


class StudioBindingNotFoundError(ProjectPersistenceError):
    pass


class ProjectRepository:
    """SQLite metadata for Projects, Project Flow ownership and Studio bindings.

    Project metadata does not own audio bytes. FlowRun artifacts remain in the
    Flow run filesystem; bindings store immutable artifact metadata and form the
    reference that M9 garbage collection must treat as live.
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
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_projects_name
                    ON projects(name COLLATE NOCASE);

                CREATE TABLE IF NOT EXISTS project_flows (
                    project_id TEXT NOT NULL,
                    flow_id TEXT PRIMARY KEY,
                    source_flow_id TEXT,
                    source_flow_revision INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
                    FOREIGN KEY (flow_id) REFERENCES flows(flow_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_project_flows_project
                    ON project_flows(project_id, created_at);

                CREATE TABLE IF NOT EXISTS studio_bindings (
                    binding_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    flow_id TEXT NOT NULL,
                    flow_output_id TEXT NOT NULL,
                    track_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('managed', 'detached')),
                    current_run_id TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, flow_id, flow_output_id),
                    UNIQUE(project_id, track_id),
                    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
                    FOREIGN KEY (flow_id) REFERENCES flows(flow_id) ON DELETE CASCADE,
                    FOREIGN KEY (current_run_id) REFERENCES flow_runs(run_id)
                );

                CREATE INDEX IF NOT EXISTS idx_studio_bindings_project_status
                    ON studio_bindings(project_id, status);
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _decode_project(row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            project_id=row["project_id"],
            name=row["name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _decode_project_flow(row: sqlite3.Row) -> ProjectFlowRecord:
        return ProjectFlowRecord(
            project_id=row["project_id"],
            flow_id=row["flow_id"],
            source_flow_id=row["source_flow_id"],
            source_flow_revision=(
                int(row["source_flow_revision"])
                if row["source_flow_revision"] is not None
                else None
            ),
            created_at=row["created_at"],
        )

    @staticmethod
    def _decode_binding(row: sqlite3.Row) -> StudioBindingRecord:
        return StudioBindingRecord(
            binding_id=row["binding_id"],
            project_id=row["project_id"],
            flow_id=row["flow_id"],
            flow_output_id=row["flow_output_id"],
            track_id=row["track_id"],
            status=StudioBindingStatus(row["status"]),
            current_run_id=row["current_run_id"],
            artifact=json.loads(row["artifact_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_project(self, name: str, *, project_id: str | None = None) -> ProjectRecord:
        stable_id = project_id or str(uuid4())
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Project name cannot be empty")
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO projects(project_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (stable_id, cleaned, now, now),
            )
        return self.get_project(stable_id)

    def list_projects(self) -> list[ProjectRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM projects WHERE substr(project_id, 1, 2) != '__' ORDER BY name COLLATE NOCASE, project_id"
            ).fetchall()
        return [self._decode_project(row) for row in rows]

    def get_project(self, project_id: str) -> ProjectRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise ProjectNotFoundError(f"Project '{project_id}' was not found")
        return self._decode_project(row)

    def rename_project(self, project_id: str, name: str) -> ProjectRecord:
        self.get_project(project_id)
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Project name cannot be empty")
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE projects SET name = ?, updated_at = ? WHERE project_id = ?",
                (cleaned, self._now(), project_id),
            )
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> None:
        self.get_project(project_id)
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))

    def attach_project_flow(
        self,
        project_id: str,
        flow_id: str,
        *,
        source_flow_id: str | None,
        source_flow_revision: int | None,
    ) -> ProjectFlowRecord:
        self.get_project(project_id)
        now = self._now()
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO project_flows(
                        project_id, flow_id, source_flow_id, source_flow_revision, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (project_id, flow_id, source_flow_id, source_flow_revision, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ProjectPersistenceError(
                    f"Flow '{flow_id}' cannot be attached to Project '{project_id}'"
                ) from exc
        return self.get_project_flow(project_id, flow_id)

    def get_project_flow(self, project_id: str, flow_id: str) -> ProjectFlowRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM project_flows WHERE project_id = ? AND flow_id = ?",
                (project_id, flow_id),
            ).fetchone()
        if row is None:
            raise ProjectFlowNotFoundError(
                f"Flow '{flow_id}' does not belong to Project '{project_id}'"
            )
        return self._decode_project_flow(row)

    def list_project_flows(self, project_id: str) -> list[ProjectFlowRecord]:
        self.get_project(project_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM project_flows WHERE project_id = ? ORDER BY created_at, flow_id",
                (project_id,),
            ).fetchall()
        return [self._decode_project_flow(row) for row in rows]

    def get_binding(self, project_id: str, flow_id: str, output_id: str) -> StudioBindingRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM studio_bindings
                 WHERE project_id = ? AND flow_id = ? AND flow_output_id = ?
                """,
                (project_id, flow_id, output_id),
            ).fetchone()
        return self._decode_binding(row) if row is not None else None

    def get_binding_by_id(self, binding_id: str) -> StudioBindingRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM studio_bindings WHERE binding_id = ?", (binding_id,)
            ).fetchone()
        if row is None:
            raise StudioBindingNotFoundError(
                f"Studio binding '{binding_id}' was not found"
            )
        return self._decode_binding(row)

    def create_binding(
        self,
        *,
        project_id: str,
        flow_id: str,
        output_id: str,
        run_id: str,
        artifact: dict,
        track_id: str | None = None,
    ) -> StudioBindingRecord:
        self.get_project_flow(project_id, flow_id)
        existing = self.get_binding(project_id, flow_id, output_id)
        if existing is not None:
            if existing.current_run_id == run_id:
                return existing
            raise ProjectPersistenceError(
                f"Studio binding '{existing.binding_id}' already exists for "
                f"{project_id}/{flow_id}/{output_id}; use reconcile_binding() to move it "
                f"from Run '{existing.current_run_id}' to Run '{run_id}'"
            )
        now = self._now()
        binding_id = str(uuid4())
        stable_track_id = track_id or f"flow-track-{uuid4()}"
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO studio_bindings(
                    binding_id, project_id, flow_id, flow_output_id, track_id,
                    status, current_run_id, artifact_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding_id,
                    project_id,
                    flow_id,
                    output_id,
                    stable_track_id,
                    StudioBindingStatus.MANAGED.value,
                    run_id,
                    json.dumps(artifact, separators=(",", ":")),
                    now,
                    now,
                ),
            )
        binding = self.get_binding(project_id, flow_id, output_id)
        assert binding is not None
        return binding


    def reconcile_binding(
        self,
        binding_id: str,
        *,
        run_id: str,
        artifact: dict,
        status: StudioBindingStatus = StudioBindingStatus.MANAGED,
    ) -> StudioBindingRecord:
        """Move an existing stable binding to a newer immutable Run artifact.

        binding_id and track_id are intentionally never changed. Re-attaching a
        previously detached output is therefore the same managed Studio track,
        not a newly created one.
        """
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM studio_bindings WHERE binding_id = ?", (binding_id,)
            ).fetchone()
            if row is None:
                raise StudioBindingNotFoundError(
                    f"Studio binding '{binding_id}' was not found"
                )
            connection.execute(
                """
                UPDATE studio_bindings
                   SET status = ?, current_run_id = ?, artifact_json = ?, updated_at = ?
                 WHERE binding_id = ?
                """,
                (
                    status.value,
                    run_id,
                    json.dumps(artifact, separators=(",", ":")),
                    self._now(),
                    binding_id,
                ),
            )
        return self.get_binding_by_id(binding_id)

    def detach_outputs_not_in(
        self, project_id: str, flow_id: str, published_output_ids: set[str]
    ) -> list[StudioBindingRecord]:
        """Detach managed tracks whose stable Flow Output no longer publishes.

        Missing artifacts from a partial/failed Run are not handled here: the
        caller passes the definition's published output IDs, so a temporarily
        unavailable output keeps its previous managed media instead of being
        mistaken for a Flow edit that removed the output.
        """
        detached: list[StudioBindingRecord] = []
        for binding in self.list_bindings(project_id):
            if (
                binding.flow_id == flow_id
                and binding.status == StudioBindingStatus.MANAGED
                and binding.flow_output_id not in published_output_ids
            ):
                detached.append(
                    self.set_binding_status(binding.binding_id, StudioBindingStatus.DETACHED)
                )
        return detached

    def set_binding_status(
        self, binding_id: str, status: StudioBindingStatus
    ) -> StudioBindingRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM studio_bindings WHERE binding_id = ?", (binding_id,)
            ).fetchone()
            if row is None:
                raise StudioBindingNotFoundError(
                    f"Studio binding '{binding_id}' was not found"
                )
            connection.execute(
                "UPDATE studio_bindings SET status = ?, updated_at = ? WHERE binding_id = ?",
                (status.value, self._now(), binding_id),
            )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM studio_bindings WHERE binding_id = ?", (binding_id,)
            ).fetchone()
        assert row is not None
        return self._decode_binding(row)


    def has_project_flow(self, flow_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM project_flows WHERE flow_id = ? LIMIT 1", (flow_id,)
            ).fetchone()
        return row is not None

    def list_all_bindings(self) -> list[StudioBindingRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM studio_bindings ORDER BY created_at, binding_id"
            ).fetchall()
        return [self._decode_binding(row) for row in rows]

    def list_bindings(self, project_id: str) -> list[StudioBindingRecord]:
        self.get_project(project_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM studio_bindings
                 WHERE project_id = ?
                 ORDER BY created_at, binding_id
                """,
                (project_id,),
            ).fetchall()
        return [self._decode_binding(row) for row in rows]
