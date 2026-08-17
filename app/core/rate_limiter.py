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


class RollingWindowRateLimiter:
    """Enforces a maximum number of requests within a rolling time window.
    Allows bursting up to the limit, then waits until a slot becomes available.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()

            # Remove timestamps older than the window
            self.timestamps = [
                t for t in self.timestamps if now - t < self.window_seconds
            ]

            if len(self.timestamps) >= self.limit:
                # Wait until the oldest timestamp falls out of the window
                oldest = self.timestamps[0]
                wait_time = self.window_seconds - (now - oldest)
                if wait_time > 0:
                    await asyncio.sleep(wait_time)

                now = time.monotonic()
                self.timestamps = [
                    t for t in self.timestamps if now - t < self.window_seconds
                ]

            self.timestamps.append(time.monotonic())
