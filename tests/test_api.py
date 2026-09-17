import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dct_hub.api import create_app
from dct_hub.config import Config
from dct_hub.dct import RenderResult


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
        return {"path": str(board_file), "title": "Fake"}


@pytest.fixture
def client(tmp_path):
    charts = tmp_path / "proj" / "charts"
    charts.mkdir(parents=True)
    (charts / "sales_daily.yml").write_text("title: Daily sales report\n")
    config = Config(project_dir=tmp_path / "proj", storage={"dir": tmp_path / ".hub"})
    app = create_app(config)
    fake = FakeDct()
    app.state.service.dct = fake
    return TestClient(app), fake


def test_render_then_cached(client):
    client, fake = client

    first = client.post("/api/renders", json={"board": "sales_daily", "vars": {"region": "West"}, "wait": True})
    assert first.status_code == 200
    assert first.json()["outcome"] == "done"
    assert first.json()["cached"] is False
    assert first.json()["url"] == "/b/sales_daily?region=West"
    assert len(fake.renders) == 1

    second = client.post("/api/renders", json={"vars": {"region": "West"}, "board": "charts/sales_daily.yml"})
    assert second.json()["cached"] is True
    assert second.json()["key"] == first.json()["key"]
    assert len(fake.renders) == 1

    status = client.get(f"/api/renders/{first.json()['key']}")
    assert status.status_code == 200
    assert status.json()["status"] == "ok"
    assert status.json()["vars"] == {"region": "West"}


def test_view_board_renders_on_miss_and_serves_html(client):
    client, fake = client

    resp = client.get("/b/sales_daily?region=East&day=2026-09-16")
    assert resp.status_code == 200
    assert "rendered" in resp.text
    assert resp.headers["x-dct-hub-key"]

    again = client.get("/b/sales_daily?region=East&day=2026-09-16")
    assert len(fake.renders) == 1
    assert again.text == resp.text


def test_unknown_board_and_traversal(client):
    client, _ = client

    assert client.post("/api/renders", json={"board": "nope"}).status_code == 404
    assert client.post("/api/renders", json={"board": "../secret"}).status_code == 400
    assert client.get("/api/renders/deadbeef").status_code == 404


def test_boards_listing(client):
    client, _ = client
    boards = client.get("/api/boards").json()
    assert boards == [{"board": "sales_daily", "title": "Daily sales report", "notes": ""}]


@pytest.mark.skipif(shutil.which("dct") is None, reason="dct CLI not installed")
def test_e2e_real_dct_render():
    root = Path(__file__).resolve().parent.parent
    state_dir = root / ".hub-test"
    config = Config(project_dir=root / "sample_project", storage={"dir": state_dir})
    try:
        with TestClient(create_app(config)) as client:
            resp = client.post("/api/renders", json={"board": "sales_daily", "vars": {"day": "2026-09-16"}, "wait": True})
            assert resp.status_code == 200, resp.text
            assert resp.json()["outcome"] == "done"
            assert resp.json()["cached"] is False

            cached = client.post("/api/renders", json={"board": "sales_daily", "vars": {"day": "2026-09-16"}})
            assert cached.json()["cached"] is True
            assert cached.json()["key"] == resp.json()["key"]

            view = client.get("/b/sales_daily?day=2026-09-16")
            assert view.status_code == 200
            assert "<" in view.text
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)
