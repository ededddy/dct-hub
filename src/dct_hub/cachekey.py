"""Artifact cache keys.

An artifact is addressed by everything that determines its bytes: the board
path, the variable values, the board source (the board file plus the meta.yml
chain that deep-merges into it), the output format and the dct version.
"""

import hashlib
import json
from pathlib import Path


def board_fingerprint(charts_root: Path, board_file: Path, project_config: Path | None = None) -> str:
    h = hashlib.sha256()
    if project_config is not None and project_config.exists():
        h.update(project_config.read_bytes())
    directory = charts_root.resolve()
    board_file = board_file.resolve()
    chain = [directory]
    for part in board_file.parent.relative_to(directory).parts:
        directory = directory / part
        chain.append(directory)
    for directory in chain:
        meta = directory / "meta.yml"
        if meta.exists():
            h.update(meta.read_bytes())
    h.update(board_file.read_bytes())
    return h.hexdigest()


def artifact_key(board: str, variables: dict[str, str], fingerprint: str, fmt: str, dct_version: str) -> str:
    payload = {
        "board": board,
        "vars": sorted(variables.items()),
        "src": fingerprint,
        "format": fmt,
        "dct": dct_version,
    }
    blob = json.dumps(payload, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]
