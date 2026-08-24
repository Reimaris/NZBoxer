"""
Self Healing Cycle
==================
Checks active downloads in TorBox. If a download has failed or errored out,
it removes the associated DownloadHistory entry and blacklists the NZB so that
the next automation cycle will pick the next best release.

Also provides run_download_check_cycle() which checks the current TorBox
download status and transitions DOWNLOADING items to DOWNLOADED/COMPLETED once done.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.core.logging_config import log_process_end, log_process_start
from app.db.database import async_session_factory
from app.db.models import (
    BlacklistedRelease,
    DownloadHistory,
    Episode,
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    MediaType,
    ProviderProfile,
    Season,
    SeasonStatus,
)
from app.services import telegram, torbox

logger = logging.getLogger(__name__)


async def run_self_healing_cycle() -> None:
    """Checks all active downloads in TorBox for failures."""
    log_process_start(logger, "Self-Healing Engine")
    try:
        if not settings.torbox_api_key:
            logger.debug("TorBox API key missing. Skipping self-healing cycle.")
            return

        logger.info("🔧 Starte Self-Healing-Zyklus...")

        async with async_session_factory() as session:
            stmt = (
                select(MediaItem)
                .where(MediaItem.status == MediaStatus.DOWNLOADING)
                .options(selectinload(MediaItem.download_history))
            )
            movies = (await session.execute(stmt)).scalars().all()

            stmt_seasons = (
                select(Season)
                .where(Season.status == SeasonStatus.DOWNLOADING)
                .options(
                    selectinload(Season.download_history),
                    selectinload(Season.media_item),
                )
            )
            seasons = (await session.execute(stmt_seasons)).scalars().all()

            stmt_episodes = (
                select(Episode)
                .where(Episode.status == EpisodeStatus.DOWNLOADING)
                .options(
                    selectinload(Episode.download_history),
                    selectinload(Episode.season).selectinload(Season.media_item),
                )
            )
            episodes = (await session.execute(stmt_episodes)).scalars().all()

            items_to_check: list[
                tuple[MediaItem | Season | Episode, DownloadHistory]
            ] = []
            for m in movies:
                if m.download_history:
                    latest = max(
                        m.download_history,
                        key=lambda h: (
                            h.torbox_sent_at
                            or datetime.min.replace(tzinfo=timezone.utc)
                        ),
                    )
                    items_to_check.append((m, latest))

            for s in seasons:
                if s.download_history:
                    latest = max(
                        s.download_history,
                        key=lambda h: (
                            h.torbox_sent_at
                            or datetime.min.replace(tzinfo=timezone.utc)
                        ),
                    )
                    items_to_check.append((s, latest))

            for e in episodes:
                if e.download_history:
                    latest = max(
                        e.download_history,
                        key=lambda h: (
                            h.torbox_sent_at
                            or datetime.min.replace(tzinfo=timezone.utc)
                        ),
                    )
                    items_to_check.append((e, latest))

            for target, history in items_to_check:
                if not history.torbox_id:
                    continue

                status_res = await torbox.check_download_status(history.torbox_id)
                status = status_res.get("status", "")

                logger.debug("TorBox status for %s: %s", history.nzb_title, status)

                if status in ("failed", "error", "not_found"):
                    logger.warning(
                        "⚠️ Self-Healing: Download failed for %s",
                        history.nzb_title,
                    )

                    media_item_id = (
                        target.id
                        if isinstance(target, MediaItem)
                        else (
                            target.media_item_id
                            if isinstance(target, Season)
                            else target.season.media_item_id
                        )
                    )
                    blacklist_entry = BlacklistedRelease(
                        media_item_id=media_item_id,
                        nzb_guid=history.nzb_guid,
                        nzb_title=history.nzb_title,
                        reason=f"TorBox reported status: {status}",
                    )
                    session.add(blacklist_entry)

                    await session.delete(history)

                    if isinstance(target, MediaItem):
                        target.status = MediaStatus.SEARCHING
                        title_for_log = target.title
                        media_item = target
                    elif isinstance(target, Season):
                        target.status = SeasonStatus.SEARCHING
                        title_for_log = target.media_item.title
                        media_item = target.media_item
                    else:
                        target.status = EpisodeStatus.SEARCHING
                        title_for_log = f"{target.season.media_item.title} S{target.season.season_number:02d}E{target.episode_number:02d}"
                        media_item = target.season.media_item

                    profile_stmt = (
                        select(ProviderProfile)
                        .where(
                            ProviderProfile.provider_id == media_item.provider_id,
                            ProviderProfile.media_type
                            == (
                                "movies"
                                if media_item.media_type == MediaType.MOVIE
                                else "shows"
                            ),
                        )
                        .options(selectinload(ProviderProfile.notification_channel))
                    )
                    profile_res = await session.execute(profile_stmt)
                    profile = profile_res.scalar_one_or_none()

                    if profile and profile.notification_channel:
                        channel = profile.notification_channel
                        if (
                            channel.type == "telegram"
                            and channel.bot_token
                            and channel.chat_id
                        ):
                            msg = f"⚠️ <b>Self-Healing Triggered</b>\n\n<b>{title_for_log}</b>\nTorBox download failed.\n<code>{history.nzb_title}</code> has been blacklisted. The next best release will be grabbed on the next cycle."
                            await telegram.send_notification(
                                msg, token=channel.bot_token, chat_id=channel.chat_id
                            )

            await session.commit()

        logger.info("✅ Self-Healing-Zyklus abgeschlossen.")
    finally:
        log_process_end(logger, "Self-Healing Engine")


async def run_download_check_cycle() -> None:
    """Checks TorBox for all DOWNLOADING items and updates their status.

    - completed / cached → set to DOWNLOADED or COMPLETED (if score >= target)
    - downloading / queued → leave as DOWNLOADING (still in progress)
    - failed / error / not_found → hand off to self-healing (blacklist + revert)
    """
    log_process_start(logger, "Download Check Engine")
    try:
        if not settings.torbox_api_key:
            logger.debug("TorBox API key missing. Skipping download check.")
            return

        logger.info("🔎 Checking download status on TorBox...")

        try:
            import httpx

            headers = {"Authorization": f"Bearer {settings.torbox_api_key}"}
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    "https://api.torbox.app/v1/api/usenet/mylist", headers=headers
                )
                resp.raise_for_status()
                tb_data = resp.json()
        except Exception as e:
            logger.error("❌ Konnte TorBox-Liste nicht abrufen: %s", e)
            return

        if not tb_data.get("success"):
            logger.error("❌ TorBox API Error: %s", tb_data.get("detail"))
            return

        tb_items: dict[str, dict] = {}
        for item in tb_data.get("data", []) or []:
            tb_items[str(item.get("id", ""))] = item

        async with async_session_factory() as session:
            from app.config import scoring_config

            cutoffs = scoring_config.get("cutoffs", {})
            target_score = cutoffs.get("target_score", 2500)

            stmt = (
                select(MediaItem)
                .where(MediaItem.status == MediaStatus.DOWNLOADING)
                .options(selectinload(MediaItem.download_history))
            )
            movies = (await session.execute(stmt)).scalars().all()

            stmt_s = (
                select(Season)
                .where(Season.status == SeasonStatus.DOWNLOADING)
                .options(
                    selectinload(Season.download_history),
                    selectinload(Season.media_item),
                )
            )
            seasons = (await session.execute(stmt_s)).scalars().all()

            stmt_e = (
                select(Episode)
                .where(Episode.status == EpisodeStatus.DOWNLOADING)
                .options(
                    selectinload(Episode.download_history),
                    selectinload(Episode.season).selectinload(Season.media_item),
                )
            )
            episodes = (await session.execute(stmt_e)).scalars().all()

            def _get_tb_status(history: DownloadHistory) -> tuple[str, dict]:
                if not history.torbox_id:
                    return "no_id", {}
                tb = tb_items.get(str(history.torbox_id), {})
                if not tb:
                    return "not_found", {}
                return tb.get("download_state", "unknown"), tb

            changed = 0

            def _handle_status_update(
                target,
                history,
                status,
                title,
                media_item_id,
                searching_status,
                completed_status,
                downloaded_status,
            ):
                nonlocal changed
                if status in ("completed", "cached", "paused"):
                    is_completed = (
                        history.score is not None and history.score >= target_score
                    )
                    final_status = (
                        completed_status if is_completed else downloaded_status
                    )
                    status_str = "COMPLETED" if is_completed else "DOWNLOADED"
                    logger.info(
                        "    ✅ %s → %s (TorBox: %s)", title, status_str, status
                    )
                    target.status = final_status
                    changed += 1
                elif status in ("failed", "error"):
                    logger.warning(
                        "    ⚠️ %s → fehlgeschlagen (%s), wird erneut gesucht.",
                        title,
                        status,
                    )
                    bl = BlacklistedRelease(
                        media_item_id=media_item_id,
                        nzb_guid=history.nzb_guid,
                        nzb_title=history.nzb_title,
                        reason=f"TorBox: {status}",
                    )
                    session.add(bl)
                    # We cannot await inside this sync helper, so we mark it for deletion outside
                    return True  # mark as failed
                elif status == "not_found":
                    is_completed = (
                        history.score is not None and history.score >= target_score
                    )
                    final_status = (
                        completed_status if is_completed else downloaded_status
                    )
                    status_str = "COMPLETED" if is_completed else "DOWNLOADED"
                    logger.info(
                        "    ✅ %s → %s (nicht mehr in TorBox-Liste)", title, status_str
                    )
                    target.status = final_status
                    changed += 1
                else:
                    logger.info(
                        "    ⏳ %s → still downloading (TorBox: %s)", title, status
                    )
                return False

            for movie in movies:
                if not movie.download_history:
                    continue
                latest = max(
                    movie.download_history,
                    key=lambda h: (
                        h.torbox_sent_at or datetime.min.replace(tzinfo=timezone.utc)
                    ),
                )
                status, tb = _get_tb_status(latest)
                if _handle_status_update(
                    movie,
                    latest,
                    status,
                    movie.title,
                    movie.id,
                    MediaStatus.SEARCHING,
                    MediaStatus.COMPLETED,
                    MediaStatus.DOWNLOADED,
                ):
                    await session.delete(latest)
                    movie.status = MediaStatus.SEARCHING
                    changed += 1

            for season in seasons:
                if not season.download_history:
                    continue
                latest = max(
                    season.download_history,
                    key=lambda h: (
                        h.torbox_sent_at or datetime.min.replace(tzinfo=timezone.utc)
                    ),
                )
                status, tb = _get_tb_status(latest)
                title = f"{season.media_item.title} S{season.season_number:02d}"
                if _handle_status_update(
                    season,
                    latest,
                    status,
                    title,
                    season.media_item_id,
                    SeasonStatus.SEARCHING,
                    SeasonStatus.COMPLETED,
                    SeasonStatus.DOWNLOADED,
                ):
                    await session.delete(latest)
                    season.status = SeasonStatus.SEARCHING
                    changed += 1

            for episode in episodes:
                if not episode.download_history:
                    continue
                latest = max(
                    episode.download_history,
                    key=lambda h: (
                        h.torbox_sent_at or datetime.min.replace(tzinfo=timezone.utc)
                    ),
                )
                status, tb = _get_tb_status(latest)
                title = f"{episode.season.media_item.title} S{episode.season.season_number:02d}E{episode.episode_number:02d}"
                if _handle_status_update(
                    episode,
                    latest,
                    status,
                    title,
                    episode.season.media_item_id,
                    EpisodeStatus.SEARCHING,
                    EpisodeStatus.COMPLETED,
                    EpisodeStatus.DOWNLOADED,
                ):
                    await session.delete(latest)
                    episode.status = EpisodeStatus.SEARCHING
                    changed += 1

            await session.commit()

        if changed:
            logger.info(
                "🔎 Download-Check abgeschlossen: %d Status aktualisiert.", changed
            )
        else:
            logger.info("🔎 Download check finished: No changes.")
    finally:
        log_process_end(logger, "Download Check Engine")
