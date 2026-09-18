"""Artifact retention: periodic pruning of old renders.

Frozen snapshots are exempt — immutability is their contract. Non-frozen
artifacts are pruned when older than `max_age_days`, or when a board exceeds
`max_per_board` non-frozen artifacts (newest kept). Boards whose YAML was
deleted from the repo are treated as non-frozen, so orphaned renders age out.
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .boards import BoardNotFoundError, InvalidBoardRef, resolve_board_file
from .config import Config
from .policy import CachePolicy
from .store import ArtifactStore, RenderRecord

logger = logging.getLogger("dct_hub.gc")


class RetentionSweeper:
    def __init__(self, config: Config, store: ArtifactStore, policy: CachePolicy):
        self.config = config
        self.store = store
        self.policy = policy
        self.retention = config.retention

    async def _keeps(self, record: RenderRecord) -> bool:
        try:
            board_file = resolve_board_file(self.config.charts_root, record.board)
        except (BoardNotFoundError, InvalidBoardRef):
            return False  # board deleted from repo: ages out like any non-frozen render
        frozen = await self.policy.frozen(board_file, record.variables)
        return frozen is not False  # unknown (describe failed) -> keep, never prune blind

    async def sweep_once(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.retention.max_age_days)
        pruned = 0

        survivors: dict[str, list[RenderRecord]] = defaultdict(list)
        for record in self.store.all_renders():
            if await self._keeps(record):
                continue
            rendered = datetime.fromisoformat(record.rendered_at)
            if rendered.tzinfo is None:
                rendered = rendered.replace(tzinfo=timezone.utc)
            if rendered < cutoff:
                self.store.delete_render(record.key)
                pruned += 1
            else:
                survivors[record.board].append(record)

        for board, records in survivors.items():
            for record in records[self.retention.max_per_board :]:
                self.store.delete_render(record.key)
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
