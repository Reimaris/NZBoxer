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
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging_config import log_process_end, log_process_start
from app.db.database import async_session_factory
from app.db.models import (
    BlacklistedRelease,
    BookItem,
    DownloadHistory,
    Episode,
    EpisodeStatus,
    MangaVolume,
    MediaItem,
    MediaStatus,
    MediaType,
    ProviderProfile,
    Season,
    SeasonStatus,
    SeenTorboxDownload,
    SystemSettings,
)
from app.services import telegram, torbox

logger = logging.getLogger(__name__)


async def count_downloading_entities(session: AsyncSession) -> int:
    """Counts active downloading entities across all supported media models."""
    movies_c = (
        await session.execute(
            select(func.count(MediaItem.id)).where(
                MediaItem.status == MediaStatus.DOWNLOADING
            )
        )
    ).scalar() or 0

    seasons_c = (
        await session.execute(
            select(func.count(Season.id)).where(
                Season.status == SeasonStatus.DOWNLOADING
            )
        )
    ).scalar() or 0

    episodes_c = (
        await session.execute(
            select(func.count(Episode.id)).where(
                Episode.status == EpisodeStatus.DOWNLOADING
            )
        )
    ).scalar() or 0

    manga_c = (
        await session.execute(
            select(func.count(MangaVolume.id)).where(
                MangaVolume.status == EpisodeStatus.DOWNLOADING
            )
        )
    ).scalar() or 0

    books_c = (
        await session.execute(
            select(func.count(BookItem.id)).where(
                BookItem.status == MediaStatus.DOWNLOADING
            )
        )
    ).scalar() or 0

    return int(movies_c + seasons_c + episodes_c + manga_c + books_c)


async def run_self_healing_cycle(
    pre_fetched_downloads: list[dict[str, Any]] | None = None,
) -> None:
    """Checks all active downloads in TorBox for failures."""
    log_process_start(logger, "Self-Healing Engine")
    try:
        tb_key = await torbox.resolve_api_key()
        if not tb_key:
            logger.debug("TorBox API key missing. Skipping self-healing cycle.")
            return

        async with async_session_factory() as session:
            if pre_fetched_downloads is None:
                active_count = await count_downloading_entities(session)
                if active_count == 0:
                    logger.info(
                        "⏩ No active downloads found in DB. Bypassing Self-Healing TorBox polling."
                    )
                    return
                raw_downloads = await torbox.get_usenet_downloads(session=session)
            else:
                raw_downloads = pre_fetched_downloads

            settings_stmt = select(SystemSettings).where(SystemSettings.id == 1)
            sys_settings = (await session.execute(settings_stmt)).scalars().first()
            download_timeout_hours = (
                sys_settings.download_timeout_hours if sys_settings else 24
            )

            logger.info(
                "🔧 Starting Self-Healing cycle (timeout: %dh)...",
                download_timeout_hours,
            )

            tb_map: dict[str, dict[str, Any]] = {}
            for d in raw_downloads:
                if d.get("id") is not None:
                    tb_map[str(d["id"])] = d

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

            now_utc = datetime.now(timezone.utc)

            for target, history in items_to_check:
                if not history.torbox_id:
                    continue

                tb_item = tb_map.get(str(history.torbox_id))
                if tb_item is not None:
                    status = tb_item.get("download_state", "unknown")
                else:
                    sent_at = history.torbox_sent_at
                    if sent_at and sent_at.tzinfo is None:
                        sent_at = sent_at.replace(tzinfo=timezone.utc)
                    if sent_at and (now_utc - sent_at).total_seconds() < 900:
                        logger.debug(
                            "Download %s for '%s' missing from TorBox but within 15m grace period (%ss elapsed). Skipping.",
                            history.torbox_id,
                            history.nzb_title,
                            int((now_utc - sent_at).total_seconds()),
                        )
                        continue
                    status = "not_found"

                logger.debug("TorBox status for %s: %s", history.nzb_title, status)

                status_lower = status.lower()
                is_stalled = False
                sent_at = history.torbox_sent_at
                if sent_at and sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)

                if tb_item is not None and status_lower in (
                    "downloading",
                    "processing",
                    "queued",
                    "unknown",
                ):
                    if (
                        sent_at
                        and (now_utc - sent_at).total_seconds()
                        > download_timeout_hours * 3600
                    ):
                        is_stalled = True

                if is_stalled or status_lower in (
                    "failed",
                    "error",
                    "not_found",
                    "aborted",
                    "cannot be re-completed",
                    "not enough repair blocks",
                ):
                    from app.core.failure_logger import log_failure

                    if is_stalled:
                        elapsed_h = (
                            int((now_utc - sent_at).total_seconds() // 3600)
                            if sent_at
                            else 0
                        )
                        reason = f"Stalled download exceeded timeout ({download_timeout_hours}h)"
                        log_msg = f"TorBox download stalled past {download_timeout_hours}h limit ({elapsed_h}h elapsed)"
                        logger.warning(
                            "⚠️ Self-Healing: Stalled download detected for '%s' (%dh elapsed > %dh limit). Purging and blacklisting.",
                            history.nzb_title,
                            elapsed_h,
                            download_timeout_hours,
                        )
                    else:
                        reason = (
                            "TorBox download missing (not_found)"
                            if status_lower == "not_found"
                            else f"TorBox reported status: {status}"
                        )
                        log_msg = f"TorBox download failed: {status}"
                        logger.warning(
                            "⚠️ Self-Healing: Download failed for %s (%s)",
                            history.nzb_title,
                            status,
                        )

                    log_failure(
                        session,
                        target,
                        "torbox_error",
                        log_msg,
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
                        reason=reason,
                    )
                    session.add(blacklist_entry)

                    # Delete from TorBox
                    if history.torbox_id:
                        tb_type = (
                            tb_item.get("_type", "usenet") if tb_item else "usenet"
                        )
                        try:
                            if tb_type == "torrent":
                                await torbox.delete_torrent_download(
                                    int(history.torbox_id), session=session
                                )
                            else:
                                await torbox.delete_usenet_download(
                                    int(history.torbox_id), session=session
                                )
                        except Exception as e:
                            logger.error(f"Failed to delete {tb_type} from TorBox: {e}")

                        # Purge from SeenTorboxDownload cache
                        await session.execute(
                            delete(SeenTorboxDownload).where(
                                SeenTorboxDownload.torbox_id == str(history.torbox_id)
                            )
                        )

                    await session.delete(history)

                    if isinstance(target, MediaItem):
                        target.status = MediaStatus.SEARCHING
                        if hasattr(target, "pending_candidate_json"):
                            target.pending_candidate_json = None
                        title_for_log = target.title
                        media_item = target
                    elif isinstance(target, Season):
                        target.status = SeasonStatus.SEARCHING
                        if hasattr(target, "pending_candidate_json"):
                            target.pending_candidate_json = None
                        title_for_log = target.media_item.title
                        media_item = target.media_item
                    else:
                        target.status = EpisodeStatus.SEARCHING
                        if hasattr(target, "pending_candidate_json"):
                            target.pending_candidate_json = None
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
                    profile = profile_res.scalars().first()

                    if profile and profile.notification_channel:
                        channel = profile.notification_channel
                        if (
                            channel.type == "telegram"
                            and channel.bot_token
                            and channel.chat_id
                        ):
                            if is_stalled:
                                msg = f"⚠️ <b>Self-Healing Triggered (Timeout)</b>\n\n<b>{title_for_log}</b>\nTorBox download stalled past {download_timeout_hours}h limit.\n<code>{history.nzb_title}</code> has been purged and blacklisted. The next best release will be grabbed on the next cycle."
                            else:
                                msg = f"⚠️ <b>Self-Healing Triggered</b>\n\n<b>{title_for_log}</b>\nTorBox download failed.\n<code>{history.nzb_title}</code> has been blacklisted. The next best release will be grabbed on the next cycle."
                            await telegram.send_notification(
                                msg, token=channel.bot_token, chat_id=channel.chat_id
                            )

            await session.commit()

            # --- Auto-Adoption from TorBox ---
            try:
                if raw_downloads:
                    await sync_torbox_cache(session, raw_downloads)
                    await adopt_torbox_downloads_for_video(session)
                    await adopt_torbox_downloads_for_print(session)
            except Exception as e:
                logger.error("Error during TorBox cache sync and adoption: %s", e)

        logger.info("✅ Self-Healing cycle complete.")
    finally:
        log_process_end(logger, "Self-Healing Engine")


async def run_download_check_cycle(
    pre_fetched_downloads: list[dict[str, Any]] | None = None,
) -> None:
    """Checks TorBox for all DOWNLOADING items and updates their status.

    - completed / cached → set to DOWNLOADED or COMPLETED (if score >= target)
    - downloading / queued → leave as DOWNLOADING (still in progress)
    - failed / error / aborted / cannot be re-completed / not enough repair blocks / not_found → hand off to self-healing (blacklist + revert)
    """
    log_process_start(logger, "Download Check Engine")
    try:
        tb_key = await torbox.resolve_api_key()
        if not tb_key:
            logger.debug("TorBox API key missing. Skipping download check.")
            return

        async with async_session_factory() as session:
            if pre_fetched_downloads is None:
                active_count = await count_downloading_entities(session)
                if active_count == 0:
                    logger.info(
                        "⏩ No active downloads found in DB. Bypassing Download Check TorBox polling."
                    )
                    return
                raw_downloads = await torbox.get_usenet_downloads(session=session)
            else:
                raw_downloads = pre_fetched_downloads

            logger.info("🔎 Checking download status on TorBox...")

            tb_items: dict[str, dict] = {}
            for item in raw_downloads:
                item["_type"] = "usenet"
                if item.get("id") is not None:
                    tb_items[str(item.get("id"))] = item

            # Fetch Torrents via service
            try:
                raw_torrents = await torbox.get_torrent_downloads(session=session)
                for item in raw_torrents:
                    item["_type"] = "torrent"
                    if item.get("id") is not None:
                        tb_items[str(item.get("id"))] = item
            except Exception as e:
                logger.debug("Failed to fetch torrent downloads in check cycle: %s", e)

            import re

            from app.config import scoring_config
            from app.core.reading_scorer import (
                detect_print_format,
                match_volume_or_issue,
                score_print_release,
            )

            def clean_title(title: str) -> str:
                return re.sub(r"[^a-z0-9]", "", title.lower())

            settings_stmt = select(SystemSettings).where(SystemSettings.id == 1)
            sys_settings = (await session.execute(settings_stmt)).scalars().first()
            download_timeout_hours = (
                sys_settings.download_timeout_hours if sys_settings else 24
            )

            cutoffs = scoring_config.get("cutoffs", {})
            target_score = cutoffs.get("target_score", 8000)

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

            stmt_b = select(BookItem).where(BookItem.status == MediaStatus.DOWNLOADING)
            books = (await session.execute(stmt_b)).scalars().all()

            stmt_mv = (
                select(MangaVolume)
                .where(MangaVolume.status == EpisodeStatus.DOWNLOADING)
                .options(selectinload(MangaVolume.manga))
            )
            manga_volumes = (await session.execute(stmt_mv)).scalars().all()

            def _get_tb_status(history: DownloadHistory) -> tuple[str, dict]:
                tb = None
                if history.torbox_id:
                    tb = tb_items.get(str(history.torbox_id))

                if not tb and history.torbox_hash:
                    for tb_item in tb_items.values():
                        if (
                            str(tb_item.get("hash", "")).lower()
                            == history.torbox_hash.lower()
                        ):
                            history.torbox_id = str(tb_item.get("id"))
                            tb = tb_item
                            break

                if not tb and history.nzb_title:
                    for tb_item in tb_items.values():
                        tb_name = str(tb_item.get("name") or tb_item.get("title") or "")
                        # Often TorBox normalizes the name slightly, check substring
                        if tb_name and history.nzb_title.lower() in tb_name.lower():
                            history.torbox_id = str(tb_item.get("id"))
                            tb = tb_item
                            break
                        # Inverse substring just in case
                        if tb_name and tb_name.lower() in history.nzb_title.lower():
                            history.torbox_id = str(tb_item.get("id"))
                            tb = tb_item
                            break

                if not tb:
                    if not history.torbox_id:
                        return "no_id", {}
                    now_utc = datetime.now(timezone.utc)
                    sent_at = history.torbox_sent_at
                    if sent_at and sent_at.tzinfo is None:
                        sent_at = sent_at.replace(tzinfo=timezone.utc)
                    if sent_at and (now_utc - sent_at).total_seconds() < 900:
                        return "downloading", {}
                    return "not_found", {}

                return tb.get("download_state", "unknown"), tb

            changed = 0

            async def _handle_status_update(
                target,
                history,
                tb_info,
                status,
                title,
                media_item_id,
                searching_status,
                completed_status,
                downloaded_status,
            ):
                nonlocal changed
                status_lower = status.lower()

                if status_lower in ("completed", "cached", "paused"):
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
                elif (
                    status_lower.startswith("failed")
                    or status_lower.startswith("error")
                    or "aborted" in status_lower
                    or "repair failed" in status_lower
                    or "not-complete" in status_lower
                    or status_lower
                    in (
                        "cannot be re-completed",
                        "not enough repair blocks",
                        "not_found",
                    )
                ):
                    is_not_found = status_lower == "not_found"
                    reason = (
                        "TorBox download missing (not_found)"
                        if is_not_found
                        else f"TorBox: {status}"
                    )
                    if is_not_found:
                        logger.warning(
                            "    ⚠️ Download missing from TorBox: %s, returning to search queue.",
                            title,
                        )
                    else:
                        logger.warning(
                            "    ⚠️ %s → failed (%s), returning to search queue.",
                            title,
                            status,
                        )
                    from app.core.failure_logger import log_failure
                    from app.services import torbox

                    log_failure(
                        session,
                        target,
                        "torbox_error",
                        f"TorBox download failed: {status}",
                    )
                    bl = BlacklistedRelease(
                        media_item_id=media_item_id,
                        nzb_guid=history.nzb_guid,
                        nzb_title=history.nzb_title,
                        reason=reason,
                    )
                    session.add(bl)

                    if history.torbox_id:
                        tb_type = (
                            tb_info.get("_type", "usenet") if tb_info else "usenet"
                        )
                        try:
                            if tb_type == "usenet":
                                await torbox.delete_usenet_download(
                                    int(history.torbox_id), session=session
                                )
                            else:
                                await torbox.delete_torrent_download(
                                    int(history.torbox_id), session=session
                                )
                        except Exception as e:
                            logger.error(f"Failed to delete {tb_type} from TorBox: {e}")

                        # Purge from SeenTorboxDownload
                        await session.execute(
                            delete(SeenTorboxDownload).where(
                                SeenTorboxDownload.torbox_id == str(history.torbox_id)
                            )
                        )

                    # Revert target status and clear candidate
                    target.status = searching_status
                    if hasattr(target, "pending_candidate_json"):
                        target.pending_candidate_json = None
                    return True  # mark as failed
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
                if await _handle_status_update(
                    movie,
                    latest,
                    tb,
                    status,
                    movie.title,
                    movie.id,
                    MediaStatus.SEARCHING,
                    MediaStatus.COMPLETED,
                    MediaStatus.DOWNLOADED,
                ):
                    await session.delete(latest)
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
                if await _handle_status_update(
                    season,
                    latest,
                    tb,
                    status,
                    title,
                    season.media_item_id,
                    SeasonStatus.SEARCHING,
                    SeasonStatus.COMPLETED,
                    SeasonStatus.DOWNLOADED,
                ):
                    await session.delete(latest)
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
                if await _handle_status_update(
                    episode,
                    latest,
                    tb,
                    status,
                    title,
                    episode.season.media_item_id,
                    EpisodeStatus.SEARCHING,
                    EpisodeStatus.COMPLETED,
                    EpisodeStatus.DOWNLOADED,
                ):
                    await session.delete(latest)
                    changed += 1

            for book in books:
                expected_book = clean_title(book.title)
                matched_tb = None
                best_tb_score = -1.0
                for tb_item in tb_items.values():
                    tb_name = str(tb_item.get("name") or tb_item.get("title") or "")
                    if not tb_name:
                        continue
                    c_name = clean_title(tb_name)
                    if expected_book in c_name:
                        fmt = detect_print_format(tb_name, media_type="book")
                        sc = score_print_release(fmt, media_type="book")["score"]
                        st = str(
                            tb_item.get("download_state") or tb_item.get("status") or ""
                        ).lower()
                        priority_boost = (
                            10000 if st in ("completed", "cached", "paused") else 0
                        )
                        total_p = priority_boost + sc
                        if total_p > best_tb_score:
                            best_tb_score = total_p
                            matched_tb = tb_item

                now_utc = datetime.now(timezone.utc)
                sent_at = book.last_searched_at
                if sent_at and sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)

                if matched_tb is not None:
                    tb_status = str(
                        matched_tb.get("download_state")
                        or matched_tb.get("status")
                        or "unknown"
                    )
                else:
                    if sent_at and (now_utc - sent_at).total_seconds() < 900:
                        tb_status = "downloading"
                    else:
                        tb_status = "not_found"

                status_lower = tb_status.lower()
                if matched_tb is not None and status_lower in (
                    "completed",
                    "cached",
                    "paused",
                ):
                    tb_name = str(
                        matched_tb.get("name") or matched_tb.get("title") or ""
                    )
                    fmt = detect_print_format(tb_name, media_type="book")
                    score = score_print_release(fmt, media_type="book")["score"]
                    if book.best_score is None or score > book.best_score:
                        book.best_score = score
                    is_completed = (
                        book.best_score is not None and book.best_score >= 1000
                    )
                    book.status = (
                        MediaStatus.COMPLETED
                        if is_completed
                        else MediaStatus.DOWNLOADED
                    )
                    book.empty_search_count = 0
                    book.last_error = None
                    logger.info(
                        "    ✅ Book '%s' → %s (TorBox: %s)",
                        book.title,
                        book.status.value,
                        tb_status,
                    )
                    changed += 1
                elif matched_tb is not None and status_lower in (
                    "downloading",
                    "processing",
                    "queued",
                    "unknown",
                ):
                    if (
                        sent_at
                        and (now_utc - sent_at).total_seconds()
                        > download_timeout_hours * 3600
                    ):
                        logger.warning(
                            "    ⚠️ Stalled download detected for Book '%s' (%sh elapsed > %sh limit). Purging.",
                            book.title,
                            int((now_utc - sent_at).total_seconds() / 3600),
                            download_timeout_hours,
                        )
                        tb_id = matched_tb.get("id")
                        if tb_id:
                            tb_type = matched_tb.get("_type", "usenet")
                            try:
                                if tb_type == "torrent":
                                    await torbox.delete_torrent_download(
                                        int(tb_id), session=session
                                    )
                                else:
                                    await torbox.delete_usenet_download(
                                        int(tb_id), session=session
                                    )
                            except Exception as e:
                                logger.error(
                                    "Failed to delete stalled book from TorBox: %s", e
                                )
                            await session.execute(
                                delete(SeenTorboxDownload).where(
                                    SeenTorboxDownload.torbox_id == str(tb_id)
                                )
                            )
                        book.status = MediaStatus.SEARCHING
                        from app.core.failure_logger import log_failure

                        log_failure(
                            session,
                            book,
                            "stalled_timeout",
                            f"TorBox download stalled past {download_timeout_hours}h limit",
                        )
                        changed += 1
                    else:
                        logger.info(
                            "    ⏳ Book '%s' → still downloading (TorBox: %s)",
                            book.title,
                            tb_status,
                        )
                elif (
                    status_lower.startswith("failed")
                    or status_lower.startswith("error")
                    or "aborted" in status_lower
                    or "repair failed" in status_lower
                    or "not-complete" in status_lower
                    or status_lower
                    in (
                        "cannot be re-completed",
                        "not enough repair blocks",
                        "not_found",
                    )
                ):
                    is_not_found = status_lower == "not_found"
                    reason = (
                        "TorBox download missing (not_found)"
                        if is_not_found
                        else f"TorBox download failed: {tb_status}"
                    )
                    if is_not_found:
                        logger.warning(
                            "    ⚠️ Download missing from TorBox: Book '%s', returning to search queue.",
                            book.title,
                        )
                    else:
                        logger.warning(
                            "    ⚠️ Book '%s' → failed (%s), returning to search queue.",
                            book.title,
                            tb_status,
                        )
                    if matched_tb:
                        tb_id = matched_tb.get("id")
                        if tb_id:
                            tb_type = matched_tb.get("_type", "usenet")
                            try:
                                if tb_type == "torrent":
                                    await torbox.delete_torrent_download(
                                        int(tb_id), session=session
                                    )
                                else:
                                    await torbox.delete_usenet_download(
                                        int(tb_id), session=session
                                    )
                            except Exception as e:
                                logger.error(
                                    "Failed to delete failed book from TorBox: %s", e
                                )
                            await session.execute(
                                delete(SeenTorboxDownload).where(
                                    SeenTorboxDownload.torbox_id == str(tb_id)
                                )
                            )
                    book.status = MediaStatus.SEARCHING
                    from app.core.failure_logger import log_failure

                    log_failure(
                        session,
                        book,
                        "torbox_error",
                        reason,
                    )
                    changed += 1

            for vol in manga_volumes:
                manga = vol.manga
                if not manga:
                    continue
                expected_manga = clean_title(manga.title)
                vol_num = vol.volume_number

                matched_tb = None
                best_tb_score = -1.0
                for tb_item in tb_items.values():
                    tb_name = str(tb_item.get("name") or tb_item.get("title") or "")
                    if not tb_name:
                        continue
                    c_name = clean_title(tb_name)
                    if expected_manga in c_name:
                        is_match, _ = match_volume_or_issue(tb_name, vol_num)
                        if not is_match:
                            continue
                        fmt = detect_print_format(tb_name, media_type="manga")
                        sc = score_print_release(fmt, media_type="manga")["score"]
                        st = str(
                            tb_item.get("download_state") or tb_item.get("status") or ""
                        ).lower()
                        priority_boost = (
                            10000 if st in ("completed", "cached", "paused") else 0
                        )
                        total_p = priority_boost + sc
                        if total_p > best_tb_score:
                            best_tb_score = total_p
                            matched_tb = tb_item

                now_utc = datetime.now(timezone.utc)
                sent_at = vol.last_searched_at or manga.last_searched_at
                if sent_at and sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=timezone.utc)

                if matched_tb is not None:
                    tb_status = str(
                        matched_tb.get("download_state")
                        or matched_tb.get("status")
                        or "unknown"
                    )
                else:
                    if sent_at and (now_utc - sent_at).total_seconds() < 900:
                        tb_status = "downloading"
                    else:
                        tb_status = "not_found"

                status_lower = tb_status.lower()
                if matched_tb is not None and status_lower in (
                    "completed",
                    "cached",
                    "paused",
                ):
                    tb_name = str(
                        matched_tb.get("name") or matched_tb.get("title") or ""
                    )
                    fmt = detect_print_format(tb_name, media_type="manga")
                    score = score_print_release(fmt, media_type="manga")["score"]
                    if vol.best_score is None or score > vol.best_score:
                        vol.best_score = score
                    is_completed = vol.best_score is not None and vol.best_score >= 1000
                    vol.status = (
                        EpisodeStatus.COMPLETED
                        if is_completed
                        else EpisodeStatus.DOWNLOADED
                    )
                    vol.empty_search_count = 0
                    logger.info(
                        "    ✅ Manga '%s' Vol %d → %s (TorBox: %s)",
                        manga.title,
                        vol_num,
                        vol.status.value,
                        tb_status,
                    )
                    changed += 1
                elif matched_tb is not None and status_lower in (
                    "downloading",
                    "processing",
                    "queued",
                    "unknown",
                ):
                    if (
                        sent_at
                        and (now_utc - sent_at).total_seconds()
                        > download_timeout_hours * 3600
                    ):
                        logger.warning(
                            "    ⚠️ Stalled download detected for Manga '%s' Vol %d (%sh elapsed > %sh limit). Purging.",
                            manga.title,
                            vol_num,
                            int((now_utc - sent_at).total_seconds() / 3600),
                            download_timeout_hours,
                        )
                        tb_id = matched_tb.get("id")
                        if tb_id:
                            tb_type = matched_tb.get("_type", "usenet")
                            try:
                                if tb_type == "torrent":
                                    await torbox.delete_torrent_download(
                                        int(tb_id), session=session
                                    )
                                else:
                                    await torbox.delete_usenet_download(
                                        int(tb_id), session=session
                                    )
                            except Exception as e:
                                logger.error(
                                    "Failed to delete stalled manga from TorBox: %s", e
                                )
                            await session.execute(
                                delete(SeenTorboxDownload).where(
                                    SeenTorboxDownload.torbox_id == str(tb_id)
                                )
                            )
                        vol.status = EpisodeStatus.SEARCHING
                        from app.core.failure_logger import log_failure

                        log_failure(
                            session,
                            vol,
                            "stalled_timeout",
                            f"TorBox download stalled past {download_timeout_hours}h limit",
                        )
                        changed += 1
                    else:
                        logger.info(
                            "    ⏳ Manga '%s' Vol %d → still downloading (TorBox: %s)",
                            manga.title,
                            vol_num,
                            tb_status,
                        )
                elif (
                    status_lower.startswith("failed")
                    or status_lower.startswith("error")
                    or "aborted" in status_lower
                    or "repair failed" in status_lower
                    or "not-complete" in status_lower
                    or status_lower
                    in (
                        "cannot be re-completed",
                        "not enough repair blocks",
                        "not_found",
                    )
                ):
                    is_not_found = status_lower == "not_found"
                    reason = (
                        "TorBox download missing (not_found)"
                        if is_not_found
                        else f"TorBox download failed: {tb_status}"
                    )
                    if is_not_found:
                        logger.warning(
                            "    ⚠️ Download missing from TorBox: Manga '%s' Vol %d, returning to search queue.",
                            manga.title,
                            vol_num,
                        )
                    else:
                        logger.warning(
                            "    ⚠️ Manga '%s' Vol %d → failed (%s), returning to search queue.",
                            manga.title,
                            vol_num,
                            tb_status,
                        )
                    if matched_tb:
                        tb_id = matched_tb.get("id")
                        if tb_id:
                            tb_type = matched_tb.get("_type", "usenet")
                            try:
                                if tb_type == "torrent":
                                    await torbox.delete_torrent_download(
                                        int(tb_id), session=session
                                    )
                                else:
                                    await torbox.delete_usenet_download(
                                        int(tb_id), session=session
                                    )
                            except Exception as e:
                                logger.error(
                                    "Failed to delete failed manga from TorBox: %s", e
                                )
                            await session.execute(
                                delete(SeenTorboxDownload).where(
                                    SeenTorboxDownload.torbox_id == str(tb_id)
                                )
                            )
                    vol.status = EpisodeStatus.SEARCHING
                    from app.core.failure_logger import log_failure

                    log_failure(
                        session,
                        vol,
                        "torbox_error",
                        reason,
                    )
                    changed += 1

            await session.commit()

        if changed:
            logger.info("🔎 Download check complete: %d statuses updated.", changed)
        else:
            logger.info("🔎 Download check finished: No changes.")
    finally:
        log_process_end(logger, "Download Check Engine")


async def sync_torbox_cache(
    session, raw_downloads: list[dict], force_rescan: bool = False
) -> None:
    """Parses new/modified TorBox items into SeenTorboxDownload records.
    Ignores unchanged items to save CPU cycles.
    """
    from sqlalchemy import select

    from app.core.parser import parse_release_name
    from app.db.models import SeenTorboxDownload

    if not raw_downloads:
        return

    tb_ids = [str(d.get("id")) for d in raw_downloads if d.get("id")]
    if not tb_ids:
        return

    existing_stmt = select(SeenTorboxDownload).where(
        SeenTorboxDownload.torbox_id.in_(tb_ids)
    )
    existing_items = {
        item.torbox_id: item
        for item in (await session.execute(existing_stmt)).scalars().all()
    }

    changed = False

    for d in raw_downloads:
        tb_id = str(d.get("id"))
        if not tb_id:
            continue

        name = d.get("name") or d.get("title") or "Unknown"
        state = d.get("download_state") or d.get("status") or "unknown"
        size = d.get("size") or 0
        try:
            progress = float(d.get("progress") or 0.0)
        except ValueError:
            progress = 0.0

        existing = existing_items.get(tb_id)

        if existing:
            # Skip if nothing changed
            if (
                not force_rescan
                and existing.download_state == state
                and existing.progress == progress
            ):
                continue

            existing.download_state = state
            existing.progress = progress
            existing.size_bytes = size
            changed = True
        else:
            # Only parse if new
            parsed = parse_release_name(name)

            media_type = "unknown"
            if parsed.season is not None:
                if parsed.episode is not None:
                    media_type = "series_episode"
                else:
                    media_type = "series_season"
            elif parsed.resolution or parsed.video_codec:
                media_type = "movie"
            else:
                lower_name = name.lower()
                if (
                    ".epub" in lower_name
                    or ".pdf" in lower_name
                    or ".mobi" in lower_name
                    or ".azw3" in lower_name
                ):
                    media_type = "book"
                elif (
                    ".cbz" in lower_name
                    or ".cbr" in lower_name
                    or "manga" in lower_name
                ):
                    media_type = "manga"

            new_item = SeenTorboxDownload(
                torbox_id=tb_id,
                raw_title=name,
                parsed_title=parsed.title or name,
                parsed_year=parsed.year,
                season_number=parsed.season,
                episode_number=parsed.episode,
                media_type=media_type,
                download_state=state,
                size_bytes=size,
                progress=progress,
            )
            session.add(new_item)
            changed = True

    if changed:
        await session.commit()


async def adopt_torbox_downloads_for_video(session) -> None:
    """Auto-adopts SEARCHING video items using the local TorBox download cache."""
    from datetime import datetime, timezone

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.config import scoring_config
    from app.core.parser import parse_release_name
    from app.core.scorer import score_release
    from app.db.models import (
        DownloadHistory,
        Episode,
        EpisodeStatus,
        MediaItem,
        MediaStatus,
        MediaType,
        Season,
        SeasonStatus,
        SeenTorboxDownload,
    )

    target_score = scoring_config.get("cutoffs", {}).get("target_score", 8000)

    # 1. Fetch active cached video downloads
    stmt_td = select(SeenTorboxDownload).where(
        SeenTorboxDownload.media_type.in_(["movie", "series_season", "series_episode"]),
        SeenTorboxDownload.download_state.not_in(["failed", "error"]),
    )
    active_downloads = (await session.execute(stmt_td)).scalars().all()

    if not active_downloads:
        return

    import re

    def clean_title(title: str) -> str:
        return re.sub(r"[^a-z0-9]", "", title.lower())

    changed = False

    # 2. Match Movies
    stmt_m = select(MediaItem).where(
        MediaItem.media_type == MediaType.MOVIE,
        MediaItem.status == MediaStatus.SEARCHING,
    )
    movies = (await session.execute(stmt_m)).scalars().all()

    for movie in movies:
        expected = clean_title(movie.title)
        best_item = None
        best_score = -1.0
        best_parsed = None

        for d in active_downloads:
            if d.media_type != "movie":
                continue
            if not d.parsed_title or clean_title(d.parsed_title) != expected:
                continue
            if d.parsed_year and movie.year and d.parsed_year != movie.year:
                continue

            parsed = parse_release_name(d.raw_title)
            score_res = score_release(
                parsed,
                d.size_bytes or 0,
                expected_title=movie.title,
                expected_year=movie.year,
            )
            if not score_res.is_rejected and score_res.score > best_score:
                best_score = score_res.score
                best_item = d
                best_parsed = parsed

        if best_item and best_parsed:
            is_completed = best_score >= target_score
            new_status = (
                MediaStatus.COMPLETED if is_completed else MediaStatus.DOWNLOADED
            )
            if best_item.download_state in ("downloading", "queued", "processing"):
                new_status = MediaStatus.DOWNLOADING

            movie.status = new_status
            movie.empty_search_count = 0
            dh = DownloadHistory(
                media_item_id=movie.id,
                nzb_title=best_item.raw_title,
                score=best_score,
                size_bytes=best_item.size_bytes,
                resolution=best_parsed.resolution,
                video_codec=best_parsed.video_codec,
                audio_codec=best_parsed.audio_codec,
                source=best_parsed.source,
                torbox_id=best_item.torbox_id,
                torbox_sent_at=datetime.now(timezone.utc),
            )
            session.add(dh)
            changed = True

    # 3. TV Shows (Season Packs)
    stmt_s = (
        select(Season)
        .where(Season.status == SeasonStatus.SEARCHING)
        .options(selectinload(Season.media_item), selectinload(Season.episodes))
    )
    seasons = (await session.execute(stmt_s)).scalars().all()

    for season in seasons:
        expected = clean_title(season.media_item.title)
        best_item = None
        best_score = -1.0
        best_parsed = None

        for d in active_downloads:
            if d.media_type not in ("series_season", "series_episode"):
                continue
            if d.episode_number is not None:
                continue
            if not d.parsed_title or clean_title(d.parsed_title) != expected:
                continue
            if d.season_number != season.season_number:
                continue

            parsed = parse_release_name(d.raw_title)
            score_res = score_release(
                parsed,
                d.size_bytes or 0,
                expected_title=season.media_item.title,
                expected_year=season.media_item.year,
                expected_season=season.season_number,
            )
            if not score_res.is_rejected and score_res.score > best_score:
                best_score = score_res.score
                best_item = d
                best_parsed = parsed

        if best_item and best_parsed:
            is_completed = best_score >= target_score
            new_season_status = (
                SeasonStatus.COMPLETED if is_completed else SeasonStatus.DOWNLOADED
            )
            if best_item.download_state in ("downloading", "queued", "processing"):
                new_season_status = SeasonStatus.DOWNLOADING

            season.status = new_season_status
            season.empty_search_count = 0

            # Cascade to episodes
            ep_status = (
                EpisodeStatus.DOWNLOADED
                if new_season_status != SeasonStatus.DOWNLOADING
                else EpisodeStatus.DOWNLOADING
            )
            for ep in season.episodes:
                if ep.status in (EpisodeStatus.PENDING, EpisodeStatus.SEARCHING):
                    ep.status = ep_status

            dh = DownloadHistory(
                media_item_id=season.media_item_id,
                season_id=season.id,
                nzb_title=best_item.raw_title,
                score=best_score,
                size_bytes=best_item.size_bytes,
                resolution=best_parsed.resolution,
                video_codec=best_parsed.video_codec,
                audio_codec=best_parsed.audio_codec,
                source=best_parsed.source,
                torbox_id=best_item.torbox_id,
                torbox_sent_at=datetime.now(timezone.utc),
            )
            session.add(dh)
            changed = True

    # 4. Episodes
    stmt_e = (
        select(Episode)
        .where(Episode.status == EpisodeStatus.SEARCHING)
        .options(selectinload(Episode.season).selectinload(Season.media_item))
    )
    episodes = (await session.execute(stmt_e)).scalars().all()

    for episode in episodes:
        expected = clean_title(episode.season.media_item.title)
        best_item = None
        best_score = -1.0
        best_parsed = None

        for d in active_downloads:
            if d.media_type != "series_episode":
                continue
            if not d.parsed_title or clean_title(d.parsed_title) != expected:
                continue
            if (
                d.season_number != episode.season.season_number
                or d.episode_number != episode.episode_number
            ):
                continue

            parsed = parse_release_name(d.raw_title)
            score_res = score_release(
                parsed,
                d.size_bytes or 0,
                expected_title=episode.season.media_item.title,
                expected_year=episode.season.media_item.year,
                expected_season=episode.season.season_number,
                expected_episode=episode.episode_number,
            )
            if not score_res.is_rejected and score_res.score > best_score:
                best_score = score_res.score
                best_item = d
                best_parsed = parsed

        if best_item and best_parsed:
            is_completed = best_score >= target_score
            new_episode_status = (
                EpisodeStatus.COMPLETED if is_completed else EpisodeStatus.DOWNLOADED
            )
            if best_item.download_state in ("downloading", "queued", "processing"):
                new_episode_status = EpisodeStatus.DOWNLOADING

            episode.status = new_episode_status
            episode.empty_search_count = 0
            dh = DownloadHistory(
                media_item_id=episode.season.media_item_id,
                season_id=episode.season.id,
                episode_id=episode.id,
                nzb_title=best_item.raw_title,
                score=best_score,
                size_bytes=best_item.size_bytes,
                resolution=best_parsed.resolution,
                video_codec=best_parsed.video_codec,
                audio_codec=best_parsed.audio_codec,
                source=best_parsed.source,
                torbox_id=best_item.torbox_id,
                torbox_sent_at=datetime.now(timezone.utc),
            )
            session.add(dh)
            changed = True

    if changed:
        await session.commit()


async def adopt_torbox_downloads_for_print(session) -> None:
    """Auto-adopts SEARCHING print items using the local TorBox download cache."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.core.reading_scorer import (
        detect_print_format,
        match_volume_or_issue,
        score_print_release,
    )
    from app.db.models import (
        BookItem,
        EpisodeStatus,
        MangaItem,
        MediaStatus,
        SeenTorboxDownload,
    )

    stmt_td = select(SeenTorboxDownload).where(
        SeenTorboxDownload.media_type.in_(["book", "manga"]),
        SeenTorboxDownload.download_state.not_in(["failed", "error"]),
    )
    active_downloads = (await session.execute(stmt_td)).scalars().all()

    if not active_downloads:
        return

    import re

    def clean_title(title: str) -> str:
        return re.sub(r"[^a-z0-9]", "", title.lower())

    changed = False

    # 1. Books
    stmt_b = select(BookItem).where(
        BookItem.status.in_([MediaStatus.SEARCHING, MediaStatus.DOWNLOADING])
    )
    books = (await session.execute(stmt_b)).scalars().all()

    for book in books:
        expected = clean_title(book.title)
        best_item = None
        best_score = -1.0

        for d in active_downloads:
            if d.media_type != "book":
                continue
            if not d.parsed_title or clean_title(d.parsed_title) != expected:
                if expected not in clean_title(d.raw_title):
                    continue

            fmt = detect_print_format(d.raw_title, media_type="book")
            sc_res_dict = score_print_release(fmt, media_type="book")
            score = sc_res_dict["score"]
            if score > best_score:
                best_score = score
                best_item = d

        if best_item:
            if best_item.download_state in ("downloading", "queued", "processing"):
                new_status = MediaStatus.DOWNLOADING
            else:
                new_status = (
                    MediaStatus.COMPLETED
                    if best_score >= 1000
                    else MediaStatus.DOWNLOADED
                )

            if book.status != new_status or book.best_score != best_score:
                book.status = new_status
                book.empty_search_count = 0
                book.best_score = best_score
                changed = True
                logger.info(
                    "  🚀 Auto-adopted Book '%s' from TorBox (%s)",
                    book.title,
                    new_status.value,
                )

    # 2. Manga Volumes
    stmt_m = (
        select(MangaItem)
        .where(MangaItem.status.in_([MediaStatus.SEARCHING, MediaStatus.DOWNLOADING]))
        .options(selectinload(MangaItem.volumes))
    )
    mangas = (await session.execute(stmt_m)).scalars().all()

    for manga in mangas:
        expected = clean_title(manga.title)

        for vol in manga.volumes:
            if vol.status not in (
                EpisodeStatus.PENDING,
                EpisodeStatus.SEARCHING,
                EpisodeStatus.DOWNLOADING,
            ):
                continue

            best_item = None
            best_score = -1.0

            for d in active_downloads:
                if d.media_type != "manga":
                    continue
                if not d.parsed_title or clean_title(d.parsed_title) != expected:
                    if expected not in clean_title(d.raw_title):
                        continue

                if not match_volume_or_issue(d.raw_title, vol.volume_number):
                    continue

                fmt = detect_print_format(d.raw_title, media_type="manga")
                sc_res_dict = score_print_release(fmt, media_type="manga")
                score = sc_res_dict["score"]
                if score > best_score:
                    best_score = score
                    best_item = d

            if best_item:
                if best_item.download_state in ("downloading", "queued", "processing"):
                    new_epi_status = EpisodeStatus.DOWNLOADING
                else:
                    new_epi_status = (
                        EpisodeStatus.COMPLETED
                        if best_score >= 1000
                        else EpisodeStatus.DOWNLOADED
                    )

                if vol.status != new_epi_status or vol.best_score != best_score:
                    vol.status = new_epi_status
                    vol.empty_search_count = 0
                    vol.best_score = best_score
                    changed = True
                    logger.info(
                        "  🚀 Auto-adopted Manga '%s' Vol %d from TorBox (%s)",
                        manga.title,
                        vol.volume_number,
                        new_epi_status.value,
                    )

    if changed:
        await session.commit()


async def match_and_adopt_target_from_cache(session, target) -> bool:
    """
    Instantaneous evaluation of a newly added target against the cache.
    Evaluates only the single provided target against the cached downloads.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.config import scoring_config
    from app.core.parser import parse_release_name
    from app.core.reading_scorer import (
        detect_print_format,
        match_volume_or_issue,
        score_print_release,
    )
    from app.core.scorer import score_release
    from app.db.models import (
        BookItem,
        DownloadHistory,
        EpisodeStatus,
        MangaItem,
        MediaItem,
        MediaStatus,
        MediaType,
        Season,
        SeasonStatus,
        SeenTorboxDownload,
    )

    await session.commit()
    target_score = scoring_config.get("cutoffs", {}).get("target_score", 8000)

    import re

    def clean_title(title: str) -> str:
        return re.sub(r"[^a-z0-9]", "", title.lower())

    changed = False

    if isinstance(target, MediaItem) and target.media_type == MediaType.MOVIE:
        if target.status != MediaStatus.SEARCHING:
            return False

        stmt_td = select(SeenTorboxDownload).where(
            SeenTorboxDownload.media_type == "movie",
            SeenTorboxDownload.download_state.not_in(["failed", "error"]),
        )
        active_downloads = (await session.execute(stmt_td)).scalars().all()

        expected = clean_title(target.title)
        best_item = None
        best_score = -1.0
        best_parsed = None

        for d in active_downloads:
            if not d.parsed_title or clean_title(d.parsed_title) != expected:
                continue
            if d.parsed_year and target.year and d.parsed_year != target.year:
                continue

            parsed = parse_release_name(d.raw_title)
            score_res = score_release(
                parsed,
                d.size_bytes or 0,
                expected_title=target.title,
                expected_year=target.year,
            )
            if not score_res.is_rejected and score_res.score > best_score:
                best_score = score_res.score
                best_item = d
                best_parsed = parsed

        if best_item and best_parsed:
            is_completed = best_score >= target_score
            new_status = (
                MediaStatus.COMPLETED if is_completed else MediaStatus.DOWNLOADED
            )
            if best_item.download_state in ("downloading", "queued", "processing"):
                new_status = MediaStatus.DOWNLOADING

            target.status = new_status
            target.empty_search_count = 0
            dh = DownloadHistory(
                media_item_id=target.id,
                nzb_title=best_item.raw_title,
                score=best_score,
                size_bytes=best_item.size_bytes,
                resolution=best_parsed.resolution,
                video_codec=best_parsed.video_codec,
                audio_codec=best_parsed.audio_codec,
                source=best_parsed.source,
                torbox_id=best_item.torbox_id,
                torbox_sent_at=datetime.now(timezone.utc),
            )
            session.add(dh)
            changed = True

    elif isinstance(target, MediaItem) and target.media_type in (
        MediaType.SHOW,
        MediaType.ANIME,
    ):
        # We need seasons and episodes loaded
        if not hasattr(target, "seasons") or not target.seasons:
            stmt = (
                select(MediaItem)
                .where(MediaItem.id == target.id)
                .options(selectinload(MediaItem.seasons).selectinload(Season.episodes))
            )
            target = (await session.execute(stmt)).scalars().first()
            if not target:
                return False

        stmt_td = select(SeenTorboxDownload).where(
            SeenTorboxDownload.media_type.in_(["series_season", "series_episode"]),
            SeenTorboxDownload.download_state.not_in(["failed", "error"]),
        )
        active_downloads = (await session.execute(stmt_td)).scalars().all()
        expected = clean_title(target.title)

        for season in target.seasons:
            if season.status != SeasonStatus.SEARCHING:
                continue

            best_item = None
            best_score = -1.0
            best_parsed = None

            for d in active_downloads:
                if d.media_type not in ("series_season", "series_episode"):
                    continue
                if d.episode_number is not None:
                    continue
                if not d.parsed_title or clean_title(d.parsed_title) != expected:
                    continue
                if d.season_number != season.season_number:
                    continue

                parsed = parse_release_name(d.raw_title)
                score_res = score_release(
                    parsed,
                    d.size_bytes or 0,
                    expected_title=target.title,
                    expected_year=target.year,
                    expected_season=season.season_number,
                )
                if not score_res.is_rejected and score_res.score > best_score:
                    best_score = score_res.score
                    best_item = d
                    best_parsed = parsed

            if best_item and best_parsed:
                is_completed = best_score >= target_score
                new_season_status = (
                    SeasonStatus.COMPLETED if is_completed else SeasonStatus.DOWNLOADED
                )
                if best_item.download_state in ("downloading", "queued", "processing"):
                    new_season_status = SeasonStatus.DOWNLOADING

                season.status = new_season_status
                season.empty_search_count = 0

                ep_status = (
                    EpisodeStatus.DOWNLOADED
                    if new_season_status != SeasonStatus.DOWNLOADING
                    else EpisodeStatus.DOWNLOADING
                )
                for ep in season.episodes:
                    if ep.status in (EpisodeStatus.PENDING, EpisodeStatus.SEARCHING):
                        ep.status = ep_status

                dh = DownloadHistory(
                    media_item_id=target.id,
                    season_id=season.id,
                    nzb_title=best_item.raw_title,
                    score=best_score,
                    size_bytes=best_item.size_bytes,
                    resolution=best_parsed.resolution,
                    video_codec=best_parsed.video_codec,
                    audio_codec=best_parsed.audio_codec,
                    source=best_parsed.source,
                    torbox_id=best_item.torbox_id,
                    torbox_sent_at=datetime.now(timezone.utc),
                )
                session.add(dh)
                changed = True

            # Check individual episodes if season pack not found
            if season.status == SeasonStatus.SEARCHING:
                for ep in season.episodes:
                    if ep.status != EpisodeStatus.SEARCHING:
                        continue

                    best_item_ep = None
                    best_score_ep = -1.0
                    best_parsed_ep = None

                    for d in active_downloads:
                        if d.media_type != "series_episode":
                            continue
                        if (
                            not d.parsed_title
                            or clean_title(d.parsed_title) != expected
                        ):
                            continue
                        if (
                            d.season_number != season.season_number
                            or d.episode_number != ep.episode_number
                        ):
                            continue

                        parsed = parse_release_name(d.raw_title)
                        score_res = score_release(
                            parsed,
                            d.size_bytes or 0,
                            expected_title=target.title,
                            expected_year=target.year,
                            expected_season=season.season_number,
                            expected_episode=ep.episode_number,
                        )
                        if (
                            not score_res.is_rejected
                            and score_res.score > best_score_ep
                        ):
                            best_score_ep = score_res.score
                            best_item_ep = d
                            best_parsed_ep = parsed

                    if best_item_ep and best_parsed_ep:
                        is_completed = best_score_ep >= target_score
                        new_episode_status = (
                            EpisodeStatus.COMPLETED
                            if is_completed
                            else EpisodeStatus.DOWNLOADED
                        )
                        if best_item_ep.download_state in (
                            "downloading",
                            "queued",
                            "processing",
                        ):
                            new_episode_status = EpisodeStatus.DOWNLOADING

                        ep.status = new_episode_status
                        ep.empty_search_count = 0
                        dh = DownloadHistory(
                            media_item_id=target.id,
                            season_id=season.id,
                            episode_id=ep.id,
                            nzb_title=best_item_ep.raw_title,
                            score=best_score_ep,
                            size_bytes=best_item_ep.size_bytes,
                            resolution=best_parsed_ep.resolution,
                            video_codec=best_parsed_ep.video_codec,
                            audio_codec=best_parsed_ep.audio_codec,
                            source=best_parsed_ep.source,
                            torbox_id=best_item_ep.torbox_id,
                            torbox_sent_at=datetime.now(timezone.utc),
                        )
                        session.add(dh)
                        changed = True

    elif isinstance(target, BookItem):
        if target.status != MediaStatus.SEARCHING:
            return False

        stmt_td = select(SeenTorboxDownload).where(
            SeenTorboxDownload.media_type == "book",
            SeenTorboxDownload.download_state.not_in(["failed", "error"]),
        )
        active_downloads = (await session.execute(stmt_td)).scalars().all()

        expected = clean_title(target.title)
        best_item = None
        best_score = -1.0

        for d in active_downloads:
            if not d.parsed_title or clean_title(d.parsed_title) != expected:
                if expected not in clean_title(d.raw_title):
                    continue

            fmt = detect_print_format(d.raw_title, media_type="book")
            sc_res_dict = score_print_release(fmt, media_type="book")
            score = sc_res_dict["score"]
            if score > best_score:
                best_score = score
                best_item = d

        if best_item:
            new_book_status = MediaStatus.COMPLETED
            if best_item.download_state in ("downloading", "queued", "processing"):
                new_book_status = MediaStatus.DOWNLOADING

            target.status = new_book_status
            target.empty_search_count = 0
            target.best_score = best_score
            changed = True

    elif isinstance(target, MangaItem):
        if target.status != MediaStatus.SEARCHING:
            return False

        if not hasattr(target, "volumes") or not target.volumes:
            stmt_manga_local = (
                select(MangaItem)
                .where(MangaItem.id == target.id)
                .options(selectinload(MangaItem.volumes))
            )
            target = (await session.execute(stmt_manga_local)).scalars().first()
            if not target:
                return False

        stmt_td = select(SeenTorboxDownload).where(
            SeenTorboxDownload.media_type == "manga",
            SeenTorboxDownload.download_state.not_in(["failed", "error"]),
        )
        active_downloads = (await session.execute(stmt_td)).scalars().all()

        expected = clean_title(target.title)

        for vol in target.volumes:
            if vol.status not in (EpisodeStatus.PENDING, EpisodeStatus.SEARCHING):
                continue

            best_item = None
            best_score = -1.0

            for d in active_downloads:
                if not d.parsed_title or clean_title(d.parsed_title) != expected:
                    if expected not in clean_title(d.raw_title):
                        continue

                if not match_volume_or_issue(d.raw_title, vol.volume_number):
                    continue

                fmt = detect_print_format(d.raw_title, media_type="manga")
                sc_res_dict = score_print_release(fmt, media_type="manga")
                score = sc_res_dict["score"]
                if score > best_score:
                    best_score = score
                    best_item = d

            if best_item:
                new_epi_status = EpisodeStatus.COMPLETED
                if best_item.download_state in ("downloading", "queued", "processing"):
                    new_epi_status = EpisodeStatus.DOWNLOADING

                vol.status = new_epi_status
                vol.empty_search_count = 0
                vol.best_score = best_score
                changed = True

    if changed:
        await session.commit()
        return True

    return False
