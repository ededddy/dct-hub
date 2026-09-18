"""Server-rendered catalog UI: home page and board shell pages.

All assets are inline (air-gapped: no CDN fonts/scripts). The board shell is a
thin wrapper — controls, freshness, refresh button — around the artifact,
which loads in an iframe from /raw/{board} so the shell stays fast while a
first render is still running.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .boards import BoardInfo, list_boards
from .dct import DctError
from .policy import Freshness

if TYPE_CHECKING:
    from .api import RenderService

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

_WIDGET_MAP = {"datepicker": "date", "input": "text", "radio": "select", "checkbox": "select", "multiselect": "text"}


def humanize(iso: str) -> str:
    seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def prepare_variables(describe: dict, current: dict[str, str]) -> list[dict]:
    prepared = []
    for v in describe.get("variables", []) or []:
        kind = v.get("type") or "text"
        widget = _WIDGET_MAP.get(kind, kind)
        if widget not in {"select", "date", "number", "text"}:
            widget = "text"
        options = [str(o) for o in (v.get("options") or [])]
        if kind == "checkbox":
            options = ["true", "false"]
        default = v.get("default")
        prepared.append(
            {
                "name": v["name"],
                "label": v.get("label") or v["name"].replace("_", " "),
                "notes": v.get("notes") or "",
                "widget": widget,
                "options": options,
                "allow_empty": default is None and widget == "select",
                "current": current.get(v["name"], "" if default is None else str(default)),
                "placeholder": "" if default is None else str(default),
            }
        )
    return prepared


def _vars_summary(variables: dict[str, str]) -> str:
    if not variables:
        return "(defaults)"
    return ", ".join(f"{k}={v}" for k, v in sorted(variables.items()))


async def _search(service: "RenderService", q: str, allowed: list[BoardInfo]) -> list[BoardInfo]:
    by_file = {}
    for b in allowed:
        by_file[f"charts/{b.board}.yml"] = b
        by_file[f"charts/{b.board}.yaml"] = b
    try:
        results = await service.dct.search(q)
    except Exception:
        lowered = q.lower()
        return [b for b in allowed if lowered in b.title.lower() or lowered in b.notes.lower()]
    return [by_file[r["file_path"]] for r in results if r.get("file_path") in by_file]


def register_ui(app: FastAPI, service: "RenderService", config, identity_of, authorize) -> None:
    policy = app.state.policy
    auth_on = config.auth.enabled

    def user_ctx(identity) -> dict | None:
        if identity is None or identity.via == "anon":
            return None
        return {"name": identity.name, "sub": identity.sub}

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request, q: str = "") -> HTMLResponse:
        identity = identity_of(request)
        if auth_on and identity is None:
            return RedirectResponse(f"/auth/login?next={quote(request.url.path)}")
        boards = list_boards(service.config.charts_root)
        if policy is not None:
            boards = [b for b in boards if policy.allows(identity, "view", b.board)]
        if q.strip():
            boards = await _search(service, q.strip(), boards)

        latest = await service.store.latest_renders()
        cards = [
            {
                "board": b.board,
                "title": b.title,
                "notes": b.notes.strip(),
                "url": f"/b/{b.board}",
                "last_rendered": humanize(latest[b.board].rendered_at) if b.board in latest else None,
            }
            for b in boards
        ]
        folders: dict[str, list[dict]] = {}
        for card in cards:
            folder = card["board"].rsplit("/", 1)[0] if "/" in card["board"] else ""
            folders.setdefault(folder, []).append(card)
        groups = sorted(folders.items(), key=lambda item: (item[0] != "", item[0]))
        for _, group in groups:
            group.sort(key=lambda c: c["title"].lower())

        return templates.TemplateResponse(
            request, "home.html", {"q": q, "groups": groups, "user": user_ctx(identity), "auth_on": auth_on}
        )

    @app.get("/b/{board:path}", response_class=HTMLResponse)
    async def board_page(board: str, request: Request) -> HTMLResponse:
        board = service._normalize(board)
        identity = authorize(request, "view", board)
        query_vars = {k: v for k, v in request.query_params.items() if v != ""}
        record, key, board_file, fingerprint, variables = await service.lookup(board, query_vars, "html")
        await service.validate_vars(board_file, variables)
        try:
            describe = await service.describe_cached(board_file)
        except DctError as exc:
            raise HTTPException(status_code=502, detail=exc.stderr[-1000:])

        freshness = None
        freshness_class = "never"
        if record is not None:
            level = await service.policy.freshness(board_file, fingerprint, variables, record)
            freshness, freshness_class = {
                Freshness.FROZEN: ("frozen snapshot", "frozen"),
                Freshness.FRESH: ("fresh", "fresh"),
                Freshness.STALE: ("stale — refresh runs in background", "stale"),
            }.get(level, (None, "never"))

        can_refresh = policy is None or policy.allows(identity, "refresh", board)
        renders = [
            {
                "ago": humanize(r.rendered_at),
                "vars_summary": _vars_summary(r.variables),
                "duration_ms": r.duration_ms,
                "url": f"/b/{board}" + ("?" + urlencode(r.variables) if r.variables else ""),
            }
            for r in await service.store.list_renders(board, limit=10)
        ]

        return templates.TemplateResponse(
            request,
            "board.html",
            {
                "board": board,
                "title": describe.get("title") or board,
                "notes": (describe.get("notes") or "").strip(),
                "variables": prepare_variables(describe, variables),
                "variables_current": variables,
                "record": record,
                "rendered_ago": humanize(record.rendered_at) if record else None,
                "freshness": freshness,
                "freshness_class": freshness_class,
                "can_refresh": can_refresh,
                "raw_url": f"/raw/{board}" + ("?" + urlencode(variables) if variables else ""),
                "renders": renders,
                "user": user_ctx(identity),
                "auth_on": auth_on,
            },
        )
