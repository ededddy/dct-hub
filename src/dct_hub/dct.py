"""Async wrapper around the dct CLI (render / describe / search)."""

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path


class DctError(Exception):
    def __init__(self, message: str, stderr: str = "", returncode: int = 1):
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


class RenderTimeout(DctError):
    pass


@dataclass
class RenderResult:
    output_path: Path
    duration_ms: int


def _var_arg(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class Dct:
    def __init__(self, project_dir: Path, bin: str = "dct", timeout_s: int = 120, query_cache: Path | None = None):
        self.project_dir = project_dir
        self.bin = bin
        self.timeout_s = timeout_s
        self.query_cache = query_cache
        self._version: str | None = None

    async def _run(self, *args: str, timeout: int | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            self.bin,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.project_dir,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout or self.timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RenderTimeout(f"dct {args[0]} timed out after {timeout or self.timeout_s}s", returncode=-1)
        if proc.returncode != 0:
            raise DctError(
                f"dct {args[0]} exited {proc.returncode}",
                stderr=stderr.decode(errors="replace"),
                returncode=proc.returncode,
            )
        return stdout.decode(errors="replace")

    async def version(self) -> str:
        if self._version is None:
            out = await self._run("--version")
            self._version = out.split()[1] if len(out.split()) > 1 else out.strip()
        return self._version

    async def render(self, board_file: Path, variables: dict[str, str], output: Path, fmt: str) -> RenderResult:
        args = [
            "render",
            str(board_file),
            "--project-dir",
            str(self.project_dir),
            "--format",
            fmt,
            "--output",
            str(output),
        ]
        for key, value in variables.items():
            args += ["--var", f"{key}={_var_arg(value)}"]
        if self.query_cache:
            args += ["--cache", str(self.query_cache)]
        started = time.monotonic()
        await self._run(*args)
        return RenderResult(output_path=output, duration_ms=int((time.monotonic() - started) * 1000))

    async def describe(self, board_file: Path) -> dict:
        out = await self._run("describe", str(board_file), "--project-dir", str(self.project_dir), "--json")
        return json.loads(out)

    async def search(self, query: str) -> list[dict]:
        out = await self._run("search", query, "--project-dir", str(self.project_dir), "--json")
        return json.loads(out).get("results", [])
