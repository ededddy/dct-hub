"""FastAPI surface: render triggers, render metadata, jobs, board catalog, artifact serving.

When auth is enabled, every route is behind the access policy: `view` to see a
board or its artifacts, `refresh` to trigger warehouse queries (renders,
including render-on-miss board views). Browser flows get redirected to the
OIDC login; API callers get 401/403 JSON.

Renders run through an async queue (`POST /api/renders` returns 202 with a job
id; `wait: true` blocks until done). Freshness policy decides when existing
artifacts are served: frozen (all date vars in the past → never re-render),
fresh (within TTL), stale (served while a background refresh is enqueued).
"""

from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException, Request, Response
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
from .dct import Dct, DctError
from .policy import CachePolicy, Freshness
from .queue import RenderQueue
from .store import ArtifactStore, JobRecord, RenderRecord, utcnow_iso
from .ui import register_ui

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
    wait: bool = False


class RenderResponse(BaseModel):
    key: str
    board: str
    vars: dict[str, str]
    format: str
    outcome: str  # "cached" | "queued" | "running" | "done"
    cached: bool
    stale: bool = False
    job_id: str | None = None
    duration_ms: int | None = None
    rendered_at: str | None = None
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
        self.policy = CachePolicy(config.policy, describe=self.describe_cached)
        concurrency = 1 if config.render.query_cache else config.policy.max_concurrent
        self.queue = RenderQueue(self.store, concurrency, self.execute_render)
        self._describe_cache: dict[str, dict] = {}

    async def describe_cached(self, board_file: Path) -> dict:
        fingerprint = board_fingerprint(self.config.charts_root, board_file, self.config.project_dir / "dbt_charts.yml")
        if fingerprint not in self._describe_cache:
            self._describe_cache[fingerprint] = await self.dct.describe(board_file)
        return self._describe_cache[fingerprint]

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

    async def _key(self, board: str, board_file: Path, variables: dict[str, str], fmt: str) -> tuple[str, str, str]:
        fingerprint = board_fingerprint(self.config.charts_root, board_file, self.config.project_dir / "dbt_charts.yml")
        dct_version = await self.dct.version()
        return artifact_key(board, variables, fingerprint, fmt, dct_version), dct_version, fingerprint

    def _usable(self, record: RenderRecord | None) -> RenderRecord | None:
        if record and record.status == "ok" and Path(record.artifact_path).exists():
            return record
        return None

    async def lookup(self, board: str, variables: dict[str, Any], fmt: str) -> tuple[RenderRecord | None, str, Path, str, dict[str, str]]:
        variables = {k: str(v) for k, v in variables.items() if str(v) != ""}
        _, board_file = self._resolve(board)
        key, _, fingerprint = await self._key(board, board_file, variables, fmt)
        return self._usable(self.store.get(key)), key, board_file, fingerprint, variables

    async def execute_render(self, job: JobRecord) -> RenderRecord:
        # Only successful renders enter the renders table: a failed refresh must
        # never displace the last good artifact. Failures are recorded on the job.
        _, board_file = self._resolve(job.board)
        dct_version = await self.dct.version()
        output = self.store.new_artifact_path(job.board, job.key, job.format)
        result = await self.dct.render(board_file, job.variables, output, job.format)
        record = RenderRecord(
            key=job.key,
            board=job.board,
            variables=job.variables,
            format=job.format,
            artifact_path=str(output),
            status="ok",
            error=None,
            duration_ms=result.duration_ms,
            rendered_at=utcnow_iso(),
            dct_version=dct_version,
        )
        self.store.put(record)
        return record


def _render_response(record: RenderRecord | None, key: str, board: str, variables: dict[str, str], fmt: str,
                     outcome: str, stale: bool = False, job: JobRecord | None = None) -> RenderResponse:
    url = f"/b/{board}"
    if variables:
        url = f"{url}?{urlencode(variables)}"
    return RenderResponse(
        key=key,
        board=board,
        vars=variables,
        format=fmt,
        outcome=outcome,
        cached=outcome == "cached",
        stale=stale,
        job_id=job.id if job else None,
        duration_ms=(record.duration_ms if record else None),
        rendered_at=(record.rendered_at if record else None),
        url=url,
        artifact_url=f"/api/renders/{key}/artifact",
    )


def _job_json(job: JobRecord) -> dict:
    return {
        "id": job.id,
        "key": job.key,
        "board": job.board,
        "vars": job.variables,
        "format": job.format,
        "status": job.status,
        "mode": job.mode,
        "error": job.error,
        "requested_by": job.requested_by,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "duration_ms": job.duration_ms,
    }


def create_app(config: Config, oidc_http=None) -> FastAPI:
    auth_on = config.auth.enabled
    app = FastAPI(
        title="dct-hub",
        version="0.3.0",
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
    async def trigger_render(request: RenderRequest, raw: Request, response: Response) -> RenderResponse:
        board = service._normalize(request.board)
        identity = authorize(raw, "refresh", board)
        record, key, board_file, fingerprint, variables = await service.lookup(board, request.vars, request.format)

        if record is not None and not request.force:
            freshness = await service.policy.freshness(board_file, fingerprint, variables, record)
            if freshness in (Freshness.FROZEN, Freshness.FRESH):
                return _render_response(record, key, board, variables, request.format, "cached")
            if service.policy.within_min_interval(service.store.last_activity(key)):
                return _render_response(record, key, board, variables, request.format, "cached", stale=True)

        mode = "force" if request.force else "auto"
        job, _ = await service.queue.submit(
            key=key, board=board, variables=variables, fmt=request.format,
            mode=mode, requested_by=identity.sub if identity else "anon",
        )
        if request.wait:
            job = await service.queue.wait(job.id, timeout_s=config.render.timeout_s + 15)
            if job and job.status == "done":
                record = service.store.get(key)
                return _render_response(record, key, board, variables, request.format, "done", job=job)
            if job and job.status == "error":
                raise HTTPException(status_code=502, detail=f"dct render failed: {(job.error or '')[-1000:]}")
        response.status_code = 202
        return _render_response(record, key, board, variables, request.format, job.status if job else "queued", job=job)

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

    @app.get("/api/jobs")
    async def jobs(request: Request, limit: int = 50) -> list[dict]:
        identity = identity_of(request)
        if app.state.policy is not None and identity is None:
            raise Unauthenticated()
        all_jobs = service.store.list_jobs(limit)
        if app.state.policy is not None:
            all_jobs = [j for j in all_jobs if app.state.policy.allows(identity, "view", j.board)]
        return [_job_json(j) for j in all_jobs]

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str, request: Request) -> dict:
        identity = identity_of(request)
        if app.state.policy is not None and identity is None:
            raise Unauthenticated()
        job = service.store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
        require(app.state.policy, identity, "view", job.board)
        return _job_json(job)

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

    @app.get("/raw/{board:path}")
    async def raw_board(board: str, request: Request) -> FileResponse:
        board = service._normalize(board)
        variables_qs = dict(request.query_params)
        identity = authorize(request, "view", board)
        record, key, board_file, fingerprint, variables = await service.lookup(board, variables_qs, "html")

        if record is None:
            authorize(request, "refresh", board)
            job, _ = await service.queue.submit(
                key=key, board=board, variables=variables, fmt="html",
                mode="auto", requested_by=identity.sub if identity else "anon",
            )
            job = await service.queue.wait(job.id, timeout_s=config.render.timeout_s + 15)
            if job is not None and job.status == "error":
                raise HTTPException(status_code=502, detail=f"dct render failed: {(job.error or '')[-1000:]}")
            record = service._usable(service.store.get(key))
            if record is None:
                raise HTTPException(status_code=504, detail="render still in progress; retry shortly")
        else:
            freshness = await service.policy.freshness(board_file, fingerprint, variables, record)
            if (
                freshness == Freshness.STALE
                and not service.policy.within_min_interval(service.store.last_activity(key))
                and service.store.active_job_for_key(key) is None
            ):
                await service.queue.submit(
                    key=key, board=board, variables=variables, fmt="html",
                    mode="auto", requested_by=identity.sub if identity else "anon",
                )

        return FileResponse(
            record.artifact_path,
            media_type="text/html",
            headers={
                "X-Dct-Hub-Key": record.key,
                "X-Rendered-At": record.rendered_at,
                # artifacts are replaced in place after a refresh; never let
                # the browser serve its own cached copy
                "Cache-Control": "no-cache",
            },
        )

    register_ui(app, service, config, identity_of, authorize)

    return app
