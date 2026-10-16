"""Best-effort Jellyseerr/Overseerr availability callback (m68, ADR-149).

Fire-and-forget: a failure is logged and never touches download history or the poller.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.log_redaction import redact_text
from app.db.database import async_session_factory
from app.db.models import (
    ACTIVE_LIST_STATUSES,
    MediaItem,
    MediaItemWatchlistSource,
    MediaType,
    Provider,
    SeasonStatus,
    SystemSettings,
)
from app.services.watchlists import jellyseerr

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task[None]] = set()
_SEERR_TYPES = ("jellyseerr", "overseerr")
_CALL_TIMEOUT_SECONDS = 10


def schedule_availability_notification(media_item_id: int) -> None:
    """Run :func:`notify_availability` in the background, keeping a task reference."""
    task = asyncio.create_task(notify_availability(media_item_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def notify_availability(media_item_id: int) -> None:
    """Tell qualifying Seerr sources that *media_item_id* is available. Never raises."""
    try:
        await _notify(media_item_id)
    except Exception as exc:  # noqa: BLE001 - best effort by design
        logger.warning(
            "[AVAILABILITY] item %s: %s", media_item_id, redact_text(str(exc))
        )


async def _notify(media_item_id: int) -> None:
    async with async_session_factory() as session:
        settings = (
            await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
        ).scalar_one_or_none()
        if settings is not None and settings.dry_run:
            return

        item = (
            await session.execute(
                select(MediaItem)
                .where(MediaItem.id == media_item_id)
                .options(selectinload(MediaItem.seasons))
            )
        ).scalar_one_or_none()
        if item is None:
            return
        if item.media_type != MediaType.MOVIE:
            seasons = [s for s in item.seasons if s.status != SeasonStatus.IGNORED]
            if not seasons or any(s.status != SeasonStatus.COMPLETED for s in seasons):
                return

        rows = (
            await session.execute(
                select(MediaItemWatchlistSource, Provider)
                .join(Provider, Provider.id == MediaItemWatchlistSource.provider_id)
                .where(
                    MediaItemWatchlistSource.media_item_id == media_item_id,
                    MediaItemWatchlistSource.list_status.in_(ACTIVE_LIST_STATUSES),
                    MediaItemWatchlistSource.availability_notified_at.is_(None),
                    Provider.type.in_(_SEERR_TYPES),
                    Provider.is_active.is_(True),
                )
            )
        ).all()

        for source, provider in rows:
            cfg = provider.watchlist_config
            if not cfg.get("notify_available") or not source.remote_id.isdigit():
                continue
            try:
                await asyncio.wait_for(
                    jellyseerr.mark_available(
                        cfg["server_url"], cfg["api_key"], int(source.remote_id)
                    ),
                    _CALL_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 - includes TimeoutError
                logger.warning(
                    "[AVAILABILITY] %s: %s",
                    provider.name,
                    redact_text(str(exc) or type(exc).__name__),
                )
                continue
            source.availability_notified_at = datetime.now(timezone.utc)
            await session.commit()
            logger.info(
                "[AVAILABILITY] Notified %s that media %s is available",
                provider.name,
                source.remote_id,
            )
