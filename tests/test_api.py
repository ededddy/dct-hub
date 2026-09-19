import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dct_hub.api import create_app
from dct_hub.blobs import artifact_locator
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
                {"name": "day", "type": "date"},
                {"name": "region", "type": "radio", "options": ["East", "West"]},
                {"name": "limit", "type": "number"},
                {"name": "flag", "type": "checkbox"},
            ],
        }


@pytest.fixture
def client(tmp_path):
    charts = tmp_path / "proj" / "charts"
    charts.mkdir(parents=True)
    (charts / "sales_daily.yml").write_text("title: Daily sales report\n")
    config = Config(project_dir=tmp_path / "proj", storage={"dir": tmp_path / ".hub"})
    app = create_app(config)
    fake = FakeDct()
    app.state.service.dct = fake
    # context-managed: runs the lifespan (queue workers start at boot) and keeps
    # one event loop across requests, like uvicorn does in production
    with TestClient(app) as test_client:
        yield test_client, fake


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


def test_raw_board_renders_on_miss_and_serves_html(client):
    client, fake = client

    resp = client.get("/raw/sales_daily?region=East&day=2026-09-16")
    assert resp.status_code == 200
    assert "rendered" in resp.text
    assert resp.headers["x-dct-hub-key"]

    again = client.get("/raw/sales_daily?region=East&day=2026-09-16")
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


def test_unknown_format_rejected(client):
    client, _ = client
    assert client.post("/api/renders", json={"board": "sales_daily", "format": "exe"}).status_code == 422
    assert client.post("/api/renders", json={"board": "sales_daily", "format": "../evil"}).status_code == 422


def test_artifact_locator_rejects_unsafe_format():
    with pytest.raises(ValueError):
        artifact_locator("b", "k", "../x")
    assert artifact_locator("b", "k", "html") == "b/k.html"


def test_var_validation(client):
    client, fake = client
    resp = client.post("/api/renders", json={"board": "sales_daily", "vars": {"bogus": "1"}})
    assert resp.status_code == 400
    assert "bogus" in resp.json()["detail"]

    assert client.post("/api/renders", json={"board": "sales_daily", "vars": {"region": "South"}}).status_code == 400
    assert client.post("/api/renders", json={"board": "sales_daily", "vars": {"day": "not-a-date"}}).status_code == 400
    assert (
        client.post("/api/renders", json={"board": "sales_daily", "vars": {"day": "2026-09-16 OR 1=1"}}).status_code
        == 400
    )
    assert client.post("/api/renders", json={"board": "sales_daily", "vars": {"limit": "lots"}}).status_code == 400
    assert client.post("/api/renders", json={"board": "sales_daily", "vars": {"flag": "yes"}}).status_code == 400

    ok = client.post(
        "/api/renders",
        json={
            "board": "sales_daily",
            "vars": {"region": "East", "day": "2026-09-16", "limit": "10", "flag": "true"},
            "wait": True,
        },
    )
    assert ok.status_code == 200
    assert len(fake.renders) == 1


def test_raw_miss_with_invalid_var_does_not_render(client):
    client, fake = client
    assert client.get("/raw/sales_daily?bogus=1").status_code == 400
    assert len(fake.renders) == 0


def test_describe_failure_serves_cache_but_blocks_new_renders(client):
    client, fake = client
    ok = client.post("/api/renders", json={"board": "sales_daily", "vars": {"region": "East"}, "wait": True})
    assert ok.status_code == 200

    class FlakyDct(FakeDct):
        async def describe(self, board_file):
            raise DctError("boom", stderr="describe exploded")

    client.app.state.service.dct = FlakyDct()
    client.app.state.service._describe_cache.clear()

    # cached artifact still served: no queries run, so no validation either
    assert client.get("/raw/sales_daily?region=East").status_code == 200
    # render paths fail closed — unvalidated vars must not reach the warehouse
    resp = client.post("/api/renders", json={"board": "sales_daily", "vars": {"region": "East"}, "force": True})
    assert resp.status_code == 502


def test_security_headers(client):
    client, _ = client
    resp = client.get("/")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "same-origin"
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"
    assert "default-src 'self'" in resp.headers["content-security-policy"]
    assert "object-src 'none'" in resp.headers["content-security-policy"]
    assert "base-uri 'none'" in resp.headers["content-security-policy"]

    client.post("/api/renders", json={"board": "sales_daily", "wait": True})
    raw = client.get("/raw/sales_daily")
    assert raw.headers["content-security-policy"] == "sandbox allow-scripts"

    page = client.get("/b/sales_daily")
    assert 'sandbox="allow-scripts"' in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]

    key = client.post("/api/renders", json={"board": "sales_daily"}).json()["key"]
    artifact = client.get(f"/api/renders/{key}/artifact")
    assert artifact.headers["content-security-policy"] == "sandbox allow-scripts"

    # svg is script-capable too — it gets the sandbox CSP as well
    svg_key = client.post("/api/renders", json={"board": "sales_daily", "format": "svg", "wait": True}).json()["key"]
    svg = client.get(f"/api/renders/{svg_key}/artifact")
    assert svg.headers["content-security-policy"] == "sandbox allow-scripts"


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

            view = client.get("/raw/sales_daily?day=2026-09-16")
            assert view.status_code == 200
            assert "<" in view.text
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def test_jobs_limit_validation(client):
    client, _ = client
    assert client.get("/api/jobs?limit=0").status_code == 422
    assert client.get("/api/jobs?limit=-1").status_code == 422
    assert client.get("/api/jobs?limit=501").status_code == 422
    assert client.get("/api/jobs?limit=1").status_code == 200


def test_var_value_length_cap(client):
    client, _ = client
    resp = client.post("/api/renders", json={"board": "sales_daily", "vars": {"region": "E" * 5000}})
    assert resp.status_code == 400
    assert "too long" in resp.json()["detail"]


def test_oversized_body_rejected(client):
    client, _ = client
    payload = b'{"board": "sales_daily", "vars": {"region": "' + b"x" * (1024 * 1024 + 64) + b'"}}'
    resp = client.post("/api/renders", content=payload, headers={"content-type": "application/json"})
    assert resp.status_code == 413
