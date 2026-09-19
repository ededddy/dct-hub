"""Postgres metadata store: the HA backend (many replicas, one database).

Same interface as LocalStore, plus the multi-node primitives:

- single-flight is DB-enforced by a partial unique index — an insert conflict
  on an active key means "coalesce onto the active job";
- workers claim with FOR UPDATE SKIP LOCKED, stamping `claimed_by` with their
  node id, so a crashed replica's jobs are distinguishable from healthy ones;
- `try_leader`/`release_leader` (advisory lock) elect the janitor;
- `reap_stale_jobs` turns dead replicas' running jobs into `interrupted`;
- `PgRateLimiter` shares the per-identity render cap across replicas.

Timestamps stay ISO text (schema parity with LocalStore; lexicographic
compare is valid for the fixed-format strings `utcnow_iso` emits).
"""

import json
from datetime import datetime, timedelta, timezone

from .store import JobRecord, RenderRecord, utcnow_iso

try:
    import asyncpg
except ImportError:  # local installs without the ha extra
    asyncpg = None

# Advisory-lock keys (arbitrary stable constants, distinct per purpose).
_DDL_LOCK_KEY = 72721601
_JANITOR_LOCK_KEY = 72721602

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
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_per_key
    ON jobs (key) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs (status, created_at);
CREATE TABLE IF NOT EXISTS rate_hits (
    identity TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS rate_hits_identity_ts ON rate_hits (identity, ts);
"""

_JOB_COLS = "id, key, board, vars_json, format, status, mode, error, requested_by, created_at, started_at, finished_at, duration_ms, claimed_by"
_RENDER_COLS = "key, board, vars_json, format, artifact_path, status, error, duration_ms, rendered_at, dct_version"


class PgStore:
    def __init__(self, dsn: str):
        if asyncpg is None:
            raise RuntimeError("HA storage requires the ha extra: pip install 'dct-hub[ha]'")
        self.dsn = dsn
        self.pool = None
        self._leader_con = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=4)
        async with self.pool.acquire() as con:
            # DDL under a lock so simultaneous replica boots can't race it.
            await con.execute("SELECT pg_advisory_lock($1)", _DDL_LOCK_KEY)
            try:
                await con.execute(_SCHEMA)
            finally:
                await con.execute("SELECT pg_advisory_unlock($1)", _DDL_LOCK_KEY)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    # --- renders ---

    async def get(self, key: str) -> RenderRecord | None:
        row = await self.pool.fetchrow(f"SELECT {_RENDER_COLS} FROM renders WHERE key = $1", key)
        return self._to_record(row) if row else None

    async def put(self, record: RenderRecord) -> None:

        await self.pool.execute(
            f"INSERT INTO renders ({_RENDER_COLS}) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) "
            "ON CONFLICT (key) DO UPDATE SET board=$2, vars_json=$3, format=$4, artifact_path=$5, "
            "status=$6, error=$7, duration_ms=$8, rendered_at=$9, dct_version=$10",
            record.key, record.board, json.dumps(record.variables), record.format, record.artifact_path,
            record.status, record.error, record.duration_ms, record.rendered_at, record.dct_version,
        )

    async def delete_render(self, key: str) -> str | None:
        """Delete the metadata row, returning the blob locator for the caller
        to remove from the blob store (row-first delete ordering)."""
        return await self.pool.fetchval("DELETE FROM renders WHERE key = $1 RETURNING artifact_path", key)

    async def list_renders(self, board: str, limit: int = 10) -> list[RenderRecord]:
        rows = await self.pool.fetch(
            f"SELECT {_RENDER_COLS} FROM renders WHERE board = $1 AND status = 'ok' "
            "ORDER BY rendered_at DESC, key DESC LIMIT $2",
            board, limit,
        )
        return [self._to_record(row) for row in rows]

    async def latest_renders(self) -> dict[str, RenderRecord]:
        rows = await self.pool.fetch(
            f"""
            SELECT {", ".join(f"r.{c}" for c in _RENDER_COLS.split(", "))} FROM renders r
            JOIN (SELECT board, MAX(rendered_at) AS m FROM renders WHERE status = 'ok' GROUP BY board) t
              ON r.board = t.board AND r.rendered_at = t.m
            """
        )
        return {row["board"]: self._to_record(row) for row in rows}

    async def all_renders(self) -> list[RenderRecord]:
        rows = await self.pool.fetch(f"SELECT {_RENDER_COLS} FROM renders WHERE status = 'ok' ORDER BY rendered_at DESC")
        return [self._to_record(row) for row in rows]

    def _to_record(self, row) -> RenderRecord:

        return RenderRecord(
            key=row["key"],
            board=row["board"],
            variables=json.loads(row["vars_json"]),
            format=row["format"],
            artifact_path=row["artifact_path"],
            status=row["status"],
            error=row["error"],
            duration_ms=row["duration_ms"],
            rendered_at=row["rendered_at"],
            dct_version=row["dct_version"],
        )

    # --- render jobs ---

    async def submit_job(self, job: JobRecord) -> tuple[JobRecord, bool]:
        """Insert the job, or coalesce onto the active job for its key — the
        partial unique index makes this race-free across replicas."""

        try:
            await self.pool.execute(
                f"INSERT INTO jobs ({_JOB_COLS}) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
                job.id, job.key, job.board, json.dumps(job.variables), job.format, job.status, job.mode,
                job.error, job.requested_by, job.created_at, job.started_at, job.finished_at,
                job.duration_ms, job.claimed_by,
            )
            return job, True
        except asyncpg.UniqueViolationError:
            active = await self.active_job_for_key(job.key)
            if active is not None:
                return active, False
            # The winner finished between our insert and this select: its
            # artifact is fresh, so coalescing onto the finished job is right.
            latest = await self.pool.fetchrow(
                f"SELECT {_JOB_COLS} FROM jobs WHERE key = $1 ORDER BY created_at DESC, id DESC LIMIT 1", job.key
            )
            return self._to_job(latest), False

    async def get_job(self, job_id: str) -> JobRecord | None:
        row = await self.pool.fetchrow(f"SELECT {_JOB_COLS} FROM jobs WHERE id = $1", job_id)
        return self._to_job(row) if row else None

    async def active_job_for_key(self, key: str) -> JobRecord | None:
        row = await self.pool.fetchrow(
            f"SELECT {_JOB_COLS} FROM jobs WHERE key = $1 AND status IN ('queued', 'running') "
            "ORDER BY created_at LIMIT 1",
            key,
        )
        return self._to_job(row) if row else None

    async def claim_next_job(self, claimed_by: str) -> JobRecord | None:
        row = await self.pool.fetchrow(
            f"""
            UPDATE jobs SET status = 'running', started_at = $1, claimed_by = $2
            WHERE id = (
                SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at
                LIMIT 1 FOR UPDATE SKIP LOCKED
            )
            RETURNING {_JOB_COLS}
            """,
            utcnow_iso(), claimed_by,
        )
        return self._to_job(row) if row else None

    async def finish_job(self, job_id: str, status: str, error: str | None = None, duration_ms: int | None = None) -> None:
        await self.pool.execute(
            "UPDATE jobs SET status = $1, error = $2, duration_ms = $3, finished_at = $4 WHERE id = $5",
            status, error, duration_ms, utcnow_iso(), job_id,
        )

    async def interrupt_jobs(self, claimed_by: str | None = None) -> int:
        """Mark active jobs interrupted. None = all (single-node boot); a node
        id = only that node's (container crash-restart in HA)."""
        if claimed_by is None:
            result = await self.pool.execute(
                "UPDATE jobs SET status = 'interrupted', finished_at = $1 WHERE status IN ('queued', 'running')",
                utcnow_iso(),
            )
        else:
            result = await self.pool.execute(
                "UPDATE jobs SET status = 'interrupted', finished_at = $1 "
                "WHERE status IN ('queued', 'running') AND claimed_by = $2",
                utcnow_iso(), claimed_by,
            )
        return int(result.split()[-1])

    async def reap_stale_jobs(self, stale_after_s: int) -> int:
        """Interrupt running jobs whose starter is presumed dead (render
        timeout + margin exceeded). Queued jobs are unclaimed work — any
        worker can still take them, so they're never stale."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after_s)).isoformat(timespec="seconds")
        result = await self.pool.execute(
            "UPDATE jobs SET status = 'interrupted', finished_at = $1 WHERE status = 'running' AND started_at < $2",
            utcnow_iso(), cutoff,
        )
        return int(result.split()[-1])

    async def list_jobs(self, limit: int = 50) -> list[JobRecord]:
        limit = max(1, min(int(limit), 1000))
        rows = await self.pool.fetch(
            f"SELECT {_JOB_COLS} FROM jobs ORDER BY created_at DESC, id DESC LIMIT $1", limit
        )
        return [self._to_job(row) for row in rows]

    async def last_activity(self, key: str) -> str | None:
        return await self.pool.fetchval("SELECT MAX(created_at) FROM jobs WHERE key = $1", key)

    def _to_job(self, row) -> JobRecord:

        return JobRecord(
            id=row["id"],
            key=row["key"],
            board=row["board"],
            variables=json.loads(row["vars_json"]),
            format=row["format"],
            status=row["status"],
            mode=row["mode"],
            error=row["error"],
            requested_by=row["requested_by"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            duration_ms=row["duration_ms"],
            claimed_by=row["claimed_by"],
        )

    # --- leadership (janitor) ---

    async def try_leader(self) -> bool:
        """Take the janitor advisory lock on a held connection. The lock is
        session-scoped: if the leader replica dies, Postgres releases it."""
        if self._leader_con is not None:
            return True  # already leading
        con = await self.pool.acquire()
        try:
            got = await con.fetchval("SELECT pg_try_advisory_lock($1)", _JANITOR_LOCK_KEY)
        except Exception:
            await self.pool.release(con)
            raise
        if got:
            self._leader_con = con
        else:
            await self.pool.release(con)
        return bool(got)

    async def release_leader(self) -> None:
        if self._leader_con is None:
            return
        con, self._leader_con = self._leader_con, None
        try:
            await con.fetchval("SELECT pg_advisory_unlock($1)", _JANITOR_LOCK_KEY)
        finally:
            await self.pool.release(con)

    async def prune_rate_hits(self) -> None:
        await self.pool.execute("DELETE FROM rate_hits WHERE ts < now() - interval '60 seconds'")


class PgRateLimiter:
    """Per-identity render cap shared across replicas (D16's abuse cap, now
    cluster-wide). Approximate under concurrency — count-then-insert across
    nodes can overshoot by a little; acceptable for an abuse cap."""

    def __init__(self, store: PgStore, limit_per_minute: int):
        self.store = store
        self.limit = limit_per_minute

    async def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        async with self.store.pool.acquire() as con:
            async with con.transaction():
                await con.execute("DELETE FROM rate_hits WHERE identity = $1 AND ts < now() - interval '60 seconds'", key)
                hits = await con.fetchval(
                    "SELECT COUNT(*) FROM rate_hits WHERE identity = $1 AND ts > now() - interval '60 seconds'", key
                )
                if hits >= self.limit:
                    return False
                await con.execute("INSERT INTO rate_hits (identity) VALUES ($1)", key)
                return True
