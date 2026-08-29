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
        tb_key = await torbox.resolve_api_key()
        if not tb_key:
            logger.debug("TorBox API key missing. Skipping self-healing cycle.")
            return

        logger.info("🔧 Starting Self-Healing cycle...")

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

                if status in (
                    "failed",
                    "error",
                    "not_found",
                    "aborted",
                    "cannot be re-completed",
                    "not enough repair blocks",
                ):
                    from app.core.failure_logger import log_failure

                    log_failure(
                        session,
                        target,
                        "torbox_error",
                        f"TorBox download failed: {status}",
                    )
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

            # --- Auto-Adoption from TorBox ---
            try:
                raw_downloads = await torbox.get_usenet_downloads(session=session)
                if raw_downloads:
                    await sync_torbox_cache(session, raw_downloads)
                    await adopt_torbox_downloads_for_video(session)
                    await adopt_torbox_downloads_for_print(session)
            except Exception as e:
                logger.error("Error during TorBox cache sync and adoption: %s", e)

        logger.info("✅ Self-Healing cycle complete.")
    finally:
        log_process_end(logger, "Self-Healing Engine")


async def run_download_check_cycle() -> None:
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

        logger.info("🔎 Checking download status on TorBox...")

        try:
            import httpx

            headers = {"Authorization": f"Bearer {tb_key}"}
            tb_items: dict[str, dict] = {}

            async with httpx.AsyncClient(timeout=15.0) as client:
                # Fetch Usenet
                resp = await client.get(
                    "https://api.torbox.app/v1/api/usenet/mylist", headers=headers
                )
                if resp.status_code == 200:
                    tb_data = resp.json()
                    if tb_data.get("success"):
                        for item in tb_data.get("data", []) or []:
                            tb_items[str(item.get("id", ""))] = item

                # Fetch Torrents
                resp_t = await client.get(
                    "https://api.torbox.app/v1/api/torrents/mylist", headers=headers
                )
                if resp_t.status_code == 200:
                    tb_data_t = resp_t.json()
                    if tb_data_t.get("success"):
                        for item in tb_data_t.get("data", []) or []:
                            tb_items[str(item.get("id", ""))] = item

        except Exception as e:
            logger.error("❌ Konnte TorBox-Liste nicht abrufen: %s", e)
            return

        async with async_session_factory() as session:
            from app.config import scoring_config

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
                    return "no_id" if not history.torbox_id else "not_found", {}

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
                elif status in (
                    "failed",
                    "error",
                    "aborted",
                    "cannot be re-completed",
                    "not enough repair blocks",
                ):
                    logger.warning(
                        "    ⚠️ %s → failed (%s), returning to search queue.",
                        title,
                        status,
                    )
                    from app.core.failure_logger import log_failure

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
    stmt_b = select(BookItem).where(BookItem.status == MediaStatus.SEARCHING)
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
            new_status = MediaStatus.COMPLETED
            if best_item.download_state in ("downloading", "queued", "processing"):
                new_status = MediaStatus.DOWNLOADING

            book.status = new_status
            book.empty_search_count = 0
            book.best_score = best_score
            changed = True
            logger.info("  🚀 Auto-adopted Book '%s' from TorBox", book.title)

    # 2. Manga Volumes
    stmt_m = (
        select(MangaItem)
        .where(MangaItem.status == MediaStatus.SEARCHING)
        .options(selectinload(MangaItem.volumes))
    )
    mangas = (await session.execute(stmt_m)).scalars().all()

    for manga in mangas:
        expected = clean_title(manga.title)

        for vol in manga.volumes:
            if vol.status not in (EpisodeStatus.PENDING, EpisodeStatus.SEARCHING):
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
                new_epi_status = EpisodeStatus.COMPLETED
                if best_item.download_state in ("downloading", "queued", "processing"):
                    new_epi_status = EpisodeStatus.DOWNLOADING

                vol.status = new_epi_status
                vol.empty_search_count = 0
                vol.best_score = best_score
                changed = True
                logger.info(
                    "  🚀 Auto-adopted Manga '%s' Vol %d from TorBox",
                    manga.title,
                    vol.volume_number,
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
            target = (await session.execute(stmt)).scalar_one_or_none()
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
            target = (await session.execute(stmt_manga_local)).scalar_one_or_none()
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
