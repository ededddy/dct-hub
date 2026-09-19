import asyncio
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from dct_hub.api import create_app
from dct_hub.config import Config
from dct_hub.dct import DctError, RenderResult

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


def build_authed(tmp_path, monkeypatch, policy=None, auth=None):
    monkeypatch.setenv("DCT_HUB_TEST_TOKEN", "env-token")
    charts = tmp_path / "proj" / "charts"
    (charts / "finance").mkdir(parents=True)
    (charts / "restricted").mkdir(parents=True)
    (charts / "sales_daily.yml").write_text("title: Daily sales report\n")
    (charts / "finance" / "q4.yml").write_text("title: Q4 finance\n")
    (charts / "restricted" / "board.yml").write_text("title: Restricted\n")

    idp = FakeIdP()
    oidc_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=idp.app()))
    kwargs = {"auth": auth or AUTH_CONFIG, "access": ACCESS_CONFIG}
    if policy is not None:
        kwargs["policy"] = policy
    config = Config(project_dir=tmp_path / "proj", storage={"dir": tmp_path / ".hub"}, **kwargs)
    app = create_app(config, oidc_http=oidc_http)
    fake = FakeDct()
    app.state.service.dct = fake
    return app, fake, idp


@pytest.fixture
def env(tmp_path, monkeypatch):
    app, fake, idp = build_authed(tmp_path, monkeypatch)
    with TestClient(app) as client:
        yield client, fake, idp


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


def test_auth_events_logged(env, caplog):
    client, _, idp = env

    with caplog.at_level(logging.WARNING, logger="dct_hub.auth"):
        client.get("/api/boards", headers={"Authorization": "Bearer wrong"})
    assert "unrecognized bearer token" in caplog.text
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="dct_hub.auth"):
        login(client, idp)
    assert "login:" in caplog.text
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="dct_hub.access"):
        assert client.post("/api/renders", json={"board": "sales_daily"}).status_code == 403
    assert "lacks 'refresh'" in caplog.text


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
    assert client.get("/raw/finance/q4").status_code == 403  # render-on-miss needs refresh
    assert client.get("/b/restricted/board").status_code == 403  # no grant for data group

    # catalog hides restricted boards
    boards = {b["board"] for b in client.get("/api/boards").json()}
    assert boards == {"sales_daily", "finance/q4"}

    # logout drops the session
    client.post("/auth/logout", follow_redirects=False)
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
    client.post("/auth/logout", follow_redirects=False)

    idp.rotate_key()  # same kid, new key material — cached JWKS is now stale
    login(client, idp)  # must refetch JWKS and succeed
    assert client.get("/auth/me").json()["user"]["email"] == "ada@corp.test"


def test_open_redirect_blocked(env):
    client, _, _ = env
    resp = client.get("/auth/login?next=//evil.test", follow_redirects=False)
    assert resp.status_code in (302, 307)
    # login proceeds but `next` is sanitized to "/"
    assert "state=" in resp.headers["location"]


def test_login_next_rejects_backslash_variants(env):
    client, _, idp = env
    for bad in ("/\\evil.test", "/%5Cevil.test"):
        resp = client.get(f"/auth/login?next={bad}", follow_redirects=False)
        assert resp.status_code in (302, 307)
        auth_url = urlparse(resp.headers["location"])
        idp_resp = idp_get(idp, f"{auth_url.path}?{auth_url.query}")
        callback = urlparse(idp_resp.headers["location"])
        resp = client.get(f"{callback.path}?{callback.query}", follow_redirects=False)
        assert resp.headers["location"] == "/", bad
        client.post("/auth/logout")


def test_id_token_without_exp_rejected(env):
    client, _, idp = env
    idp.omit_exp = True
    resp = client.get("/auth/login", follow_redirects=False)
    auth_url = urlparse(resp.headers["location"])
    idp_resp = idp_get(idp, f"{auth_url.path}?{auth_url.query}")
    callback = urlparse(idp_resp.headers["location"])
    assert client.get(f"{callback.path}?{callback.query}", follow_redirects=False).status_code == 401


def test_logout_requires_post(env):
    client, _, _ = env
    assert client.get("/auth/logout", follow_redirects=False).status_code == 405


def poll_job(client, job_id, headers=None):
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}", headers=headers or {}).json()
        if job["status"] in ("done", "error", "interrupted"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_job_error_hidden_from_viewers(env):
    client, _, idp = env

    class FailDct(FakeDct):
        async def render(self, board_file, variables, output, fmt):
            raise DctError("boom", stderr="query exploded: schema secret_analytics")

    client.app.state.service.dct = FailDct()
    resp = client.post(
        "/api/renders",
        json={"board": "sales_daily"},
        headers={"Authorization": "Bearer ci-token"},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    job = poll_job(client, job_id, {"Authorization": "Bearer ci-token"})
    assert job["status"] == "error"
    assert "query exploded" in job["error"]  # operators keep the detail

    login(client, idp)  # data group: view-only
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "error"
    assert job["error"] is None  # viewers get no stderr
    jobs = client.get("/api/jobs").json()
    assert jobs[0]["error"] is None


def test_stale_refresh_requires_refresh_grant(tmp_path, monkeypatch):
    app, fake, idp = build_authed(tmp_path, monkeypatch, policy={"default_ttl_s": 0, "min_interval_s": 0})
    with TestClient(app) as client:
        resp = client.post(
            "/api/renders",
            json={"board": "sales_daily", "wait": True},
            headers={"Authorization": "Bearer ci-token"},
        )
        assert resp.status_code == 200
        assert len(fake.renders) == 1

        # TTL 0: immediately stale. A viewer is served the stale artifact but
        # no background render is enqueued.
        idp.user = {"sub": "u-bob", "name": "Bob", "email": "bob@corp.test", "groups": ["data"]}
        login(client, idp)
        assert client.get("/raw/sales_daily").status_code == 200
        time.sleep(0.4)
        assert len(fake.renders) == 1

        # an operator's view enqueues the background refresh
        assert client.get("/raw/sales_daily", headers={"Authorization": "Bearer ci-token"}).status_code == 200
        deadline = time.monotonic() + 5
        while len(fake.renders) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(fake.renders) == 2


def test_render_key_and_job_existence_not_oracle(env):
    client, _, idp = env
    idp.user = {"sub": "u-cara", "name": "Cara", "email": "cara@corp.test", "groups": ["finance"]}
    login(client, idp)
    resp = client.post("/api/renders", json={"board": "finance/q4", "wait": True})
    assert resp.status_code == 200
    key = resp.json()["key"]
    job_id = resp.json()["job_id"]
    client.post("/auth/logout")

    # ci (data-ops): the finance/* carve-out shadows its * grant, so it has no
    # view on finance/q4. Unauthorized must look exactly like unknown.
    ci = {"Authorization": "Bearer ci-token"}
    unknown_key = client.get("/api/renders/" + "0" * 16, headers=ci)
    forbidden_key = client.get(f"/api/renders/{key}", headers=ci)
    assert unknown_key.status_code == forbidden_key.status_code == 404
    # identical message shape (the only difference is the caller-supplied key)
    assert unknown_key.json()["detail"].startswith("unknown render key:")
    assert forbidden_key.json()["detail"].startswith("unknown render key:")

    unknown_job = client.get("/api/jobs/" + "0" * 16, headers=ci)
    forbidden_job = client.get(f"/api/jobs/{job_id}", headers=ci)
    assert unknown_job.status_code == forbidden_job.status_code == 404
    assert unknown_job.json()["detail"].startswith("unknown job:")
    assert forbidden_job.json()["detail"].startswith("unknown job:")


def test_oidc_redirect_url_pinned(tmp_path, monkeypatch):
    auth = dict(AUTH_CONFIG)
    auth["oidc"] = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "redirect_url": "https://hub.example.com/auth/callback",
    }
    app, _, _ = build_authed(tmp_path, monkeypatch, auth=auth)
    with TestClient(app) as client:
        resp = client.get("/auth/login", follow_redirects=False)
        assert resp.status_code in (302, 307)
        # the IdP is sent the pinned callback, not the request-derived one
        assert "redirect_uri=https%3A%2F%2Fhub.example.com%2Fauth%2Fcallback" in resp.headers["location"]
