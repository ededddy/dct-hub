"""FastAPI surface: render triggers, render metadata, board catalog, artifact serving.

When auth is enabled, every route is behind the access policy: `view` to see a
board or its artifacts, `refresh` to trigger warehouse queries (renders,
including render-on-miss board views). Browser flows get redirected to the
OIDC login; API callers get 401/403 JSON.
"""

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .auth.access import AccessPolicy, Forbidden, Unauthenticated, require
from .auth.identity import Identity, resolve_identity
from .auth.oidc import OidcClient, register_auth_routes
from .boards import (
    BoardNotFoundError,
    InvalidBoardRef,
    list_boards,
    normalize_board,
    resolve_board_file,
)
from .cachekey import artifact_key, board_fingerprint
from .config import Config
from .dct import Dct, DctError, RenderTimeout
from .store import ArtifactStore, RenderRecord, utcnow_iso

MEDIA_TYPES = {
    "html": "text/html",
    "svg": "image/svg+xml",
    "png": "image/png",
    "pdf": "application/pdf",
    "json": "application/json",
    "yaml": "application/yaml",
}


class RenderRequest(BaseModel):
    board: str
    vars: dict[str, Any] = Field(default_factory=dict)
    format: str = "html"
    force: bool = False


class RenderResponse(BaseModel):
    key: str
    board: str
    vars: dict[str, str]
    format: str
    cached: bool
    duration_ms: int | None
    rendered_at: str
    url: str
    artifact_url: str


class RenderService:
    def __init__(self, config: Config):
        self.config = config
        self.store = ArtifactStore(config.storage.dir)
        self.dct = Dct(
            project_dir=config.project_dir,
            bin=config.dct_bin,
            timeout_s=config.render.timeout_s,
            query_cache=config.render.query_cache,
        )
        self.lock = asyncio.Lock()

    def _resolve(self, board_ref: str) -> tuple[str, Path]:
        try:
            board = normalize_board(board_ref)
            return board, resolve_board_file(self.config.charts_root, board)
        except InvalidBoardRef as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except BoardNotFoundError:
            raise HTTPException(status_code=404, detail=f"board not found: {board_ref}")

    def _normalize(self, board_ref: str) -> str:
        try:
            return normalize_board(board_ref)
        except InvalidBoardRef as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    async def _key(self, board: str, board_file: Path, variables: dict[str, str], fmt: str) -> tuple[str, str]:
        fingerprint = board_fingerprint(self.config.charts_root, board_file, self.config.project_dir / "dbt_charts.yml")
        dct_version = await self.dct.version()
        return artifact_key(board, variables, fingerprint, fmt, dct_version), dct_version

    def _usable(self, record: RenderRecord | None) -> RenderRecord | None:
        if record and record.status == "ok" and Path(record.artifact_path).exists():
            return record
        return None

    async def peek(self, board_ref: str, variables: dict[str, Any], fmt: str) -> RenderRecord | None:
        board, board_file = self._resolve(board_ref)
        variables = {k: str(v) for k, v in variables.items()}
        key, _ = await self._key(board, board_file, variables, fmt)
        return self._usable(self.store.get(key))

    async def ensure_artifact(self, board_ref: str, variables: dict[str, Any], fmt: str, force: bool = False) -> tuple[RenderRecord, bool]:
        board, board_file = self._resolve(board_ref)
        variables = {k: str(v) for k, v in variables.items()}
        key, dct_version = await self._key(board, board_file, variables, fmt)

        cached = self._usable(self.store.get(key))
        if not force and cached:
            return cached, False

        async with self.lock:
            existing = self._usable(self.store.get(key))
            if not force and existing:
                return existing, False

            output = self.store.new_artifact_path(board, key, fmt)
            try:
                result = await self.dct.render(board_file, variables, output, fmt)
                record = RenderRecord(
                    key=key,
                    board=board,
                    variables=variables,
                    format=fmt,
                    artifact_path=str(output),
                    status="ok",
                    error=None,
                    duration_ms=result.duration_ms,
                    rendered_at=utcnow_iso(),
                    dct_version=dct_version,
                )
            except RenderTimeout as exc:
                raise HTTPException(status_code=504, detail=str(exc))
            except DctError as exc:
                self.store.put(
                    RenderRecord(key, board, variables, fmt, str(output), "error", exc.stderr[-2000:], None, utcnow_iso(), dct_version)
                )
                raise HTTPException(status_code=502, detail=f"dct render failed: {exc.stderr[-1000:]}")
            self.store.put(record)
            return record, True


def _render_response(record: RenderRecord, cached: bool) -> RenderResponse:
    url = f"/b/{record.board}"
    if record.variables:
        url = f"{url}?{urlencode(record.variables)}"
    return RenderResponse(
        key=record.key,
        board=record.board,
        vars=record.variables,
        format=record.format,
        cached=cached,
        duration_ms=record.duration_ms,
        rendered_at=record.rendered_at,
        url=url,
        artifact_url=f"/api/renders/{record.key}/artifact",
    )


def create_app(config: Config, oidc_http=None) -> FastAPI:
    auth_on = config.auth.enabled
    app = FastAPI(
        title="dct-hub",
        version="0.2.0",
        docs_url=None if auth_on else "/docs",
        redoc_url=None if auth_on else "/redoc",
        openapi_url=None if auth_on else "/openapi.json",
    )
    service = RenderService(config)
    app.state.service = service
    app.state.auth_config = config.auth
    app.state.policy = AccessPolicy(config.access) if auth_on else None

    if auth_on:
        app.add_middleware(
            SessionMiddleware,
            secret_key=config.auth.session_secret,
            https_only=config.auth.session_https_only,
            max_age=config.auth.session_max_age_s,
            same_site="lax",
        )
        if config.auth.oidc is not None:
            oidc = OidcClient(config.auth.oidc, http=oidc_http)
            app.state.oidc = oidc
            register_auth_routes(app, oidc, config.auth.oidc.groups_claim)
        else:

            @app.get("/auth/login", include_in_schema=False)
            async def login_unavailable() -> None:
                raise HTTPException(status_code=400, detail="OIDC not configured; authenticate with a Bearer token")

    def identity_of(request: Request) -> Identity | None:
        return resolve_identity(request, config.auth)

    def authorize(request: Request, capability: str, board: str) -> Identity | None:
        identity = identity_of(request)
        require(app.state.policy, identity, capability, board)
        return identity

    @app.exception_handler(Unauthenticated)
    async def unauthenticated_handler(request: Request, exc: Unauthenticated):
        if request.url.path.startswith("/b/"):
            next_url = request.url.path
            if request.url.query:
                next_url += "?" + request.url.query
            return RedirectResponse(f"/auth/login?next={quote(next_url)}")
        return JSONResponse({"detail": exc.detail}, status_code=401)

    @app.exception_handler(Forbidden)
    async def forbidden_handler(request: Request, exc: Forbidden):
        return JSONResponse({"detail": exc.detail}, status_code=403)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/api/renders", response_model=RenderResponse)
    async def trigger_render(request: RenderRequest, raw: Request) -> RenderResponse:
        authorize(raw, "refresh", service._normalize(request.board))
        record, created = await service.ensure_artifact(request.board, request.vars, request.format, request.force)
        return _render_response(record, cached=not created)

    @app.get("/api/renders/{key}")
    async def render_status(key: str, request: Request) -> dict:
        identity = identity_of(request)
        if app.state.policy is not None and identity is None:
            raise Unauthenticated()
        record = service.store.get(key)
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown render key: {key}")
        require(app.state.policy, identity, "view", record.board)
        return {
            "key": record.key,
            "board": record.board,
            "vars": record.variables,
            "format": record.format,
            "status": record.status,
            "error": record.error,
            "duration_ms": record.duration_ms,
            "rendered_at": record.rendered_at,
            "dct_version": record.dct_version,
        }

    @app.get("/api/renders/{key}/artifact")
    async def render_artifact(key: str, request: Request) -> FileResponse:
        identity = identity_of(request)
        if app.state.policy is not None and identity is None:
            raise Unauthenticated()
        record = service.store.get(key)
        if record is None or record.status != "ok" or not Path(record.artifact_path).exists():
            raise HTTPException(status_code=404, detail=f"no artifact for render key: {key}")
        require(app.state.policy, identity, "view", record.board)
        return FileResponse(record.artifact_path, media_type=MEDIA_TYPES.get(record.format, "application/octet-stream"))

    @app.get("/api/boards")
    async def boards(request: Request) -> list[dict]:
        identity = identity_of(request)
        if app.state.policy is not None and identity is None:
            raise Unauthenticated()
        all_boards = list_boards(service.config.charts_root)
        if app.state.policy is not None:
            all_boards = [b for b in all_boards if app.state.policy.allows(identity, "view", b.board)]
        return [vars(info) for info in all_boards]

    @app.get("/api/boards/{board:path}")
    async def board_detail(board: str, request: Request) -> dict:
        resolved = service._normalize(board)
        authorize(request, "view", resolved)
        _, board_file = service._resolve(board)
        try:
            return await service.dct.describe(board_file)
        except DctError as exc:
            raise HTTPException(status_code=502, detail=exc.stderr[-1000:])

    @app.get("/b/{board:path}")
    async def view_board(board: str, request: Request) -> FileResponse:
        board = service._normalize(board)
        variables = dict(request.query_params)
        authorize(request, "view", board)
        existing = await service.peek(board, variables, "html")
        if existing is None:
            authorize(request, "refresh", board)
        record, _ = await service.ensure_artifact(board, variables, "html")
        return FileResponse(
            record.artifact_path,
            media_type="text/html",
            headers={"X-Dct-Hub-Key": record.key, "X-Rendered-At": record.rendered_at},
        )

    return app
