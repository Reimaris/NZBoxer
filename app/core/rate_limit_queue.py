"""
Rate-Limit Push Queue (ADR-094)
===============================
When TorBox rate-limits NZBoxer (HTTP 429 / active cooldown), Auto-Push targets
are not failed. Instead a persisted ``rate_limited`` ``DownloadHistory`` row
(and ``RATE_LIMITED`` entity status) marks the target as paused. A lightweight
APScheduler job (``rate_limit_resume_job``) re-runs Auto-Push Best for the
queued targets in FIFO order once the TorBox cooldown has expired.

The job is paused whenever the queue is empty (zero idle CPU/DB usage) and is
woken whenever a push is enqueued. Because the queue lives in the database it
survives restarts; after a restart the in-memory cooldown is unknown, so queued
pushes are attempted immediately (a fresh 429 simply re-queues them).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.db.models import DownloadHistory, MediaItem
from app.services import torbox

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

RATE_LIMITED_DETAIL = "rate_limited"
RESUME_JOB_ID = "rate_limit_resume_job"


def is_rate_limit_error(message: str | None) -> bool:
    """True if a failure reason/exception text describes a TorBox rate limit."""
    return "rate limit" in (message or "").lower()


def resume_time_label() -> str:
    """Human-readable resume time for the current TorBox cooldown."""
    remaining = torbox.get_cooldown_remaining(is_manual=True)
    if remaining <= 0:
        return "resuming shortly"
    resume_at = datetime.now().astimezone() + timedelta(seconds=remaining)
    return f"resumes {resume_at:%H:%M}"


class RateLimitQueueState:
    """APScheduler wake/sleep control for the rate-limit resume job."""

    def __init__(self) -> None:
        self._scheduler: Any | None = None

    def set_scheduler(self, scheduler: Any) -> None:
        self._scheduler = scheduler

    def wake(self) -> None:
        if self._scheduler is None:
            return
        try:
            job = self._scheduler.get_job(RESUME_JOB_ID)
            if job is not None and job.next_run_time is None:
                self._scheduler.resume_job(RESUME_JOB_ID)
                logger.info("⏸️ Rate-limit queue active — resume job woken.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not resume %s: %s", RESUME_JOB_ID, exc)

    def sleep(self) -> None:
        if self._scheduler is None:
            return
        try:
            job = self._scheduler.get_job(RESUME_JOB_ID)
            if job is not None and job.next_run_time is not None:
                self._scheduler.pause_job(RESUME_JOB_ID)
                logger.info("💤 Rate-limit queue empty — resume job paused.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not pause %s: %s", RESUME_JOB_ID, exc)


rate_limit_queue = RateLimitQueueState()


async def count_rate_limited_pushes(session: AsyncSession) -> int:
    """Number of queued (not dismissed) rate-limited pushes."""
    return (
        await session.execute(
            select(func.count(DownloadHistory.id)).where(
                DownloadHistory.status_detail == RATE_LIMITED_DETAIL,
                DownloadHistory.is_dismissed.is_(False),
            )
        )
    ).scalar() or 0


async def resume_rate_limited_pushes() -> int:
    """Re-run Auto-Push Best for queued targets (FIFO) once the cooldown is over.

    Returns the number of items whose queued targets were re-dispatched.
    """
    from app.core.push_engine import execute_auto_push
    from app.core.search_lock import item_lock_manager
    from app.core.transfer_poller import transfer_poller
    from app.db.database import async_session_factory

    if torbox.get_cooldown_remaining(is_manual=True) > 0:
        return 0

    async with async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(DownloadHistory)
                    .where(
                        DownloadHistory.status_detail == RATE_LIMITED_DETAIL,
                        DownloadHistory.is_dismissed.is_(False),
                        DownloadHistory.media_item_id.is_not(None),
                    )
                    .order_by(DownloadHistory.id)
                )
            )
            .scalars()
            .all()
        )

    if not rows:
        rate_limit_queue.sleep()
        return 0

    # Preserve FIFO order of queued rows (manual picks keep their chosen release;
    # auto-push targets are grouped per item).
    work_items: list[tuple[str, int, dict[str, Any]]] = []
    auto_groups: dict[int, dict[str, list[int]]] = {}
    for h in rows:
        assert h.media_item_id is not None
        if (h.push_mode or "auto").lower() == "manual" and h.nzb_guid:
            work_items.append(
                (
                    "manual",
                    h.media_item_id,
                    {
                        "guid": h.nzb_guid,
                        "title": h.nzb_title,
                        "size_bytes": h.size_bytes or 0,
                        "score": h.score or 0.0,
                        "is_fallback": bool(h.is_fallback),
                        "matched_language": h.grabbed_language,
                        "season_id": h.season_id,
                        "episode_id": h.episode_id,
                    },
                )
            )
            continue
        if h.media_item_id not in auto_groups:
            grp: dict[str, list[int]] = {"season_ids": [], "episode_ids": []}
            auto_groups[h.media_item_id] = grp
            work_items.append(("auto", h.media_item_id, grp))
        else:
            grp = auto_groups[h.media_item_id]
        if h.episode_id is not None:
            grp["episode_ids"].append(h.episode_id)
        elif h.season_id is not None:
            grp["season_ids"].append(h.season_id)

    resumed = 0
    for mode, item_id, payload in work_items:
        if torbox.get_cooldown_remaining(is_manual=True) > 0:
            break
        if not item_lock_manager.try_acquire(item_id, owner="background"):
            continue  # busy — retried on the next tick
        try:
            async with async_session_factory() as session:
                item = await session.get(MediaItem, item_id)
                if item is None:
                    continue
                transfer_poller.register_in_flight_push(item_id, item.title)
                if mode == "manual":
                    from app.core.push_engine import execute_manual_grab

                    logger.info(
                        "▶️ Resuming rate-limited manual pick for '%s' (%s).",
                        item.title,
                        payload.get("title"),
                    )
                    await execute_manual_grab(session, item_id, dict(payload))
                else:
                    logger.info(
                        "▶️ Resuming rate-limited Auto-Push for '%s' (%d target(s)).",
                        item.title,
                        len(payload["season_ids"]) + len(payload["episode_ids"]) or 1,
                    )
                    await execute_auto_push(session, item_id, dict(payload))
                resumed += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Resuming rate-limited push for item %s failed: %s", item_id, exc
            )
        finally:
            transfer_poller.unregister_in_flight_push(item_id)
            item_lock_manager.release(item_id)

    async with async_session_factory() as session:
        if await count_rate_limited_pushes(session) == 0:
            rate_limit_queue.sleep()
    return resumed


async def run_rate_limit_resume_tick() -> None:
    """APScheduler entry point (cheap in-memory cooldown check first)."""
    if torbox.get_cooldown_remaining(is_manual=True) > 0:
        return
    try:
        await resume_rate_limited_pushes()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Rate-limit resume tick failed: %s", exc)
