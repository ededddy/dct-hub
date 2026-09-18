"""Render/job metadata storage. Two backends share one async interface:

- `LocalStore` (this module): SQLite in the state dir — single-node default.
- `PgStore` (store_pg.py): PostgreSQL — HA mode, many replicas.

Blob bytes live in a separate blob store (blobs.py); the `artifact_path`
column holds the blob locator. Publish order is blob-then-row; delete is
row-then-blob, so a renders row always implies its blob exists.
"""

import json
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
    artifact_path: str  # blob locator
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
    duration_ms INTEGER,
    claimed_by TEXT
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
    claimed_by: str | None = None


class LocalStore:
    """Single-node store: SQLite metadata, sync driver under an async surface.

    The sync bodies are safe on the event loop: SQLite operations here are
    microseconds, and single-flight/job-claim correctness comes from the
    check-then-write running entirely inside `self._lock` on one loop.
    """

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(root / "meta.db", check_same_thread=False)
        with self._db:
            self._db.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        # Pre-HA schemas lack jobs.claimed_by; dev state dirs survive upgrades.
        cols = {row[1] for row in self._db.execute("PRAGMA table_info(jobs)")}
        if "claimed_by" not in cols:
            with self._db:
                self._db.execute("ALTER TABLE jobs ADD COLUMN claimed_by TEXT")

    async def close(self) -> None:
        self._db.close()

    # --- renders ---

    async def get(self, key: str) -> RenderRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM renders WHERE key = ?", (key,)).fetchone()
        return self._to_record(row) if row else None

    async def put(self, record: RenderRecord) -> None:
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

    async def delete_render(self, key: str) -> str | None:
        """Delete the metadata row, returning the blob locator for the caller
        to remove from the blob store (row-first delete ordering)."""
        with self._lock, self._db:
            row = self._db.execute("SELECT artifact_path FROM renders WHERE key = ?", (key,)).fetchone()
            self._db.execute("DELETE FROM renders WHERE key = ?", (key,))
        return row[0] if row else None

    async def list_renders(self, board: str, limit: int = 10) -> list[RenderRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM renders WHERE board = ? AND status = 'ok' ORDER BY rendered_at DESC, rowid DESC LIMIT ?",
                (board, limit),
            ).fetchall()
        return [self._to_record(row) for row in rows]

    async def latest_renders(self) -> dict[str, RenderRecord]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT r.* FROM renders r
                JOIN (SELECT board, MAX(rendered_at) AS m FROM renders WHERE status = 'ok' GROUP BY board) t
                  ON r.board = t.board AND r.rendered_at = t.m
                """
            ).fetchall()
        return {row[1]: self._to_record(row) for row in rows}

    async def all_renders(self) -> list[RenderRecord]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM renders WHERE status = 'ok' ORDER BY rendered_at DESC").fetchall()
        return [self._to_record(row) for row in rows]

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

    async def submit_job(self, job: JobRecord) -> tuple[JobRecord, bool]:
        """Insert the job, or coalesce onto the active job for its key.
        Returns (job, created)."""
        with self._lock, self._db:
            active = self._db.execute(
                "SELECT * FROM jobs WHERE key = ? AND status IN ('queued', 'running') ORDER BY created_at LIMIT 1",
                (job.key,),
            ).fetchone()
            if active is not None:
                return self._to_job(active), False
            self._db.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    job.claimed_by,
                ),
            )
            return job, True

    async def get_job(self, job_id: str) -> JobRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._to_job(row) if row else None

    async def active_job_for_key(self, key: str) -> JobRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM jobs WHERE key = ? AND status IN ('queued', 'running') ORDER BY created_at LIMIT 1",
                (key,),
            ).fetchone()
        return self._to_job(row) if row else None

    async def claim_next_job(self, claimed_by: str) -> JobRecord | None:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                "UPDATE jobs SET status = 'running', started_at = ?, claimed_by = ? WHERE id = ? AND status = 'queued'",
                (utcnow_iso(), claimed_by, row[0]),
            )
            return self._to_job(row)

    async def finish_job(self, job_id: str, status: str, error: str | None = None, duration_ms: int | None = None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE jobs SET status = ?, error = ?, duration_ms = ?, finished_at = ? WHERE id = ?",
                (status, error, duration_ms, utcnow_iso(), job_id),
            )

    async def interrupt_jobs(self, claimed_by: str | None = None) -> int:
        """Mark active jobs interrupted. None = all (single-node boot); a node
        id = only that node's (container crash-restart in HA)."""
        with self._lock, self._db:
            if claimed_by is None:
                cursor = self._db.execute(
                    "UPDATE jobs SET status = 'interrupted', finished_at = ? WHERE status IN ('queued', 'running')",
                    (utcnow_iso(),),
                )
            else:
                cursor = self._db.execute(
                    "UPDATE jobs SET status = 'interrupted', finished_at = ? WHERE status IN ('queued', 'running') AND claimed_by = ?",
                    (utcnow_iso(), claimed_by),
                )
            return cursor.rowcount

    async def list_jobs(self, limit: int = 50) -> list[JobRecord]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        return [self._to_job(row) for row in rows]

    async def last_activity(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT MAX(created_at) FROM jobs WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

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
            claimed_by=row[13],
        )


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
