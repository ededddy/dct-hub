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

import asyncio
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.sessions import SessionMiddleware

from .auth.access import AccessPolicy, Forbidden, Unauthenticated, require
from .auth.identity import Identity, resolve_identity
from .auth.oidc import OidcClient, register_auth_routes
from .blobs import LocalBlobs, S3Blobs, artifact_locator
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
from .gc import RetentionSweeper, janitor_loop
from .policy import DATE_INPUT_TYPES, CachePolicy, Freshness
from .queue import RenderQueue
from .ratelimit import RateLimiter
from .store import JobRecord, LocalStore, RenderRecord, utcnow_iso
from .ui import register_ui

MEDIA_TYPES = {
    "html": "text/html",
    "svg": "image/svg+xml",
    "png": "image/png",
    "pdf": "application/pdf",
    "json": "application/json",
    "yaml": "application/yaml",
}

# Rendered HTML is served into an opaque origin: inline chart JS still runs,
# but the artifact gets no cookies, no storage, and no API access as the
# viewing user — a malicious or compromised board can't act for them.
SANDBOX_CSP = "sandbox allow-scripts"


class RenderRequest(BaseModel):
    board: str
    vars: dict[str, Any] = Field(default_factory=dict)
    format: str = "html"
    force: bool = False
    wait: bool = False

    @field_validator("format")
    @classmethod
    def _known_format(cls, value: str) -> str:
        if value not in MEDIA_TYPES:
            raise ValueError(f"unknown format {value!r}; expected one of {sorted(MEDIA_TYPES)}")
        return value


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


class WarmRequest(BaseModel):
    wait: bool = True


class WarmResult(BaseModel):
    board: str
    vars: dict[str, str]
    key: str | None = None
    outcome: str  # "done" | "cached" | "queued" | "running" | "error"
    job_id: str | None = None
    duration_ms: int | None = None
    error: str | None = None


class WarmResponse(BaseModel):
    ok: bool
    results: list[WarmResult]


class RenderService:
    def __init__(self, config: Config):
        self.config = config
        self.ha_mode = bool(config.storage.postgres)
        if self.ha_mode:
            from .store_pg import PgRateLimiter, PgStore

            self.store = PgStore(config.storage.postgres)
            self.blobs = S3Blobs(
                bucket=config.storage.s3.bucket,
                prefix=config.storage.s3.prefix,
                staging_dir=config.storage.dir / "staging",
                endpoint_url=config.storage.s3.endpoint_url,
                region=config.storage.s3.region,
            )
            self.rate_limiter = PgRateLimiter(self.store, config.policy.max_renders_per_minute)
        else:
            self.store = LocalStore(config.storage.dir)
            self.blobs = LocalBlobs(config.storage.dir / "artifacts")
            self.rate_limiter = RateLimiter(config.policy.max_renders_per_minute)
        self.dct = Dct(
            project_dir=config.project_dir,
            bin=config.dct_bin,
            timeout_s=config.render.timeout_s,
            query_cache=config.render.query_cache,
        )
        self.policy = CachePolicy(config.policy, describe=self.describe_cached)
        concurrency = 1 if config.render.query_cache else config.policy.max_concurrent
        self.queue = RenderQueue(
            self.store,
            concurrency,
            self.execute_render,
            single_node=not self.ha_mode,
            # HA: submissions land in Postgres from any replica, so the idle
            # worker loop polls briskly instead of relying on the local wake.
            idle_poll_s=1.0 if self.ha_mode else 5.0,
        )
        self._describe_cache: dict[str, dict] = {}

    async def start(self) -> None:
        if hasattr(self.store, "connect"):
            await self.store.connect()
        await self.queue.start()

    async def describe_cached(self, board_file: Path) -> dict:
        fingerprint = board_fingerprint(self.config.charts_root, board_file, self.config.project_dir / "dbt_charts.yml")
        if fingerprint not in self._describe_cache:
            self._describe_cache[fingerprint] = await self.dct.describe(board_file)
        return self._describe_cache[fingerprint]

    async def validate_vars(self, board_file: Path, variables: dict[str, str]) -> None:
        """Reject undeclared or ill-typed variables before they reach a render.

        Vars become `--var k=v` on the dct command line and boards may
        interpolate them raw into SQL, so anything that can trigger warehouse
        queries is checked against the board's declared variables first.
        Free-text inputs can't be made safe here — quoting them is the board
        author's duty. Serving a cached artifact never validates: it runs no
        queries, so it must not break (D5).
        """
        try:
            info = await self.describe_cached(board_file)
        except DctError as exc:
            raise HTTPException(status_code=502, detail=f"cannot validate variables: {exc.stderr[-500:]}")
        declared = {v["name"]: v for v in info.get("variables", [])}
        unknown = sorted(set(variables) - set(declared))
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown variable(s) {unknown}; declared: {sorted(declared)}",
            )
        for name, value in variables.items():
            spec = declared[name]
            options = [str(o) for o in (spec.get("options") or [])]
            kind = spec.get("type") or ""
            if options and value not in options:
                raise HTTPException(status_code=400, detail=f"{name}: must be one of {options}")
            if kind in DATE_INPUT_TYPES:
                try:
                    date.fromisoformat(value)
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"{name}: expected an ISO date (YYYY-MM-DD)")
            elif kind == "checkbox" and value not in {"true", "false"}:
                raise HTTPException(status_code=400, detail=f"{name}: expected true or false")
            elif kind == "number":
                try:
                    float(value)
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"{name}: expected a number")

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
        # Metadata is the source of truth: blobs are published before their
        # row and deleted after it, so an ok row implies the blob exists.
        if record and record.status == "ok":
            return record
        return None

    async def lookup(self, board: str, variables: dict[str, Any], fmt: str) -> tuple[RenderRecord | None, str, Path, str, dict[str, str]]:
        variables = {k: str(v) for k, v in variables.items() if str(v) != ""}
        _, board_file = self._resolve(board)
        key, _, fingerprint = await self._key(board, board_file, variables, fmt)
        return self._usable(await self.store.get(key)), key, board_file, fingerprint, variables

    async def execute_render(self, job: JobRecord) -> RenderRecord:
        # Only successful renders enter the renders table: a failed refresh must
        # never displace the last good artifact. Failures are recorded on the job.
        _, board_file = self._resolve(job.board)
        dct_version = await self.dct.version()
        locator = artifact_locator(job.board, job.key, job.format)
        staging = self.blobs.staging_path(locator)
        try:
            result = await self.dct.render(board_file, job.variables, staging, job.format)
            # Publish blob before its metadata row; a renders row implies the blob.
            await self.blobs.put_file(locator, staging)
        finally:
            # No-op after a successful publish (os.replace moved the file);
            # removes the orphan when the render or the publish failed.
            staging.unlink(missing_ok=True)
        record = RenderRecord(
            key=job.key,
            board=job.board,
            variables=job.variables,
            format=job.format,
            artifact_path=locator,
            status="ok",
            error=None,
            duration_ms=result.duration_ms,
            rendered_at=utcnow_iso(),
            dct_version=dct_version,
        )
        await self.store.put(record)
        return record

    async def warm_submit(self, board: str, variables: dict[str, Any], fmt: str, requested_by: str) -> WarmResult:
        """Queue one board+combo render if missing or stale, for deploy-pipeline warming.

        Auto (non-force) semantics: a deploy changes the fingerprint, hence the
        key, so changed boards miss the cache on their own; unchanged boards
        report `cached` and cost no warehouse query. Never raises — failures
        are reported per entry so one bad board can't block the rest. Pair with
        `warm_await`, after every entry is submitted, so the worker pool drains
        the list concurrently.
        """
        try:
            record, key, board_file, fingerprint, variables = await self.lookup(board, variables, fmt)
        except HTTPException as exc:
            return WarmResult(board=board, vars={k: str(v) for k, v in variables.items()}, outcome="error", error=str(exc.detail))
        except DctError as exc:  # e.g. dct binary broken — per-entry error, not a request 500
            detail = f"{exc}: {exc.stderr[-300:]}" if exc.stderr else str(exc)
            return WarmResult(board=board, vars={k: str(v) for k, v in variables.items()}, outcome="error", error=detail)
        try:
            await self.validate_vars(board_file, variables)
        except HTTPException as exc:
            return WarmResult(board=board, vars=variables, key=key, outcome="error", error=str(exc.detail))

        if record is not None:
            freshness = await self.policy.freshness(board_file, fingerprint, variables, record)
            if freshness in (Freshness.FROZEN, Freshness.FRESH):
                return WarmResult(board=board, vars=variables, key=key, outcome="cached", duration_ms=record.duration_ms)
            if self.policy.within_min_interval(await self.store.last_activity(key)):
                return WarmResult(board=board, vars=variables, key=key, outcome="cached", duration_ms=record.duration_ms)

        if not await self.rate_limiter.allow(requested_by):
            return WarmResult(board=board, vars=variables, key=key, outcome="error", error="render rate limit exceeded; retry shortly")
        job, _ = await self.queue.submit(
            key=key, board=board, variables=variables, fmt=fmt, mode="auto", requested_by=requested_by,
        )
        return WarmResult(board=board, vars=variables, key=key, outcome="queued", job_id=job.id)

    async def warm_await(self, result: WarmResult) -> WarmResult:
        """Resolve a queued warm entry once its job leaves the queue (or the wait
        window closes). Entries without a job (cached/error) pass through."""
        if result.job_id is None:
            return result
        job = await self.queue.wait(result.job_id, timeout_s=self.config.render.timeout_s + 15)
        if job is not None and job.status == "done":
            record = await self.store.get(result.key) if result.key else None
            return result.model_copy(update={"outcome": "done", "duration_ms": record.duration_ms if record else None})
        if job is not None and job.status == "error":
            return result.model_copy(update={"outcome": "error", "error": (job.error or "")[-1000:]})
        # Still going past the wait window: the artifact will land on its own;
        # report the job so the pipeline can poll, but don't fail the warm.
        return result.model_copy(update={"outcome": "running"})


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
    service = RenderService(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        housekeeping = None
        if service.ha_mode:
            # Leader-elected via the metadata store: reaps orphaned jobs,
            # prunes rate-limit hits, and runs retention when enabled.
            housekeeping = asyncio.create_task(janitor_loop(service))
        elif config.retention.enabled:
            housekeeping = asyncio.create_task(RetentionSweeper(config, service.store, service.blobs, service.policy).run())
        yield
        if housekeeping is not None:
            housekeeping.cancel()

    app = FastAPI(
        title="dct-hub",
        version="0.5.0",
        lifespan=lifespan,
        docs_url=None if auth_on else "/docs",
        redoc_url=None if auth_on else "/redoc",
        openapi_url=None if auth_on else "/openapi.json",
    )
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

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        # setdefault: artifact routes override CSP with a stricter sandbox one.
        # unsafe-inline is unavoidable — all UI assets are inline by design (D13).
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src 'self' data:; frame-ancestors 'self'",
        )
        return response

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
        await service.validate_vars(board_file, variables)

        if record is not None and not request.force:
            freshness = await service.policy.freshness(board_file, fingerprint, variables, record)
            if freshness in (Freshness.FROZEN, Freshness.FRESH):
                return _render_response(record, key, board, variables, request.format, "cached")
            if service.policy.within_min_interval(await service.store.last_activity(key)):
                return _render_response(record, key, board, variables, request.format, "cached", stale=True)

        if not await service.rate_limiter.allow(identity.sub if identity else "anon"):
            raise HTTPException(status_code=429, detail="render rate limit exceeded; retry shortly")
        mode = "force" if request.force else "auto"
        job, _ = await service.queue.submit(
            key=key, board=board, variables=variables, fmt=request.format,
            mode=mode, requested_by=identity.sub if identity else "anon",
        )
        if request.wait:
            job = await service.queue.wait(job.id, timeout_s=config.render.timeout_s + 15)
            if job and job.status == "done":
                record = await service.store.get(key)
                return _render_response(record, key, board, variables, request.format, "done", job=job)
            if job and job.status == "error":
                raise HTTPException(status_code=502, detail=f"dct render failed: {(job.error or '')[-1000:]}")
        response.status_code = 202
        return _render_response(record, key, board, variables, request.format, job.status if job else "queued", job=job)

    @app.post("/api/warm", response_model=WarmResponse)
    async def warm(raw: Request, response: Response, body: WarmRequest | None = None) -> WarmResponse:
        # Deploy-pipeline warm step: render every board+combo declared in the
        # `warm:` config so no viewer pays a cold miss after a deploy or dct
        # upgrade. Per-board failures aggregate into a 502 so `curl -f` turns
        # the pipeline red while still warming everything else.
        identity = identity_of(raw)
        if app.state.policy is not None and identity is None:
            raise Unauthenticated()
        requested_by = identity.sub if identity else "anon"
        wait = True if body is None else body.wait

        async def warm_board(entry) -> list[WarmResult]:
            try:
                board = service._normalize(entry.board)
            except HTTPException as exc:
                return [WarmResult(board=entry.board, vars={}, outcome="error", error=str(exc.detail))]
            try:
                require(app.state.policy, identity, "refresh", board)
            except Forbidden as exc:
                return [WarmResult(board=board, vars={}, outcome="error", error=str(exc.detail))]
            return [await service.warm_submit(board, v, config.render.format, requested_by) for v in entry.vars]

        # Boards submit concurrently (each pays a dct describe subprocess in
        # phase 1); gather preserves config order in the flattened results.
        nested = await asyncio.gather(*(warm_board(e) for e in config.warm.boards))
        results = [r for rs in nested for r in rs]

        if wait:
            # Everything is submitted already, so the worker pool drains the
            # list concurrently — waiting here is observation, per entry.
            results = [await service.warm_await(r) for r in results]

        ok = all(r.outcome != "error" for r in results)
        if not ok:
            response.status_code = 502
        elif not wait:
            response.status_code = 202
        return WarmResponse(ok=ok, results=results)

    @app.get("/api/renders/{key}")
    async def render_status(key: str, request: Request) -> dict:
        identity = identity_of(request)
        if app.state.policy is not None and identity is None:
            raise Unauthenticated()
        record = await service.store.get(key)
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
    async def render_artifact(key: str, request: Request) -> StreamingResponse:
        identity = identity_of(request)
        if app.state.policy is not None and identity is None:
            raise Unauthenticated()
        record = await service.store.get(key)
        if record is None or record.status != "ok" or not await service.blobs.exists(record.artifact_path):
            raise HTTPException(status_code=404, detail=f"no artifact for render key: {key}")
        require(app.state.policy, identity, "view", record.board)
        # html and svg are both script-capable when opened as a document
        headers = {"Content-Security-Policy": SANDBOX_CSP} if record.format in ("html", "svg") else None
        return StreamingResponse(
            service.blobs.read(record.artifact_path),
            media_type=MEDIA_TYPES.get(record.format, "application/octet-stream"),
            headers=headers,
        )

    @app.get("/api/jobs")
    async def jobs(request: Request, limit: int = 50) -> list[dict]:
        identity = identity_of(request)
        if app.state.policy is not None and identity is None:
            raise Unauthenticated()
        all_jobs = await service.store.list_jobs(limit)
        if app.state.policy is not None:
            all_jobs = [j for j in all_jobs if app.state.policy.allows(identity, "view", j.board)]
        return [_job_json(j) for j in all_jobs]

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str, request: Request) -> dict:
        identity = identity_of(request)
        if app.state.policy is not None and identity is None:
            raise Unauthenticated()
        job = await service.store.get_job(job_id)
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
    async def raw_board(board: str, request: Request) -> StreamingResponse:
        board = service._normalize(board)
        variables_qs = dict(request.query_params)
        identity = authorize(request, "view", board)
        record, key, board_file, fingerprint, variables = await service.lookup(board, variables_qs, "html")

        if record is None:
            authorize(request, "refresh", board)
            await service.validate_vars(board_file, variables)
            if not await service.rate_limiter.allow(identity.sub if identity else "anon"):
                raise HTTPException(status_code=429, detail="render rate limit exceeded; retry shortly")
            job, _ = await service.queue.submit(
                key=key, board=board, variables=variables, fmt="html",
                mode="auto", requested_by=identity.sub if identity else "anon",
            )
            job = await service.queue.wait(job.id, timeout_s=config.render.timeout_s + 15)
            if job is not None and job.status == "error":
                raise HTTPException(status_code=502, detail=f"dct render failed: {(job.error or '')[-1000:]}")
            record = service._usable(await service.store.get(key))
            if record is None:
                raise HTTPException(status_code=504, detail="render still in progress; retry shortly")
        else:
            freshness = await service.policy.freshness(board_file, fingerprint, variables, record)
            if (
                freshness == Freshness.STALE
                and not service.policy.within_min_interval(await service.store.last_activity(key))
                and await service.store.active_job_for_key(key) is None
                and await service.rate_limiter.allow(identity.sub if identity else "anon")
            ):
                try:
                    await service.validate_vars(board_file, variables)
                except HTTPException:
                    pass  # unvalidated vars never reach the warehouse; the stale artifact is still served
                else:
                    await service.queue.submit(
                        key=key, board=board, variables=variables, fmt="html",
                        mode="auto", requested_by=identity.sub if identity else "anon",
                    )

        if not await service.blobs.exists(record.artifact_path):
            raise HTTPException(status_code=404, detail="artifact missing; retry to re-render")
        return StreamingResponse(
            service.blobs.read(record.artifact_path),
            media_type="text/html",
            headers={
                "X-Dct-Hub-Key": record.key,
                "X-Rendered-At": record.rendered_at,
                # artifacts are replaced in place after a refresh; never let
                # the browser serve its own cached copy
                "Cache-Control": "no-cache",
                "Content-Security-Policy": SANDBOX_CSP,
            },
        )

    register_ui(app, service, config, identity_of, authorize)

    return app
