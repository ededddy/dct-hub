"""Housekeeping: artifact retention and, in HA mode, the leader-elected janitor.

Frozen snapshots are exempt — immutability is their contract. Non-frozen
artifacts are pruned when older than `max_age_days`, or when a board exceeds
`max_per_board` non-frozen artifacts (newest kept). Boards whose YAML was
deleted from the repo are treated as non-frozen, so orphaned renders age out.

Deletes are row-first (`store.delete_render` returns the locator), then the
blob: a renders row must never point at a missing blob. Locally the sweeper
runs as a plain periodic task; in HA mode the janitor runs it under a
Postgres advisory lock, so exactly one replica sweeps.
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .boards import BoardNotFoundError, InvalidBoardRef, resolve_board_file
from .config import Config
from .policy import CachePolicy
from .store import RenderRecord

logger = logging.getLogger("dct_hub.gc")


class RetentionSweeper:
    def __init__(self, config: Config, store, blobs, policy: CachePolicy):
        self.config = config
        self.store = store
        self.blobs = blobs
        self.policy = policy
        self.retention = config.retention

    async def _keeps(self, record: RenderRecord) -> bool:
        try:
            board_file = resolve_board_file(self.config.charts_root, record.board)
        except (BoardNotFoundError, InvalidBoardRef):
            return False  # board deleted from repo: ages out like any non-frozen render
        frozen = await self.policy.frozen(board_file, record.variables)
        return frozen is not False  # unknown (describe failed) -> keep, never prune blind

    async def _delete(self, key: str) -> None:
        locator = await self.store.delete_render(key)
        if locator is not None:
            await self.blobs.delete(locator)

    async def sweep_once(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.retention.max_age_days)
        pruned = 0

        survivors: dict[str, list[RenderRecord]] = defaultdict(list)
        for record in await self.store.all_renders():
            if await self._keeps(record):
                continue
            rendered = datetime.fromisoformat(record.rendered_at)
            if rendered.tzinfo is None:
                rendered = rendered.replace(tzinfo=timezone.utc)
            if rendered < cutoff:
                await self._delete(record.key)
                pruned += 1
            else:
                survivors[record.board].append(record)

        for board, records in survivors.items():
            for record in records[self.retention.max_per_board :]:
                await self._delete(record.key)
                pruned += 1
        return pruned

    async def run(self) -> None:
        while True:
            try:
                pruned = await self.sweep_once()
                if pruned:
                    logger.info("retention sweep pruned %d artifact(s)", pruned)
            except Exception:
                logger.exception("retention sweep failed")
            await asyncio.sleep(self.retention.sweep_interval_s)


async def janitor_loop(service, interval_s: float = 60.0) -> None:
    """HA housekeeping on the leadership holder (Postgres advisory lock):
    reap jobs orphaned by dead replicas, prune rate-limit hits, and run the
    retention sweep when enabled. Exactly one replica leads at a time; if it
    dies, its session's lock releases and another replica takes over.
    """
    stale_after_s = service.config.render.timeout_s + 120
    while True:
        try:
            if await service.store.try_leader():
                try:
                    reaped = await service.store.reap_stale_jobs(stale_after_s)
                    if reaped:
                        logger.info("janitor reaped %d stale job(s)", reaped)
                    await service.store.prune_rate_hits()
                    if service.config.retention.enabled:
                        pruned = await RetentionSweeper(service.config, service.store, service.blobs, service.policy).sweep_once()
                        if pruned:
                            logger.info("retention sweep pruned %d artifact(s)", pruned)
                finally:
                    await service.store.release_leader()
        except Exception:
            logger.exception("janitor cycle failed")
        await asyncio.sleep(interval_s)
