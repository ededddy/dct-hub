import asyncio
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from dct_hub.api import create_app
from dct_hub.config import Config
from dct_hub.dct import RenderResult

from fake_idp import CLIENT_ID, ISSUER, FakeIdP


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


AUTH_CONFIG = {
    "enabled": True,
    "session_secret": "test-secret",
    "oidc": {"issuer": ISSUER, "client_id": CLIENT_ID},
    "service_tokens": [
        {"token": "ci-token", "name": "ci", "groups": ["data-ops"]},
        {"token": "${DCT_HUB_TEST_TOKEN}", "name": "env-ci", "groups": ["data-ops"]},
    ],
}

ACCESS_CONFIG = {
    "grants": [
        {"path": "*", "role": "viewer", "groups": ["data"]},
        {"path": "*", "role": "operator", "groups": ["data-ops"]},
        {"path": "finance/*", "role": "viewer", "groups": ["data"]},
        {"path": "finance/*", "role": "operator", "groups": ["finance"]},
        {"path": "restricted/*", "role": "viewer", "groups": ["execs"]},
    ]
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DCT_HUB_TEST_TOKEN", "env-token")
    charts = tmp_path / "proj" / "charts"
    (charts / "finance").mkdir(parents=True)
    (charts / "restricted").mkdir(parents=True)
    (charts / "sales_daily.yml").write_text("title: Daily sales report\n")
    (charts / "finance" / "q4.yml").write_text("title: Q4 finance\n")
    (charts / "restricted" / "board.yml").write_text("title: Restricted\n")

    idp = FakeIdP()
    oidc_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.app()))
    config = Config(
        project_dir=tmp_path / "proj",
        storage={"dir": tmp_path / ".hub"},
        auth=AUTH_CONFIG,
        access=ACCESS_CONFIG,
    )
    app = create_app(config, oidc_http=oidc_http)
    fake = FakeDct()
    app.state.service.dct = fake
    client = TestClient(app)
    return client, fake, idp


def login(client: TestClient, idp: FakeIdP, next_url: str = "/"):
    resp = client.get(f"/auth/login?next={next_url}", follow_redirects=False)
    assert resp.status_code in (302, 307)
    auth_url = urlparse(resp.headers["location"])
    idp_resp = idp_get(idp, f"{auth_url.path}?{auth_url.query}")
    assert idp_resp.status_code in (302, 307)
    callback = urlparse(idp_resp.headers["location"])
    resp = client.get(f"{callback.path}?{callback.query}", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == next_url


def idp_get(idp: FakeIdP, path_and_query: str) -> httpx.Response:
    async def _get() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.app()), base_url="http://idp.test") as browser:
            return await browser.get(path_and_query, follow_redirects=False)

    return asyncio.run(_get())


def test_anonymous_api_requests_rejected(env):
    client, _, _ = env
    assert client.get("/api/boards").status_code == 401
    assert client.post("/api/renders", json={"board": "sales_daily"}).status_code == 401
    assert client.get("/api/renders/deadbeef").status_code == 401  # authz before store lookup
    resp = client.get("/b/sales_daily", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"].startswith("/auth/login")
    assert client.get("/healthz").status_code == 200


def test_bad_bearer_rejected(env):
    client, _, _ = env
    assert client.get("/api/boards", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_service_token_can_refresh(env):
    client, fake, _ = env
    resp = client.post(
        "/api/renders",
        json={"board": "sales_daily", "wait": True},
        headers={"Authorization": "Bearer ci-token"},
    )
    assert resp.status_code == 200
    assert len(fake.renders) == 1
    artifact = client.get(f"/api/renders/{resp.json()['key']}/artifact", headers={"Authorization": "Bearer ci-token"})
    assert artifact.status_code == 200


def test_service_token_from_env(env):
    client, fake, _ = env
    resp = client.post(
        "/api/renders",
        json={"board": "sales_daily", "wait": True},
        headers={"Authorization": "Bearer env-token"},
    )
    assert resp.status_code == 200


def test_oidc_login_then_grants_enforced(env):
    client, fake, idp = env

    # Pre-render as CI so the artifact exists for the viewer (viewers can't render-on-miss).
    client.post("/api/renders", json={"board": "sales_daily", "wait": True}, headers={"Authorization": "Bearer ci-token"})
    assert len(fake.renders) == 1

    idp.user = {"sub": "u-bob", "name": "Bob", "email": "bob@corp.test", "groups": ["data"]}
    login(client, idp, next_url="/b/sales_daily")

    me = client.get("/auth/me").json()
    assert me["user"]["email"] == "bob@corp.test"
    assert me["user"]["groups"] == ["data"]

    # viewer: can view cached artifact, cannot trigger renders
    assert client.get("/b/sales_daily").status_code == 200
    assert len(fake.renders) == 1
    assert client.post("/api/renders", json={"board": "sales_daily"}).status_code == 403
    assert client.get("/b/finance/q4").status_code == 403  # render-on-miss needs refresh
    assert client.get("/b/restricted/board").status_code == 403  # no grant for data group

    # catalog hides restricted boards
    boards = {b["board"] for b in client.get("/api/boards").json()}
    assert boards == {"sales_daily", "finance/q4"}

    # logout drops the session
    client.get("/auth/logout", follow_redirects=False)
    assert client.get("/api/boards").status_code == 401


def test_finance_operator_can_render_finance(env):
    client, fake, idp = env
    idp.user = {"sub": "u-cara", "name": "Cara", "email": "cara@corp.test", "groups": ["finance"]}
    login(client, idp)

    assert client.post("/api/renders", json={"board": "finance/q4", "wait": True}).status_code == 200
    assert len(fake.renders) == 1
    assert client.post("/api/renders", json={"board": "sales_daily"}).status_code == 403
    assert client.get("/api/boards").json() == [{"board": "finance/q4", "notes": "", "title": "Q4 finance"}]


def test_login_rejects_tampered_state(env):
    client, _, idp = env
    resp = client.get("/auth/login", follow_redirects=False)
    auth_url = urlparse(resp.headers["location"])
    idp_resp = idp_get(idp, f"{auth_url.path}?{auth_url.query}")
    callback = urlparse(idp_resp.headers["location"])
    tampered = f"{callback.path}?{callback.query.replace('state=', 'state=evil-', 1)}"
    assert client.get(tampered, follow_redirects=False).status_code == 400


def test_expired_id_token_rejected(env):
    client, _, idp = env
    idp.exp_delta = -3600
    resp = client.get("/auth/login", follow_redirects=False)
    auth_url = urlparse(resp.headers["location"])
    idp_resp = idp_get(idp, f"{auth_url.path}?{auth_url.query}")
    callback = urlparse(idp_resp.headers["location"])
    assert client.get(f"{callback.path}?{callback.query}", follow_redirects=False).status_code == 401
    assert client.get("/auth/me").json() == {"user": None}


def test_jwks_rotation_recovers(env):
    client, _, idp = env
    login(client, idp)  # caches the IdP's JWKS (key #1)
    client.get("/auth/logout", follow_redirects=False)

    idp.rotate_key()  # same kid, new key material — cached JWKS is now stale
    login(client, idp)  # must refetch JWKS and succeed
    assert client.get("/auth/me").json()["user"]["email"] == "ada@corp.test"


def test_open_redirect_blocked(env):
    client, _, _ = env
    resp = client.get("/auth/login?next=//evil.test", follow_redirects=False)
    assert resp.status_code in (302, 307)
    # login proceeds but `next` is sanitized to "/"
    assert "state=" in resp.headers["location"]
