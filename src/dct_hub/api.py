"""FastAPI surface: render triggers, render metadata, board catalog, artifact serving."""

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

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

    async def ensure_artifact(self, board_ref: str, variables: dict[str, Any], fmt: str, force: bool = False) -> tuple[RenderRecord, bool]:
        board, board_file = self._resolve(board_ref)
        variables = {k: str(v) for k, v in variables.items()}
        fingerprint = board_fingerprint(self.config.charts_root, board_file, self.config.project_dir / "dbt_charts.yml")
        dct_version = await self.dct.version()
        key = artifact_key(board, variables, fingerprint, fmt, dct_version)

        cached = self.store.get(key)
        if not force and cached and cached.status == "ok" and Path(cached.artifact_path).exists():
            return cached, False

        async with self.lock:
            existing = self.store.get(key)
            if not force and existing and existing.status == "ok" and Path(existing.artifact_path).exists():
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


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="dct-hub", version="0.1.0")
    service = RenderService(config)
    app.state.service = service

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/api/renders", response_model=RenderResponse)
    async def trigger_render(request: RenderRequest) -> RenderResponse:
        record, created = await service.ensure_artifact(request.board, request.vars, request.format, request.force)
        return _render_response(record, cached=not created)

    @app.get("/api/renders/{key}")
    async def render_status(key: str) -> dict:
        record = service.store.get(key)
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown render key: {key}")
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
    async def render_artifact(key: str) -> FileResponse:
        record = service.store.get(key)
        if record is None or record.status != "ok" or not Path(record.artifact_path).exists():
            raise HTTPException(status_code=404, detail=f"no artifact for render key: {key}")
        return FileResponse(record.artifact_path, media_type=MEDIA_TYPES.get(record.format, "application/octet-stream"))

    @app.get("/api/boards")
    async def boards() -> list[dict]:
        return [vars(info) for info in list_boards(service.config.charts_root)]

    @app.get("/api/boards/{board:path}")
    async def board_detail(board: str) -> dict:
        _, board_file = service._resolve(board)
        try:
            return await service.dct.describe(board_file)
        except DctError as exc:
            raise HTTPException(status_code=502, detail=exc.stderr[-1000:])

    @app.get("/b/{board:path}")
    async def view_board(board: str, request: Request) -> FileResponse:
        variables = dict(request.query_params)
        record, _ = await service.ensure_artifact(board, variables, "html")
        return FileResponse(
            record.artifact_path,
            media_type="text/html",
            headers={"X-Dct-Hub-Key": record.key, "X-Rendered-At": record.rendered_at},
        )

    return app
