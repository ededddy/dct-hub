"""Per-identity render rate limit (opt-in via policy.max_renders_per_minute).

In-memory sliding window over a single asyncio loop, so no locking is needed.
State resets on restart — acceptable for an abuse cap, not a quota system.
"""

import time
from collections import deque

WINDOW_S = 60.0


class RateLimiter:
    def __init__(self, limit_per_minute: int):
        self.limit = limit_per_minute
        self._hits: dict[str, deque[float]] = {}

    async def allow(self, key: str) -> bool:
        """Record one hit for `key`; False when the window is already full."""
        if self.limit <= 0:
            return True
        now = time.monotonic()
        hits = self._hits.get(key)
        if hits is not None:
            while hits and now - hits[0] >= WINDOW_S:
                hits.popleft()
            if not hits:
                del self._hits[key]  # idle identity: drop the empty window
                hits = None
        if hits is None:
            if len(self._hits) >= 10_000:
                # bound one-shot identity growth: keep only active windows
                cutoff = now - WINDOW_S
                self._hits = {k: h for k, h in self._hits.items() if h and h[-1] > cutoff}
            hits = deque()
            self._hits[key] = hits
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True
