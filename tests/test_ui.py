from pathlib import Path

from fastapi.testclient import TestClient

from dct_hub.api import create_app
from dct_hub.config import Config
from dct_hub.dct import RenderResult


class UiFakeDct:
    def __init__(self):
        self.renders = []

    async def version(self):
        return "0.8.0"

    async def render(self, board_file, variables, output, fmt):
        self.renders.append(dict(variables))
        Path(output).write_text("<html>artifact bytes</html>")
        return RenderResult(output_path=output, duration_ms=5)

    async def describe(self, board_file):
        name = str(board_file)
        return {
            "title": "Q4 finance" if "q4" in name else "Daily sales report",
            "notes": "Board notes here",
            "variables": [
                {"name": "day", "type": "date", "default": "2026-09-17"},
                {"name": "region", "type": "select", "options": ["East", "West", "Central"]},
            ],
        }

    async def search(self, query):
        if "fin" in query.lower():
            return [{"file_path": "charts/finance/q4.yml", "title": "Q4 finance"}]
        return []


def make_app(tmp_path, fake: UiFakeDct, auth=None, access=None):
    charts = tmp_path / "proj" / "charts"
    (charts / "finance").mkdir(parents=True)
    (charts / "sales_daily.yml").write_text("title: Daily sales report\n")
    (charts / "finance" / "q4.yml").write_text("title: Q4 finance\n")
    kwargs = {}
    if auth:
        kwargs["auth"] = auth
        kwargs["access"] = access
    config = Config(project_dir=tmp_path / "proj", storage={"dir": tmp_path / ".hub"}, **kwargs)
    app = create_app(config)
    app.state.service.dct = fake
    return app


def test_home_lists_boards_grouped(tmp_path):
    with TestClient(make_app(tmp_path, UiFakeDct())) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Daily sales report" in resp.text
        assert "Q4 finance" in resp.text
        assert ">finance<" in resp.text  # folder header
        assert resp.text.count("never rendered") == 2


def test_home_search_and_badge(tmp_path):
    fake = UiFakeDct()
    with TestClient(make_app(tmp_path, fake)) as client:
        resp = client.get("/?q=finance")
        assert "Q4 finance" in resp.text
        assert "Daily sales report" not in resp.text

        # render one board -> home shows a freshness badge for it
        client.post("/api/renders", json={"board": "sales_daily", "wait": True})
        home = client.get("/")
        assert "rendered just now" in home.text
        assert home.text.count("never rendered") == 1


def test_board_shell_controls_and_iframe(tmp_path):
    with TestClient(make_app(tmp_path, UiFakeDct())) as client:
        resp = client.get("/b/sales_daily?region=West&day=2026-09-15")
        assert resp.status_code == 200
        assert '<input type="date" id="var-day" name="day" value="2026-09-15">' in resp.text
        assert '<option value="West" selected>West</option>' in resp.text
        assert 'src="/raw/sales_daily?' in resp.text
        assert "region=West" in resp.text
        assert 'id="refresh-btn"' in resp.text  # auth off -> everyone can refresh
        assert "not rendered yet" in resp.text


def test_board_shell_frozen_badge_and_history(tmp_path):
    fake = UiFakeDct()
    with TestClient(make_app(tmp_path, fake)) as client:
        client.post("/api/renders", json={"board": "sales_daily", "vars": {"day": "2026-09-15"}, "wait": True})
        client.post("/api/renders", json={"board": "sales_daily", "vars": {"day": "2026-09-14", "region": "East"}, "wait": True})

        resp = client.get("/b/sales_daily?day=2026-09-15")
        assert "frozen snapshot" in resp.text
        assert "day=2026-09-15" in resp.text
        assert "day=2026-09-14, region=East" in resp.text  # snapshot history row
        # iframe loads the raw artifact, which is served
        raw = client.get("/raw/sales_daily?day=2026-09-15")
        assert raw.status_code == 200
        assert "artifact bytes" in raw.text


def test_light_only_shell(tmp_path):
    with TestClient(make_app(tmp_path, UiFakeDct())) as client:
        home = client.get("/")
        assert "dct-hub:theme" not in home.text  # light-only shell: no theme machinery
        assert 'id="theme-toggle"' not in home.text
        assert 'data-theme="dark"' not in home.text
        assert 'class="card-link"' in home.text  # whole card is clickable
        page = client.get("/b/sales_daily")
        assert 'id="canvas-switch"' not in page.text  # no backdrop switcher either
        assert "dct-hub:canvas" not in page.text
        assert 'id="frame-loading"' in page.text  # loading veil until artifact paints


AUTH_ON = {
    "enabled": True,
    "session_secret": "ui-test-secret",
    "service_tokens": [
        {"token": "viewer-tok", "name": "v", "groups": ["data"]},
        {"token": "ops-tok", "name": "o", "groups": ["ops"]},
    ],
}
ACCESS_ON = {
    "grants": [
        {"path": "*", "role": "viewer", "groups": ["data"]},
        {"path": "*", "role": "operator", "groups": ["ops"]},
    ]
}


def test_ui_auth_flows(tmp_path):
    with TestClient(make_app(tmp_path, UiFakeDct(), auth=AUTH_ON, access=ACCESS_ON)) as client:
        # anonymous browser -> login redirect
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"].startswith("/auth/login")

        # viewer sees catalog but no refresh button
        home = client.get("/", headers={"Authorization": "Bearer viewer-tok"})
        assert "Daily sales report" in home.text
        page = client.get("/b/sales_daily", headers={"Authorization": "Bearer viewer-tok"})
        assert page.status_code == 200
        assert 'id="refresh-btn"' not in page.text

        # operator gets the refresh button
        page = client.get("/b/sales_daily", headers={"Authorization": "Bearer ops-tok"})
        assert 'id="refresh-btn"' in page.text


def test_home_search_falls_back_to_substring(tmp_path):
    class FailingSearch(UiFakeDct):
        async def search(self, query):
            raise RuntimeError("dct search broken")

    with TestClient(make_app(tmp_path, FailingSearch())) as client:
        resp = client.get("/?q=finance")
        assert resp.status_code == 200
        assert "Q4 finance" in resp.text
        assert "Daily sales report" not in resp.text
