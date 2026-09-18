import asyncio
import time
from pathlib import Path

from fastapi.testclient import TestClient

from dct_hub.api import create_app
from dct_hub.config import Config
from dct_hub.dct import DctError, RenderResult
from dct_hub.ratelimit import RateLimiter


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
        return {"variables": []}


def make_client(tmp_path, **policy):
    charts = tmp_path / "proj" / "charts"
    charts.mkdir(parents=True)
    (charts / "sales_daily.yml").write_text("title: Daily sales report\n")
    config = Config(project_dir=tmp_path / "proj", storage={"dir": tmp_path / ".hub"}, policy=policy)
    app = create_app(config)
    fake = FakeDct()
    app.state.service.dct = fake
    return TestClient(app), fake


def test_limiter_sliding_window():
    limiter = RateLimiter(2)
    assert asyncio.run(limiter.allow("alice")) is True
    assert asyncio.run(limiter.allow("alice")) is True
    assert asyncio.run(limiter.allow("alice")) is False
    assert asyncio.run(limiter.allow("bob")) is True  # windows are per identity

    unlimited = RateLimiter(0)
    for _ in range(50):
        assert asyncio.run(unlimited.allow("alice")) is True


def test_render_rate_limit_429(tmp_path):
    client, fake = make_client(tmp_path, max_renders_per_minute=2)
    with client:
        first = client.post("/api/renders", json={"board": "sales_daily", "wait": True})
        assert first.status_code == 200
        second = client.post("/api/renders", json={"board": "sales_daily", "force": True, "wait": True})
        assert second.status_code == 200
        third = client.post("/api/renders", json={"board": "sales_daily", "force": True})
        assert third.status_code == 429
        assert len(fake.renders) == 2


def test_rate_limit_default_unlimited(tmp_path):
    client, fake = make_client(tmp_path)
    with client:
        for _ in range(4):
            resp = client.post("/api/renders", json={"board": "sales_daily", "force": True, "wait": True})
            assert resp.status_code == 200
        assert len(fake.renders) == 4


def test_stale_served_without_refresh_over_limit(tmp_path):
    # ttl 0 -> always stale; min_interval 0 -> never throttled; limit 1 -> the
    # render-on-miss consumes the only slot, so the background revalidate must
    # be skipped while the stale artifact is still served.
    client, fake = make_client(tmp_path, default_ttl_s=0, min_interval_s=0, max_renders_per_minute=1)
    with client:
        assert client.get("/raw/sales_daily").status_code == 200
        assert len(fake.renders) == 1
        assert client.get("/raw/sales_daily").status_code == 200
        time.sleep(0.1)
        assert len(fake.renders) == 1


def test_stale_served_when_vars_unvalidatable(tmp_path):
    # Same shape, but the background revalidate is skipped because describe is
    # down and unvalidated vars must not reach the warehouse.
    class FlakyDct(FakeDct):
        async def describe(self, board_file):
            raise DctError("boom", stderr="describe exploded")

    client, fake = make_client(tmp_path, default_ttl_s=0, min_interval_s=0)
    with client:
        assert client.get("/raw/sales_daily").status_code == 200
        assert len(fake.renders) == 1
        client.app.state.service.dct = FlakyDct()
        client.app.state.service._describe_cache.clear()
        assert client.get("/raw/sales_daily").status_code == 200
        time.sleep(0.1)
        assert len(fake.renders) == 1
