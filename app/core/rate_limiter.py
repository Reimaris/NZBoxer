"""
Async Rate Limiter
==================
Enforces minimum time intervals between outgoing API requests per client.
"""
from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Enforces a minimum interval between requests."""

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval = min_interval_seconds
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        """Wait if necessary to ensure min_interval_seconds since the last call."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self._last_call = time.monotonic()
