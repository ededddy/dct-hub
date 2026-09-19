"""Board reference handling.

Accepts any of `sales_daily`, `sales_daily.yml`, `charts/sales_daily.yml` and
normalizes to the charts-relative path without suffix (`sales_daily`,
`folder/name`) — the same addressing dct serve uses in URLs.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml


class InvalidBoardRef(ValueError):
    pass


class BoardNotFoundError(FileNotFoundError):
    pass


def normalize_board(ref: str) -> str:
    ref = ref.strip().lstrip("/")
    if ref.startswith("charts/"):
        ref = ref[len("charts/"):]
    for suffix in (".yml", ".yaml"):
        if ref.endswith(suffix):
            ref = ref[: -len(suffix)]
            break
    parts = [p for p in ref.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise InvalidBoardRef(f"invalid board reference: {ref!r}")
    return "/".join(parts)


def resolve_board_file(charts_root: Path, board: str) -> Path:
    root = charts_root.resolve()
    for suffix in (".yml", ".yaml"):
        try:
            candidate = (charts_root / f"{board}{suffix}").resolve()
            if not candidate.is_relative_to(root):
                raise InvalidBoardRef(board)
            # Directory entries carry the true on-disk spelling, so this match
            # stays exact even on case-insensitive filesystems (APFS, Windows),
            # where is_file() would also succeed for a differently-cased ref
            # and thereby dodge path-grant string matching.
            if any(p.name == candidate.name for p in candidate.parent.iterdir()):
                return candidate
        except FileNotFoundError:
            continue  # parent directory does not exist
        except ValueError as exc:  # embedded NUL etc.: invalid ref, not a 500
            if isinstance(exc, InvalidBoardRef):
                raise
            raise InvalidBoardRef(board) from exc
    raise BoardNotFoundError(board)


@dataclass
class BoardInfo:
    board: str
    title: str
    notes: str


def list_boards(charts_root: Path) -> list[BoardInfo]:
    boards = []
    for path in sorted(charts_root.rglob("*.yml")) + sorted(charts_root.rglob("*.yaml")):
        rel = path.relative_to(charts_root).with_suffix("").as_posix()
        if path.stem == "meta" or "partials" in path.relative_to(charts_root).parts:
            continue
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError:
            raw = {}
        boards.append(BoardInfo(board=rel, title=str(raw.get("title") or rel), notes=str(raw.get("notes") or "")))
    return boards
