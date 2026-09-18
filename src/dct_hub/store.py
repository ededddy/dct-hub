"""Artifact storage: rendered files on disk, render metadata in SQLite."""

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class RenderRecord:
    key: str
    board: str
    variables: dict[str, str]
    format: str
    artifact_path: str
    status: str  # "ok" | "error"
    error: str | None
    duration_ms: int | None
    rendered_at: str
    dct_version: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS renders (
    key TEXT PRIMARY KEY,
    board TEXT NOT NULL,
    vars_json TEXT NOT NULL,
    format TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    duration_ms INTEGER,
    rendered_at TEXT NOT NULL,
    dct_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    board TEXT NOT NULL,
    vars_json TEXT NOT NULL,
    format TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    error TEXT,
    requested_by TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_key_status ON jobs (key, status);
"""


@dataclass
class JobRecord:
    id: str
    key: str
    board: str
    variables: dict[str, str]
    format: str
    status: str  # "queued" | "running" | "done" | "error" | "interrupted"
    mode: str  # "auto" | "force"
    error: str | None
    requested_by: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    duration_ms: int | None


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root
        self.artifacts_dir = root / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(root / "meta.db", check_same_thread=False)
        with self._db:
            self._db.executescript(_SCHEMA)

    def new_artifact_path(self, board: str, key: str, fmt: str) -> Path:
        if not re.fullmatch(r"[a-z0-9]+", fmt):
            raise ValueError(f"unsafe artifact format: {fmt!r}")
        path = self.artifacts_dir / board / f"{key}.{fmt}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def get(self, key: str) -> RenderRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM renders WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return self._to_record(row)

    def put(self, record: RenderRecord) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO renders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.key,
                    record.board,
                    json.dumps(record.variables),
                    record.format,
                    record.artifact_path,
                    record.status,
                    record.error,
                    record.duration_ms,
                    record.rendered_at,
                    record.dct_version,
                ),
            )

    def _to_record(self, row: tuple) -> RenderRecord:
        return RenderRecord(
            key=row[0],
            board=row[1],
            variables=json.loads(row[2]),
            format=row[3],
            artifact_path=row[4],
            status=row[5],
            error=row[6],
            duration_ms=row[7],
            rendered_at=row[8],
            dct_version=row[9],
        )

    # --- render jobs ---

    def create_job(self, job: JobRecord) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.id,
                    job.key,
                    job.board,
                    json.dumps(job.variables),
                    job.format,
                    job.status,
                    job.mode,
                    job.error,
                    job.requested_by,
                    job.created_at,
                    job.started_at,
                    job.finished_at,
                    job.duration_ms,
                ),
            )

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._to_job(row) if row else None

    def active_job_for_key(self, key: str) -> JobRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM jobs WHERE key = ? AND status IN ('queued', 'running') ORDER BY created_at LIMIT 1",
                (key,),
            ).fetchone()
        return self._to_job(row) if row else None

    def claim_next_job(self) -> JobRecord | None:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
                (utcnow_iso(), row[0]),
            )
            return self._to_job(row)

    def finish_job(self, job_id: str, status: str, error: str | None = None, duration_ms: int | None = None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE jobs SET status = ?, error = ?, duration_ms = ?, finished_at = ? WHERE id = ?",
                (status, error, duration_ms, utcnow_iso(), job_id),
            )

    def interrupt_active_jobs(self) -> int:
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE jobs SET status = 'interrupted', finished_at = ? WHERE status IN ('queued', 'running')",
                (utcnow_iso(),),
            )
            return cursor.rowcount

    def list_jobs(self, limit: int = 50) -> list[JobRecord]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        return [self._to_job(row) for row in rows]

    def last_activity(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT MAX(created_at) FROM jobs WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def list_renders(self, board: str, limit: int = 10) -> list[RenderRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM renders WHERE board = ? AND status = 'ok' ORDER BY rendered_at DESC, rowid DESC LIMIT ?",
                (board, limit),
            ).fetchall()
        return [self._to_record(row) for row in rows]

    def latest_renders(self) -> dict[str, RenderRecord]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT r.* FROM renders r
                JOIN (SELECT board, MAX(rendered_at) AS m FROM renders WHERE status = 'ok' GROUP BY board) t
                  ON r.board = t.board AND r.rendered_at = t.m
                """
            ).fetchall()
        return {row[1]: self._to_record(row) for row in rows}

    def all_renders(self) -> list[RenderRecord]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM renders WHERE status = 'ok' ORDER BY rendered_at DESC").fetchall()
        return [self._to_record(row) for row in rows]

    def delete_render(self, key: str) -> None:
        with self._lock, self._db:
            row = self._db.execute("SELECT artifact_path FROM renders WHERE key = ?", (key,)).fetchone()
            self._db.execute("DELETE FROM renders WHERE key = ?", (key,))
        if row:
            Path(row[0]).unlink(missing_ok=True)

    def _to_job(self, row: tuple) -> JobRecord:
        return JobRecord(
            id=row[0],
            key=row[1],
            board=row[2],
            variables=json.loads(row[3]),
            format=row[4],
            status=row[5],
            mode=row[6],
            error=row[7],
            requested_by=row[8],
            created_at=row[9],
            started_at=row[10],
            finished_at=row[11],
            duration_ms=row[12],
        )


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
