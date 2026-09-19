import asyncio
from datetime import datetime, timedelta, timezone

from dct_hub.blobs import LocalBlobs
from dct_hub.config import Config
from dct_hub.gc import RetentionSweeper
from dct_hub.policy import CachePolicy
from dct_hub.store import LocalStore, RenderRecord


class FakeDct:
    async def describe(self, board_file):
        return {"variables": [{"name": "day", "type": "date", "default": None}]}


def make_record(key, board, days_old, variables=None, artifact_path=None):
    rendered = datetime.now(timezone.utc) - timedelta(days=days_old)
    return RenderRecord(
        key=key,
        board=board,
        variables=variables or {},
        format="html",
        # blob locators are store-root-relative; the blob layer rejects
        # absolute or escaping paths
        artifact_path=artifact_path or f"sales_daily/{key}.html",
        status="ok",
        error=None,
        duration_ms=5,
        rendered_at=rendered.isoformat(timespec="seconds"),
        dct_version="0.8.0",
    )


def make_sweeper(tmp_path, **retention):
    charts = tmp_path / "proj" / "charts"
    charts.mkdir(parents=True)
    (charts / "sales_daily.yml").write_text("title: x\n")
    config = Config(project_dir=tmp_path / "proj", storage={"dir": tmp_path / ".hub"}, retention=retention)
    store = LocalStore(config.storage.dir)
    blobs = LocalBlobs(config.storage.dir / "artifacts")
    policy = CachePolicy(config.policy, describe=lambda bf: FakeDct().describe(bf))
    return RetentionSweeper(config, store, blobs, policy), store


def run(coro):
    return asyncio.run(coro)


def test_age_pruning_skips_frozen(tmp_path):
    sweeper, store = make_sweeper(tmp_path, enabled=True, max_age_days=30)
    old_file = tmp_path / ".hub" / "artifacts" / "sales_daily" / "old.html"
    old_file.parent.mkdir(parents=True, exist_ok=True)
    old_file.write_text("<html>old</html>")
    run(store.put(make_record("old-frozen", "sales_daily", 90, {"day": "2026-01-01"})))
    run(store.put(make_record("old-live", "sales_daily", 90, {"day": "2099-01-01"}, artifact_path="sales_daily/old.html")))
    run(store.put(make_record("young", "sales_daily", 5, {"day": "2099-01-01"})))

    assert run(sweeper.sweep_once()) == 1
    assert run(store.get("old-frozen")) is not None  # frozen snapshots are never pruned
    assert run(store.get("old-live")) is None
    assert run(store.get("young")) is not None
    assert not old_file.exists()  # artifact blob deleted with the row


def test_count_cap_applies_to_non_frozen_only(tmp_path):
    sweeper, store = make_sweeper(tmp_path, enabled=True, max_age_days=999, max_per_board=2)
    for i in range(4):
        run(store.put(make_record(f"r{i}", "sales_daily", i, {"day": "2099-01-01"})))
    run(store.put(make_record("frozen", "sales_daily", 100, {"day": "2026-01-01"})))

    assert run(sweeper.sweep_once()) == 2
    remaining = [r.key for r in run(store.list_renders("sales_daily", 10))]
    assert remaining == ["r0", "r1", "frozen"]


def test_orphaned_board_ages_out(tmp_path):
    sweeper, store = make_sweeper(tmp_path, enabled=True, max_age_days=30)
    run(store.put(make_record("orphan", "deleted_board", 90, {"day": "2026-01-01"})))
    assert run(sweeper.sweep_once()) == 1
    assert run(store.get("orphan")) is None


def test_describe_failure_never_prunes(tmp_path):
    sweeper, store = make_sweeper(tmp_path, enabled=True, max_age_days=30)
    run(store.put(make_record("unknown", "sales_daily", 90, {"day": "2026-01-01"})))

    async def broken(board_file):
        raise RuntimeError("dct exploded")

    sweeper.policy = CachePolicy(sweeper.config.policy, describe=broken)
    assert run(sweeper.sweep_once()) == 0
    assert run(store.get("unknown")) is not None
