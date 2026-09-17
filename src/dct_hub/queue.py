"""Async render queue: single-flight submission plus a small worker pool.

Jobs live in SQLite for auditability; workers are in-process asyncio tasks,
started lazily on first submission so tests and CLI use need no lifespan
management. A submission for a key with an active job coalesces onto it.
Jobs left queued/running by a crashed process are marked interrupted at start.
"""

import asyncio
import time
import uuid
from typing import Awaitable, Callable

from .store import ArtifactStore, JobRecord, utcnow_iso

TERMINAL_STATUSES = {"done", "error", "interrupted"}


class RenderQueue:
    def __init__(self, store: ArtifactStore, concurrency: int, execute: Callable[[JobRecord], Awaitable[object]]):
        self.store = store
        self.concurrency = max(1, concurrency)
        self._execute = execute
        self._started = False
        self._wake: asyncio.Event | None = None
        self._done: dict[str, asyncio.Event] = {}

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.store.interrupt_active_jobs()
        self._wake = asyncio.Event()
        for _ in range(self.concurrency):
            asyncio.create_task(self._worker())

    async def submit(
        self,
        key: str,
        board: str,
        variables: dict[str, str],
        fmt: str,
        mode: str,
        requested_by: str,
    ) -> tuple[JobRecord, bool]:
        self.start()
        active = self.store.active_job_for_key(key)
        if active is not None:
            self._done.setdefault(active.id, asyncio.Event())
            return active, False
        job = JobRecord(
            id=uuid.uuid4().hex[:16],
            key=key,
            board=board,
            variables=variables,
            format=fmt,
            status="queued",
            mode=mode,
            error=None,
            requested_by=requested_by,
            created_at=utcnow_iso(),
            started_at=None,
            finished_at=None,
            duration_ms=None,
        )
        self.store.create_job(job)
        self._done[job.id] = asyncio.Event()
        self._wake.set()
        return job, True

    async def wait(self, job_id: str, timeout_s: float) -> JobRecord | None:
        self.start()
        job = self.store.get_job(job_id)
        if job is None or job.status in TERMINAL_STATUSES:
            return job  # fast renders can finish before anyone waits
        event = self._done.setdefault(job_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout_s)
        except asyncio.TimeoutError:
            pass
        return self.store.get_job(job_id)

    async def _worker(self) -> None:
        assert self._wake is not None
        while True:
            job = self.store.claim_next_job()
            if job is None:
                self._wake.clear()
                if self.store.claim_next_job() is not None:
                    continue  # raced a submission between clear and claim
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue
            started = time.monotonic()
            try:
                await self._execute(job)
                self.store.finish_job(job.id, "done", duration_ms=int((time.monotonic() - started) * 1000))
            except Exception as exc:
                detail = getattr(exc, "stderr", None) or str(exc)
                self.store.finish_job(job.id, "error", error=detail[-2000:], duration_ms=int((time.monotonic() - started) * 1000))
            finally:
                event = self._done.pop(job.id, None)
                if event is not None:
                    event.set()
