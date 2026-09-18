"""Postgres-backed HA integration tests.

Skipped unless DCT_HUB_TEST_PG_DSN is set — they need a real Postgres (CI
service container, or a local instance). Everything here exercises multi-node
semantics against the actual database: single-flight via the partial unique
index, SKIP LOCKED claiming, scoped interrupts, the stale-job reaper, the
shared rate limiter, advisory-lock leadership, and cross-node queue waiting.
"""

import asyncio
import os
from contextlib import asynccontextmanager

import pytest

from dct_hub.queue import RenderQueue
from dct_hub.store import JobRecord, RenderRecord
from dct_hub.store_pg import PgRateLimiter, PgStore

DSN = os.environ.get("DCT_HUB_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set DCT_HUB_TEST_PG_DSN to run Postgres integration tests")


@asynccontextmanager
async def pgstore():
    store = PgStore(DSN)
    await store.connect()
    async with store.pool.acquire() as con:
        await con.execute("TRUNCATE renders, jobs, rate_hits")
    try:
        yield store
    finally:
        await store.close()


def make_job(key, job_id="job1", status="queued", claimed_by=None, started_at=None):
    return JobRecord(
        id=job_id,
        key=key,
        board="sales_daily",
        variables={},
        format="html",
        status=status,
        mode="auto",
        error=None,
        requested_by="test",
        created_at="2026-09-18T00:00:00+00:00",
        started_at=started_at,
        finished_at=None,
        duration_ms=None,
        claimed_by=claimed_by,
    )


def make_record(key):
    return RenderRecord(
        key=key,
        board="sales_daily",
        variables={},
        format="html",
        artifact_path=f"sales_daily/{key}.html",
        status="ok",
        error=None,
        duration_ms=5,
        rendered_at="2026-09-18T00:00:00+00:00",
        dct_version="0.8.0",
    )


def test_render_roundtrip():
    async def main():
        async with pgstore() as store:
            assert await store.get("k1") is None
            await store.put(make_record("k1"))
            record = await store.get("k1")
            assert record.board == "sales_daily"
            assert [r.key for r in await store.list_renders("sales_daily")] == ["k1"]
            assert (await store.latest_renders())["sales_daily"].key == "k1"
            assert [r.key for r in await store.all_renders()] == ["k1"]
            assert await store.delete_render("k1") == "sales_daily/k1.html"
            assert await store.get("k1") is None

    asyncio.run(main())


def test_submit_coalesces_across_nodes():
    async def main():
        async with pgstore() as node_a, pgstore() as node_b:
            job, created = await node_a.submit_job(make_job("k1", job_id="a1"))
            assert created is True
            dup, created = await node_b.submit_job(make_job("k1", job_id="b1"))
            assert created is False
            assert dup.id == "a1"  # coalesced onto the existing active job

    asyncio.run(main())


def test_claim_skip_locked_gives_distinct_jobs():
    async def main():
        async with pgstore() as node_a, pgstore() as node_b:
            await node_a.submit_job(make_job("k1", job_id="j1"))
            await node_a.submit_job(make_job("k2", job_id="j2"))
            first = await node_a.claim_next_job("nodeA")
            second = await node_b.claim_next_job("nodeB")
            assert {first.id, second.id} == {"j1", "j2"}
            assert first.claimed_by == "nodeA"
            assert second.claimed_by == "nodeB"
            assert await node_a.claim_next_job("nodeA") is None

    asyncio.run(main())


def test_scoped_interrupt_only_hits_own_node():
    async def main():
        async with pgstore() as node_a, pgstore() as node_b:
            await node_a.submit_job(make_job("k1", job_id="j1"))
            await node_a.claim_next_job("nodeA")
            assert await node_b.interrupt_jobs("nodeB") == 0
            assert (await node_a.get_job("j1")).status == "running"
            assert await node_a.interrupt_jobs("nodeA") == 1
            assert (await node_a.get_job("j1")).status == "interrupted"

    asyncio.run(main())


def test_reap_stale_jobs():
    async def main():
        async with pgstore() as store:
            await store.submit_job(make_job("k1", job_id="old", status="running",
                                            claimed_by="dead-node", started_at="2026-09-18T00:00:00+00:00"))
            await store.submit_job(make_job("k2", job_id="fresh", status="running",
                                            claimed_by="live-node", started_at="2999-01-01T00:00:00+00:00"))
            assert await store.reap_stale_jobs(3600) == 1
            assert (await store.get_job("old")).status == "interrupted"
            assert (await store.get_job("fresh")).status == "running"

    asyncio.run(main())


def test_rate_limiter_shared_across_nodes():
    async def main():
        async with pgstore() as node_a, pgstore() as node_b:
            limit_a = PgRateLimiter(node_a, 2)
            limit_b = PgRateLimiter(node_b, 2)
            assert await limit_a.allow("ci") is True
            assert await limit_b.allow("ci") is True  # second hit, seen cluster-wide
            assert await limit_a.allow("ci") is False
            assert await limit_b.allow("other") is True

    asyncio.run(main())


def test_leadership_is_exclusive():
    async def main():
        async with pgstore() as node_a, pgstore() as node_b:
            assert await node_a.try_leader() is True
            assert await node_b.try_leader() is False
            await node_a.release_leader()
            assert await node_b.try_leader() is True
            await node_b.release_leader()

    asyncio.run(main())


def test_wait_observes_job_finished_by_another_node():
    async def main():
        async with pgstore() as node_a, pgstore() as node_b:
            queue = RenderQueue(node_a, 1, lambda job: None)
            queue._started = True  # no local workers: the job is finished "elsewhere"
            queue._wake = asyncio.Event()
            job, _ = await node_a.submit_job(make_job("k1", job_id="j1"))

            async def finisher():
                await asyncio.sleep(0.3)
                claimed = await node_b.claim_next_job("nodeB")
                await node_b.finish_job(claimed.id, "done", duration_ms=1)

            done, _ = await asyncio.gather(queue.wait(job.id, timeout_s=5), finisher())
            assert done.status == "done"

    asyncio.run(main())
