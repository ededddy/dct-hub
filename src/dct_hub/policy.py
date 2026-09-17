"""Cache freshness policy.

Decides when an existing artifact may be served and when it must be
re-rendered. A board is FROZEN when every variable declared as a date picker
points at a past date — historical data doesn't change, so the artifact is
immutable. Anything else is FRESH within `default_ttl_s` of its render and
STALE beyond it. `daterange` variables are not treated as frozen (their value
shape is ambiguous over the API) — they follow the TTL.
"""

from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

from .config import PolicyConfig
from .store import RenderRecord

DATE_INPUT_TYPES = {"date", "datepicker"}


def _parse_ts(iso: str) -> datetime:
    parsed = datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


class Freshness(str, Enum):
    MISSING = "missing"
    FROZEN = "frozen"
    FRESH = "fresh"
    STALE = "stale"


class CachePolicy:
    def __init__(self, config: PolicyConfig, describe: Callable[[Path], Awaitable[dict]]):
        self.config = config
        self._describe = describe
        self._schemas: dict[str, dict[str, str | None]] = {}  # fingerprint -> {var name: default}

    async def _date_vars(self, board_file: Path, fingerprint: str) -> dict[str, str | None]:
        if fingerprint not in self._schemas:
            try:
                info = await self._describe(board_file)
            except Exception:
                info = {}  # freshness must never break serving; fall back to TTL
            self._schemas[fingerprint] = {
                v["name"]: (None if v.get("default") is None else str(v["default"]))
                for v in info.get("variables", [])
                if v.get("type") in DATE_INPUT_TYPES
            }
        return self._schemas[fingerprint]

    async def freshness(
        self,
        board_file: Path,
        fingerprint: str,
        variables: dict[str, str],
        record: RenderRecord | None,
        now: datetime | None = None,
    ) -> Freshness:
        if record is None:
            return Freshness.MISSING

        if self.config.frozen_date_vars:
            date_vars = await self._date_vars(board_file, fingerprint)
            if date_vars and self._all_in_past(date_vars, variables):
                return Freshness.FROZEN

        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age = (now - _parse_ts(record.rendered_at)).total_seconds()
        return Freshness.FRESH if age < self.config.default_ttl_s else Freshness.STALE

    @staticmethod
    def _all_in_past(date_vars: dict[str, str | None], variables: dict[str, str]) -> bool:
        today = date.today()
        for name, default in date_vars.items():
            raw = variables.get(name, default)
            if raw is None:
                return False  # unset date var: can't prove the render is historical
            try:
                value = date.fromisoformat(str(raw)[:10])
            except ValueError:
                return False
            if value >= today:
                return False
        return True

    def within_min_interval(self, last_activity_iso: str | None) -> bool:
        if last_activity_iso is None:
            return False
        elapsed = (datetime.now(timezone.utc) - _parse_ts(last_activity_iso)).total_seconds()
        return elapsed < self.config.min_interval_s
