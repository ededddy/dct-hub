import asyncio
import time
from pathlib import Path

from fastapi.testclient import TestClient

from dct_hub.api import create_app
from dct_hub.config import Config
from dct_hub.dct import DctError, RenderResult


class FakeDct:
    def __init__(self, sleep_s: float = 0.0, fail: bool = False):
        self.sleep_s = sleep_s
        self.fail = fail
        self.renders = []

    async def version(self):
        return "0.8.0"

    async def render(self, board_file, variables, output, fmt):
        if self.sleep_s:
            await asyncio.sleep(self.sleep_s)
        if self.fail:
            raise DctError("boom", stderr="query exploded")
        self.renders.append((str(board_file), dict(variables), fmt))
        Path(output).write_text(f"<html>rendered {variables}</html>")
        return RenderResult(output_path=output, duration_ms=5)

    async def describe(self, board_file):
        return {"variables": [{"name": "region", "type": "input"}]}


def make_app(tmp_path, fake: FakeDct, **policy_overrides):
    charts = tmp_path / "proj" / "charts"
    charts.mkdir(parents=True)
    (charts / "sales_daily.yml").write_text("title: Daily sales report\n")
    config = Config(
        project_dir=tmp_path / "proj",
        storage={"dir": tmp_path / ".hub"},
        policy=policy_overrides,
    )
    app = create_app(config)
    app.state.service.dct = fake
    return app


def wait_job(client, job_id, timeout_s=5) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error", "interrupted"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def test_async_flow(tmp_path):
    fake = FakeDct(sleep_s=0.3)
    with TestClient(make_app(tmp_path, fake)) as client:
        resp = client.post("/api/renders", json={"board": "sales_daily"})
        assert resp.status_code == 202
        assert resp.json()["outcome"] == "queued"
        job_id = resp.json()["job_id"]

        job = wait_job(client, job_id)
        assert job["status"] == "done"
        assert len(fake.renders) == 1
        assert client.get(f"/api/renders/{resp.json()['key']}").json()["status"] == "ok"


def test_single_flight_coalesces(tmp_path):
    fake = FakeDct(sleep_s=0.3)
    with TestClient(make_app(tmp_path, fake)) as client:
        first = client.post("/api/renders", json={"board": "sales_daily", "vars": {"region": "West"}})
        second = client.post("/api/renders", json={"board": "sales_daily", "vars": {"region": "West"}})
        assert first.json()["job_id"] == second.json()["job_id"]
        job = wait_job(client, first.json()["job_id"])
        assert job["status"] == "done"
        assert len(fake.renders) == 1


def test_wait_true_blocks_until_done(tmp_path):
    fake = FakeDct()
    with TestClient(make_app(tmp_path, fake)) as client:
        resp = client.post("/api/renders", json={"board": "sales_daily", "wait": True})
        assert resp.status_code == 200
        assert resp.json()["outcome"] == "done"
        assert len(fake.renders) == 1


def test_min_interval_rate_limits_stale_refresh(tmp_path):
    fake = FakeDct()
    with TestClient(make_app(tmp_path, fake, default_ttl_s=0, min_interval_s=300)) as client:
        first = client.post("/api/renders", json={"board": "sales_daily", "wait": True})
        assert first.json()["outcome"] == "done"

        # TTL is 0 so the artifact is stale, but min_interval blocks re-render
        second = client.post("/api/renders", json={"board": "sales_daily"})
        assert second.json()["outcome"] == "cached"
        assert second.json()["stale"] is True
        assert len(fake.renders) == 1

        forced = client.post("/api/renders", json={"board": "sales_daily", "force": True, "wait": True})
        assert forced.json()["outcome"] == "done"
        assert len(fake.renders) == 2


def test_stale_while_revalidate_on_view(tmp_path):
    fake = FakeDct()
    with TestClient(make_app(tmp_path, fake, default_ttl_s=0, min_interval_s=0)) as client:
        assert client.get("/raw/sales_daily").status_code == 200
        assert len(fake.renders) == 1

        # stale: served immediately, background refresh enqueued
        assert client.get("/raw/sales_daily").status_code == 200
        deadline = time.monotonic() + 5
        while len(fake.renders) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(fake.renders) == 2


def test_failed_render_marks_job_error(tmp_path):
    fake = FakeDct(fail=True)
    with TestClient(make_app(tmp_path, fake)) as client:
        resp = client.post("/api/renders", json={"board": "sales_daily", "wait": True})
        assert resp.status_code == 502
        jobs = client.get("/api/jobs").json()
        assert jobs[0]["status"] == "error"
        assert "query exploded" in jobs[0]["error"]
        # the failure is recorded but never served as an artifact
        assert client.get("/raw/sales_daily").status_code == 502


def test_failed_refresh_preserves_last_good_artifact(tmp_path):
    fake = FakeDct()
    with TestClient(make_app(tmp_path, fake, default_ttl_s=0, min_interval_s=0)) as client:
        assert client.post("/api/renders", json={"board": "sales_daily", "wait": True}).json()["outcome"] == "done"
        assert len(fake.renders) == 1

        fake.fail = True
        resp = client.post("/api/renders", json={"board": "sales_daily", "force": True, "wait": True})
        assert resp.status_code == 502
        # the previous good artifact is still served (background re-render fails harmlessly)
        assert client.get("/raw/sales_daily").status_code == 200
        assert len(fake.renders) == 1


def test_wait_returns_immediately_for_terminal_job(tmp_path):
    from dct_hub.queue import RenderQueue
    from dct_hub.store import ArtifactStore

    async def main():
        store = ArtifactStore(tmp_path / ".hub")

        async def instant(job):
            return None

        queue = RenderQueue(store, 1, instant)
        job, _ = await queue.submit(key="k", board="b", variables={}, fmt="html", mode="auto", requested_by="t")
        first = await queue.wait(job.id, timeout_s=5)
        assert first.status == "done"

        started = time.monotonic()
        again = await queue.wait(job.id, timeout_s=30)
        elapsed = time.monotonic() - started
        assert again.status == "done"
        assert elapsed < 2

    asyncio.run(main())


def test_interrupted_jobs_recovered_on_start(tmp_path):
    fake = FakeDct()
    app = make_app(tmp_path, fake)
    store = app.state.service.store
    store.create_job(
        __import__("dct_hub.store", fromlist=["JobRecord"]).JobRecord(
            id="stale1",
            key="somekey",
            board="sales_daily",
            variables={},
            format="html",
            status="running",
            mode="auto",
            error=None,
            requested_by="ghost",
            created_at="2026-09-17T00:00:00+00:00",
            started_at="2026-09-17T00:00:00+00:00",
            finished_at=None,
            duration_ms=None,
        )
    )
    with TestClient(app) as client:
        # first submission starts the queue, which interrupts stale jobs
        client.post("/api/renders", json={"board": "sales_daily", "wait": True})
        job = client.get("/api/jobs/stale1").json()
        assert job["status"] == "interrupted"
