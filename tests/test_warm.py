import asyncio
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from dct_hub.api import create_app
from dct_hub.config import Config
from dct_hub.dct import DctError, RenderResult


class FakeDct:
    def __init__(self):
        self.renders = []

    async def version(self):
        return "0.8.0"

    async def render(self, board_file, variables, output, fmt):
        self.renders.append((str(board_file), dict(variables), fmt))
        Path(output).write_text(f"<html>rendered {variables}</html>")
        return RenderResult(output_path=output, duration_ms=5)

    async def describe(self, board_file):
        return {
            "path": str(board_file),
            "title": "Fake",
            "variables": [
                {"name": "day", "type": "date", "default": "2026-09-16"},
                {"name": "region", "type": "radio", "options": ["East", "West"]},
            ],
        }


@contextmanager
def warm_client(tmp_path, warm, dct=None, **config_kwargs):
    charts = tmp_path / "proj" / "charts"
    charts.mkdir(parents=True)
    (charts / "sales_daily.yml").write_text("title: Daily sales report\n")
    config = Config(project_dir=tmp_path / "proj", storage={"dir": tmp_path / ".hub"}, warm=warm, **config_kwargs)
    app = create_app(config)
    fake = dct or FakeDct()
    app.state.service.dct = fake
    # context-managed: runs the lifespan (queue workers start at boot) and keeps
    # one event loop across requests, like uvicorn does in production
    with TestClient(app) as test_client:
        yield test_client, fake


def wait_job(client, job_id, timeout_s=5) -> dict:
    deadline = time.time() + timeout_s
    job = {}
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error", "interrupted"):
            return job
        time.sleep(0.05)
    return job


def test_warm_renders_then_cached(tmp_path):
    with warm_client(tmp_path, {"boards": ["sales_daily"]}) as (client, fake):
        first = client.post("/api/warm")
        assert first.status_code == 200
        assert first.json()["ok"] is True
        (result,) = first.json()["results"]
        assert result["board"] == "sales_daily"
        assert result["vars"] == {}
        assert result["outcome"] == "done"
        assert result["error"] is None
        assert len(fake.renders) == 1

        second = client.post("/api/warm")
        assert second.status_code == 200
        (result,) = second.json()["results"]
        assert result["outcome"] == "cached"
        assert result["key"] == first.json()["results"][0]["key"]
        assert len(fake.renders) == 1


def test_warm_explicit_var_combos(tmp_path):
    warm = {"boards": [{"board": "sales_daily", "vars": [{"region": "East"}, {"region": "West"}]}]}
    with warm_client(tmp_path, warm) as (client, fake):
        resp = client.post("/api/warm")
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert [r["vars"] for r in results] == [{"region": "East"}, {"region": "West"}]
        assert all(r["outcome"] == "done" for r in results)
        assert results[0]["key"] != results[1]["key"]
        assert len(fake.renders) == 2


def test_warm_unknown_board_fails_but_continues(tmp_path):
    with warm_client(tmp_path, {"boards": ["nope", "sales_daily"]}) as (client, fake):
        resp = client.post("/api/warm")
        assert resp.status_code == 502
        assert resp.json()["ok"] is False
        missing, warmed = resp.json()["results"]
        assert missing["board"] == "nope"
        assert missing["outcome"] == "error"
        assert "board not found" in missing["error"]
        assert warmed["outcome"] == "done"
        assert len(fake.renders) == 1


def test_warm_invalid_var_combo_fails(tmp_path):
    warm = {"boards": [{"board": "sales_daily", "vars": [{"region": "North"}]}]}
    with warm_client(tmp_path, warm) as (client, fake):
        resp = client.post("/api/warm")
        assert resp.status_code == 502
        (result,) = resp.json()["results"]
        assert result["outcome"] == "error"
        assert "must be one of" in result["error"]
        assert len(fake.renders) == 0


def test_warm_no_wait_queues(tmp_path):
    with warm_client(tmp_path, {"boards": ["sales_daily"]}) as (client, fake):
        resp = client.post("/api/warm", json={"wait": False})
        assert resp.status_code == 202
        (result,) = resp.json()["results"]
        assert result["outcome"] in ("queued", "running")
        assert result["job_id"]

        job = wait_job(client, result["job_id"])
        assert job["status"] == "done"
        assert len(fake.renders) == 1


def test_warm_empty_config_is_green_noop(tmp_path):
    with warm_client(tmp_path, {}) as (client, fake):
        resp = client.post("/api/warm")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "results": []}
        assert len(fake.renders) == 0


class SlowFakeDct(FakeDct):
    def __init__(self, sleep_s):
        super().__init__()
        self.sleep_s = sleep_s

    async def render(self, board_file, variables, output, fmt):
        await asyncio.sleep(self.sleep_s)
        return await super().render(board_file, variables, output, fmt)


def test_warm_renders_concurrently(tmp_path):
    # Two 0.6s renders on the default 2-worker pool: sequential submission
    # would take >= 1.2s wall time; submit-all-then-wait-all takes ~0.6s.
    warm = {"boards": [{"board": "sales_daily", "vars": [{"region": "East"}, {"region": "West"}]}]}
    with warm_client(tmp_path, warm, dct=SlowFakeDct(0.6)) as (client, fake):
        started = time.monotonic()
        resp = client.post("/api/warm")
        elapsed = time.monotonic() - started
        assert resp.status_code == 200
        assert all(r["outcome"] == "done" for r in resp.json()["results"])
        assert len(fake.renders) == 2
        assert elapsed < 1.0


class BrokenFakeDct(FakeDct):
    async def version(self):
        raise DctError("dct exited 1", stderr="spawn dct ENOENT")


def test_warm_broken_dct_is_per_board_error(tmp_path):
    with warm_client(tmp_path, {"boards": ["sales_daily"]}, dct=BrokenFakeDct()) as (client, _):
        resp = client.post("/api/warm")
        assert resp.status_code == 502
        (result,) = resp.json()["results"]
        assert result["outcome"] == "error"
        assert "dct exited 1" in result["error"]


AUTH = {
    "enabled": True,
    "session_secret": "test-secret",
    "service_tokens": [{"token": "ci-token", "name": "ci", "groups": ["data-ops"]}],
}
VIEW_ONLY = {"grants": [{"path": "*", "role": "viewer", "groups": ["data-ops"]}]}


def test_warm_requires_authentication(tmp_path):
    with warm_client(tmp_path, {"boards": ["sales_daily"]}, auth=AUTH, access=VIEW_ONLY) as (client, _):
        resp = client.post("/api/warm")
        assert resp.status_code == 401


def test_warm_per_board_refresh_grant(tmp_path):
    with warm_client(tmp_path, {"boards": ["sales_daily"]}, auth=AUTH, access=VIEW_ONLY) as (client, fake):
        resp = client.post("/api/warm", headers={"Authorization": "Bearer ci-token"})
        assert resp.status_code == 502
        (result,) = resp.json()["results"]
        assert result["outcome"] == "error"
        assert "lacks 'refresh'" in result["error"]
        assert len(fake.renders) == 0
