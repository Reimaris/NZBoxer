"""
Error Forwarder (ADR-096)
=========================
A `logging.Handler` that forwards ERROR-and-above log records to the enabled
Telegram / Discord channels (gated by `notify_on_errors`) so failures can be
debugged without opening the log files.

* Identical errors (same logger + normalized message) are suppressed for
  `DEDUP_WINDOW_SECONDS`; the next alert after the window carries a ``×N``
  count of suppressed repeats.
* Tracebacks are truncated to fit Telegram / Discord limits.
* Sending is fire-and-forget on the event loop and never blocks logging.
* Records from the notification modules themselves (and anything emitted while
  sending) are ignored to prevent feedback loops.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from app.core.log_redaction import redact_text

DEDUP_WINDOW_SECONDS = 600.0
MAX_TRACKED_FINGERPRINTS = 200
MAX_BODY_CHARS = 3500
_HEAD_CHARS = 1000

EXCLUDED_LOGGER_PREFIXES = (
    "app.services.discord",
    "app.services.telegram",
    "app.services.notifications",
    "app.core.error_forwarder",
)

_NORMALIZE_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b|\d+")

_sending: ContextVar[bool] = ContextVar("error_forwarder_sending", default=False)

Sender = Callable[[str, str], Awaitable[Any]]


def fingerprint(record: logging.LogRecord) -> str:
    """Stable key for dedup: logger + level + message with numbers/hashes removed."""
    msg = _NORMALIZE_RE.sub("#", str(record.msg))[:200]
    return f"{record.name}|{record.levelno}|{msg}"


def truncate_body(text: str, limit: int = MAX_BODY_CHARS) -> str:
    """Keep the head and the tail (most informative part of a traceback)."""
    if len(text) <= limit:
        return text
    marker = "\n…[truncated]…\n"
    tail = limit - _HEAD_CHARS - len(marker)
    return text[:_HEAD_CHARS] + marker + text[-tail:]


async def _default_sender(title: str, body: str) -> None:
    from app.db.database import async_session_factory
    from app.services.discord import dispatch_alert_message

    async with async_session_factory() as session:
        await dispatch_alert_message(session, "notify_on_errors", title, body)


class ErrorForwarderHandler(logging.Handler):
    """Forward ERROR+ records as deduplicated, redacted, truncated alerts."""

    def __init__(
        self,
        sender: Sender | None = None,
        dedup_window: float = DEDUP_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(level=logging.ERROR)
        self._sender: Sender = sender or _default_sender
        self._dedup_window = dedup_window
        self._clock = clock
        # fingerprint -> (window_start, suppressed_count)
        self._seen: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set[Any] = set()

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the main loop so records from worker threads can be sent."""
        self._loop = loop

    def _should_send(self, key: str) -> tuple[bool, int]:
        """Return (send?, suppressed_repeats_from_previous_window)."""
        now = self._clock()
        entry = self._seen.get(key)
        if entry is not None:
            start, suppressed = entry
            if now - start < self._dedup_window:
                self._seen[key] = (start, suppressed + 1)
                return False, 0
            self._seen.pop(key)
            self._seen[key] = (now, 0)
            return True, suppressed
        self._seen[key] = (now, 0)
        while len(self._seen) > MAX_TRACKED_FINGERPRINTS:
            self._seen.popitem(last=False)
        return True, 0

    def emit(self, record: logging.LogRecord) -> None:
        if _sending.get() or record.name.startswith(EXCLUDED_LOGGER_PREFIXES):
            return
        try:
            send, repeats = self._should_send(fingerprint(record))
            if not send:
                return
            text = f"[{record.name}] {record.getMessage()}"
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                text += f"\n{record.exc_text}"
            body = truncate_body(redact_text(text))
            title = f"❌ NZBoxer {record.levelname}"
            if repeats:
                title += (
                    f" (×{repeats + 1} in last {int(self._dedup_window // 60)} min)"
                )
            self._schedule(title, body)
        except Exception:  # noqa: BLE001 - logging handlers must never raise
            self.handleError(record)

    def _schedule(self, title: str, body: str) -> None:
        async def _run() -> None:
            token = _sending.set(True)
            try:
                await self._sender(title, body)
            except Exception:  # noqa: BLE001 - alert failures must not recurse
                pass
            finally:
                _sending.reset(token)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(_run())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif self._loop is not None and self._loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(_run(), self._loop)
            self._tasks.add(fut)
            fut.add_done_callback(self._tasks.discard)
