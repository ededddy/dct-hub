"""Async render queue: single-flight submission plus a small worker pool.

Jobs live in the metadata store for auditability; workers are in-process
asyncio tasks, started lazily on first submission so tests and CLI use need no
lifespan management. Single-flight is enforced by the store's `submit_job`
(lock locally, a partial unique index in Postgres), so coalescing holds across
replicas in HA mode. `wait` pairs the local done-event with a slow poll, so a
caller can await a job that another replica executes.
"""

import asyncio
import os
import socket
import time
import uuid
from typing import Awaitable, Callable

from .store import JobRecord, utcnow_iso

TERMINAL_STATUSES = {"done", "error", "interrupted"}

# Poll cadence for cross-node progress: `wait` when the job runs on another
# replica, and the worker idle loop when submissions arrive on other replicas.
WAIT_POLL_S = 0.5


class RenderQueue:
    def __init__(
        self,
        store,
        concurrency: int,
        execute: Callable[[JobRecord], Awaitable[object]],
        node_id: str | None = None,
        single_node: bool = True,
        idle_poll_s: float = 5.0,
    ):
        self.store = store
        self.concurrency = max(1, concurrency)
        self._execute = execute
        self.node_id = node_id or f"{socket.gethostname()}:{os.getpid()}"
        self.single_node = single_node
        self.idle_poll_s = idle_poll_s
        self._started = False
        self._wake: asyncio.Event | None = None
        self._done: dict[str, asyncio.Event] = {}

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        # Crash-restart: a single-node boot interrupts everything active (its
        # owner is gone); an HA boot only interrupts its own node id, never
        # jobs healthy replicas are running.
        await self.store.interrupt_jobs(None if self.single_node else self.node_id)
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
        await self.start()
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
        job, created = await self.store.submit_job(job)
        self._done.setdefault(job.id, asyncio.Event())
        if created and self._wake is not None:
            self._wake.set()
        return job, created

    async def wait(self, job_id: str, timeout_s: float) -> JobRecord | None:
        await self.start()
        deadline = time.monotonic() + timeout_s
        job = await self.store.get_job(job_id)
        while job is not None and job.status not in TERMINAL_STATUSES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            event = self._done.setdefault(job_id, asyncio.Event())
            try:
                # The event fires instantly when a local worker finishes the
                # job; the timeout is the poll that catches jobs finished by
                # other replicas.
                await asyncio.wait_for(event.wait(), min(WAIT_POLL_S, remaining))
            except asyncio.TimeoutError:
                pass
            job = await self.store.get_job(job_id)
        return job

    async def _worker(self) -> None:
        assert self._wake is not None
        while True:
            job = await self.store.claim_next_job(self.node_id)
            if job is None:
                self._wake.clear()
                # A submission landing between the failed claim and clear() is
                # picked up after the idle poll — bounded delay, and far
                # better than claiming a job nobody executes.
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.idle_poll_s)
                except asyncio.TimeoutError:
                    pass
                continue
            started = time.monotonic()
            try:
                await self._execute(job)
                await self.store.finish_job(job.id, "done", duration_ms=int((time.monotonic() - started) * 1000))
            except Exception as exc:
                detail = getattr(exc, "stderr", None) or str(exc)
                await self.store.finish_job(job.id, "error", error=detail[-2000:], duration_ms=int((time.monotonic() - started) * 1000))
            finally:
                event = self._done.pop(job.id, None)
                if event is not None:
                    event.set()
