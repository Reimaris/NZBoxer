"""In-memory item-level search mutex and lock manager."""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Optional

from fastapi.responses import HTMLResponse


class MediaItemLockManager:
    """In-memory, thread/task-safe async lock registry that serializes search operations per MediaItem."""

    def __init__(self) -> None:
        self._locked_items: set[int] = set()
        self._lock_owners: dict[int, str] = {}
        self._events: dict[int, asyncio.Event] = {}
        self._waiters: dict[int, int] = {}

    def is_locked(self, item_id: int) -> bool:
        """Check if an item is currently undergoing an active search."""
        return item_id in self._locked_items

    def get_lock_owner(self, item_id: int) -> Optional[str]:
        """Return the owner identifier for an actively held lock, or None if unlocked."""
        return self._lock_owners.get(item_id)

    def try_acquire(self, item_id: int, owner: str = "manual") -> bool:
        """Non-blocking lock acquisition for a specific media item.

        Returns True if acquired; returns False immediately if already locked.
        """
        if item_id in self._locked_items:
            return False
        self._locked_items.add(item_id)
        self._lock_owners[item_id] = owner
        if item_id in self._events:
            self._events[item_id].clear()
        else:
            self._events[item_id] = asyncio.Event()
        return True

    def release(self, item_id: int) -> None:
        """Release the lock for the given media item and notify waiting tasks."""
        if item_id in self._locked_items:
            self._locked_items.remove(item_id)
            self._lock_owners.pop(item_id, None)
            event = self._events.get(item_id)
            if event is not None:
                event.set()
                if self._waiters.get(item_id, 0) <= 0:
                    self._events.pop(item_id, None)

    @asynccontextmanager
    async def lock(
        self, item_id: int, timeout: Optional[float] = None
    ) -> AsyncGenerator[None, None]:
        """Async context manager for scoped item-level locking."""
        while True:
            if self.try_acquire(item_id):
                try:
                    yield
                finally:
                    self.release(item_id)
                return
            event = self._events.setdefault(item_id, asyncio.Event())
            self._waiters[item_id] = self._waiters.get(item_id, 0) + 1
            try:
                if timeout is not None:
                    await asyncio.wait_for(event.wait(), timeout=timeout)
                else:
                    await event.wait()
            finally:
                remaining = self._waiters.get(item_id, 1) - 1
                if remaining <= 0:
                    self._waiters.pop(item_id, None)
                    if item_id not in self._locked_items:
                        self._events.pop(item_id, None)
                else:
                    self._waiters[item_id] = remaining


# Global in-memory singleton
item_lock_manager = MediaItemLockManager()


def create_conflict_response(
    message: str = "A search is already in progress for this show. Please wait for it to complete.",
) -> HTMLResponse:
    """Return an HTTP 409 Conflict response containing an HTMX-compatible toast notification."""
    escaped_msg = message.replace('"', "&quot;")
    content = f"""
    <div class="bg-amber-600 text-white px-4 py-3 rounded-md shadow-lg border border-amber-700 flex items-center justify-between animate-fade-in-down mb-4">
        <div class="flex items-center gap-3">
            <svg class="w-5 h-5 shrink-0 text-amber-200" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
            <span class="text-sm font-medium">{message}</span>
        </div>
        <button onclick="this.parentElement.remove()" class="p-1 text-gray-200 hover:text-white hover:bg-black/20 rounded transition-colors focus:outline-none">
            <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
        </button>
    </div>
    """
    trigger_payload = json.dumps(
        {"showToast": {"message": escaped_msg, "type": "warning"}}
    )
    return HTMLResponse(
        content=content.strip(),
        status_code=409,
        headers={"HX-Trigger": trigger_payload},
    )
