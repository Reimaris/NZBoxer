"""Bounded in-memory TTL cache for upstream metadata lookups (ADR-126)."""

import time
from typing import Final, Generic, TypeVar

T = TypeVar("T")

METADATA_CACHE_TTL_SECONDS: Final[int] = 600


class TTLCache(Generic[T]):
    """Insertion-ordered cache with lazy expiry and oldest-first eviction."""

    def __init__(
        self, ttl_seconds: int = METADATA_CACHE_TTL_SECONDS, max_entries: int = 512
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._store: dict[str, tuple[float, T]] = {}

    def get(self, key: str) -> T | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at > self._ttl:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: T) -> None:
        self._store.pop(key, None)
        self._store[key] = (time.monotonic(), value)
        while len(self._store) > self._max_entries:
            del self._store[next(iter(self._store))]

    def clear(self) -> None:
        self._store.clear()
