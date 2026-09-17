import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dct_hub.config import PolicyConfig
from dct_hub.policy import CachePolicy, Freshness
from dct_hub.store import RenderRecord

TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)
BOARD = Path("/tmp/b.yml")

DESCRIBE = {
    "variables": [
        {"name": "day", "type": "date", "default": TODAY.isoformat()},
        {"name": "region", "type": "select", "options": ["East"]},
    ]
}


async def fake_describe(board_file: Path) -> dict:
    return DESCRIBE


def make_policy(**overrides) -> CachePolicy:
    return CachePolicy(PolicyConfig(**overrides), describe=fake_describe)


def make_record(hours_ago: float = 0) -> RenderRecord:
    rendered = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return RenderRecord(
        key="k",
        board="b",
        variables={},
        format="html",
        artifact_path="/tmp/x.html",
        status="ok",
        error=None,
        duration_ms=10,
        rendered_at=rendered.isoformat(timespec="seconds"),
        dct_version="0.8.0",
    )


def run(coro):
    return asyncio.run(coro)


def test_missing_record():
    assert run(make_policy().freshness(BOARD, "fp", {}, None)) == Freshness.MISSING


def test_past_date_is_frozen_regardless_of_age():
    policy = make_policy(default_ttl_s=60)
    record = make_record(hours_ago=500)  # way past TTL
    assert run(policy.freshness(BOARD, "fp", {"day": YESTERDAY.isoformat()}, record)) == Freshness.FROZEN


def test_today_uses_ttl():
    policy = make_policy(default_ttl_s=3600)
    assert run(policy.freshness(BOARD, "fp", {"day": TODAY.isoformat()}, make_record())) == Freshness.FRESH
    assert run(policy.freshness(BOARD, "fp", {"day": TODAY.isoformat()}, make_record(hours_ago=2))) == Freshness.STALE


def test_unset_date_var_uses_default():
    # default is today -> not frozen
    assert run(make_policy().freshness(BOARD, "fp", {}, make_record())) == Freshness.FRESH


def test_malformed_and_future_dates_are_not_frozen():
    policy = make_policy()
    assert run(policy.freshness(BOARD, "fp", {"day": "not-a-date"}, make_record())) == Freshness.FRESH
    future = (TODAY + timedelta(days=3)).isoformat()
    assert run(policy.freshness(BOARD, "fp", {"day": future}, make_record())) == Freshness.FRESH


def test_frozen_disabled_falls_back_to_ttl():
    policy = make_policy(frozen_date_vars=False, default_ttl_s=60)
    assert run(policy.freshness(BOARD, "fp", {"day": YESTERDAY.isoformat()}, make_record(hours_ago=2))) == Freshness.STALE


def test_min_interval():
    policy = make_policy(min_interval_s=300)
    assert policy.within_min_interval(None) is False
    assert policy.within_min_interval(datetime.now(timezone.utc).isoformat()) is True
    old = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
    assert policy.within_min_interval(old) is False


async def failing_describe(board_file: Path) -> dict:
    raise RuntimeError("dct exploded")


def test_describe_failure_falls_back_to_ttl():
    policy = CachePolicy(PolicyConfig(default_ttl_s=3600, frozen_date_vars=True), describe=failing_describe)
    # yesterday's date would be frozen, but with describe broken we serve via TTL
    assert run(policy.freshness(BOARD, "fp", {"day": YESTERDAY.isoformat()}, make_record())) == Freshness.FRESH
