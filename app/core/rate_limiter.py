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


class RateLimitExceeded(Exception):
    """Raised when a rolling window rate limit is exceeded and wait time exceeds max_wait_seconds."""

    def __init__(self, message: str, wait_time: float) -> None:
        super().__init__(message)
        self.wait_time = wait_time


class RollingWindowRateLimiter:
    """Enforces a maximum number of requests within a rolling time window.
    Allows bursting up to the limit. If limit is exceeded, either waits up to
    max_wait_seconds or raises RateLimitExceeded immediately.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        max_wait_seconds: float = 0.0,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_wait_seconds = max_wait_seconds
        self.timestamps: list[float] = []
        self._lock = asyncio.Lock()

    def get_wait_time(self) -> float:
        """Return the number of seconds until a request slot becomes available, or 0.0."""
        now = time.monotonic()
        self.timestamps = [t for t in self.timestamps if now - t < self.window_seconds]
        if len(self.timestamps) < self.limit:
            return 0.0
        oldest = self.timestamps[0]
        return max(0.0, self.window_seconds - (now - oldest))

    async def wait(self) -> None:
        async with self._lock:
            wait_time = self.get_wait_time()
            if wait_time > self.max_wait_seconds:
                raise RateLimitExceeded(
                    f"Rate limit exceeded ({self.limit} requests per {int(self.window_seconds)}s). Wait time: {int(wait_time)}s",
                    wait_time=wait_time,
                )

            if wait_time > 0:
                await asyncio.sleep(wait_time)

            now = time.monotonic()
            self.timestamps = [
                t for t in self.timestamps if now - t < self.window_seconds
            ]
            self.timestamps.append(time.monotonic())
