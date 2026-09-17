"""Artifact storage: rendered files on disk, render metadata in SQLite."""

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class RenderRecord:
    key: str
    board: str
    variables: dict[str, str]
    format: str
    artifact_path: str
    status: str  # "ok" | "error"
    error: str | None
    duration_ms: int | None
    rendered_at: str
    dct_version: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS renders (
    key TEXT PRIMARY KEY,
    board TEXT NOT NULL,
    vars_json TEXT NOT NULL,
    format TEXT NOT NULL,
    artifact_path TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    duration_ms INTEGER,
    rendered_at TEXT NOT NULL,
    dct_version TEXT NOT NULL
)
"""


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root
        self.artifacts_dir = root / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(root / "meta.db", check_same_thread=False)
        with self._db:
            self._db.execute(_SCHEMA)

    def new_artifact_path(self, board: str, key: str, fmt: str) -> Path:
        path = self.artifacts_dir / board / f"{key}.{fmt}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def get(self, key: str) -> RenderRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM renders WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return self._to_record(row)

    def put(self, record: RenderRecord) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO renders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.key,
                    record.board,
                    json.dumps(record.variables),
                    record.format,
                    record.artifact_path,
                    record.status,
                    record.error,
                    record.duration_ms,
                    record.rendered_at,
                    record.dct_version,
                ),
            )

    def _to_record(self, row: tuple) -> RenderRecord:
        return RenderRecord(
            key=row[0],
            board=row[1],
            variables=json.loads(row[2]),
            format=row[3],
            artifact_path=row[4],
            status=row[5],
            error=row[6],
            duration_ms=row[7],
            rendered_at=row[8],
            dct_version=row[9],
        )


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
