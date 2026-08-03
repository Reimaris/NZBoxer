"""
Self Healing Cycle
==================
Checks active downloads in TorBox. If a download has failed or errored out,
it removes the associated DownloadHistory entry and blacklists the NZB so that
the next automation cycle will pick the next best release.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.database import async_session_factory
from app.db.models import BlacklistedRelease, DownloadHistory, MediaItem, MediaStatus, Season, SeasonStatus, ProviderProfile
from app.services import torbox, telegram

logger = logging.getLogger(__name__)


async def run_self_healing_cycle() -> None:
    """Checks all active downloads in TorBox for failures."""
    if not settings.torbox_api_key:
        logger.debug("TorBox API key missing. Skipping self-healing cycle.")
        return
        
    logger.info("Starting self-healing cycle...")

    async with async_session_factory() as session:
        # We look for DownloadHistory items that were recently sent, maybe we only check
        # those associated with items that are in 'downloaded' status (meaning not yet 'completed'
        # or maybe we check all that don't have a 'completed' status in torbox).
        # Actually, TorBox Usenet downloads are pretty fast.
        
        # Let's get all MediaItems and Seasons that are currently in DOWNLOADED status.
        stmt = select(MediaItem).where(MediaItem.status == MediaStatus.DOWNLOADED).options(selectinload(MediaItem.download_history))
        movies = (await session.execute(stmt)).scalars().all()
        
        stmt_seasons = select(Season).where(Season.status == SeasonStatus.DOWNLOADED).options(selectinload(Season.download_history), selectinload(Season.media_item))
        seasons = (await session.execute(stmt_seasons)).scalars().all()
        
        items_to_check = []
        for m in movies:
            if m.download_history:
                # check the latest download
                latest = max(m.download_history, key=lambda h: h.torbox_sent_at or datetime.min.replace(tzinfo=timezone.utc))
                items_to_check.append((m, latest))
                
        for s in seasons:
            if s.download_history:
                latest = max(s.download_history, key=lambda h: h.torbox_sent_at or datetime.min.replace(tzinfo=timezone.utc))
                items_to_check.append((s, latest))
                
        for target, history in items_to_check:
            if not history.torbox_id:
                continue
                
            status_res = await torbox.check_download_status(history.torbox_id)
            status = status_res.get("status", "")
            
            logger.debug("TorBox status for %s: %s", history.nzb_title, status)
            
            if status in ("failed", "error", "not_found"):
                # Self healing triggered!
                logger.warning("Self-Healing triggered! Download failed for %s", history.nzb_title)
                
                # 1. Blacklist it
                blacklist_entry = BlacklistedRelease(
                    media_item_id=target.id if isinstance(target, MediaItem) else target.media_item_id,
                    nzb_guid=history.nzb_guid,
                    nzb_title=history.nzb_title,
                    reason=f"TorBox reported status: {status}"
                )
                session.add(blacklist_entry)
                
                # 2. Delete history
                await session.delete(history)
                
                # 3. Revert status to SEARCHING so automation picks it up again
                target.status = MediaStatus.SEARCHING if isinstance(target, MediaItem) else SeasonStatus.SEARCHING
                
                # 4. Notify
                title_for_log = target.title if isinstance(target, MediaItem) else target.media_item.title
                
                media_item = target if isinstance(target, MediaItem) else target.media_item
                profile_stmt = select(ProviderProfile).where(
                    ProviderProfile.provider_id == media_item.provider_id,
                    ProviderProfile.media_type == ("movies" if media_item.media_type == MediaType.MOVIE else "shows")
                ).options(selectinload(ProviderProfile.notification_channel))
                profile_res = await session.execute(profile_stmt)
                profile = profile_res.scalar_one_or_none()
                
                if profile and profile.notification_channel:
                    channel = profile.notification_channel
                    if channel.type == "telegram" and channel.bot_token and channel.chat_id:
                        msg = f"⚠️ <b>Self-Healing Triggered</b>\n\n<b>{title_for_log}</b>\nTorBox download failed.\n<code>{history.nzb_title}</code> has been blacklisted. The next best release will be grabbed on the next cycle."
                        await telegram.send_notification(msg, token=channel.bot_token, chat_id=channel.chat_id)
                
        await session.commit()
    
    logger.info("Self-healing cycle completed.")
