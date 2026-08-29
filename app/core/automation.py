"""
Automation Orchestrator
=======================
Handles syncing the Simkl watchlist to the local database, searching
Newznab indexers, evaluating scores, and sending releases to TorBox.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import scoring_config, settings
from app.core.automation_state import AutomationStatus, automation_state_manager
from app.core.logging_config import log_process_end, log_process_start
from app.core.parser import parse_release_name
from app.core.scorer import score_release
from app.core.self_healing import run_download_check_cycle, run_self_healing_cycle
from app.db.database import async_session_factory
from app.db.grab_tracker import can_grab_today, increment_today_grab_count
from app.db.models import (
    BookItem,
    DownloadHistory,
    MangaItem,
    MediaItem,
    MediaStatus,
    MediaType,
    Season,
    SeasonStatus,
)
from app.services import provider_service, simkl, tmdb, torbox, treasure_maps
from app.services.torbox import DownloaderNetworkError

logger = logging.getLogger(__name__)


async def sync_all_providers() -> None:
    """Sync watchlists and libraries from all configured providers (Simkl, etc.)."""
    log_process_start(logger, "Provider Sync Engine")
    from app.db.models import Provider

    async with async_session_factory() as session:
        try:
            stmt = select(Provider).options(selectinload(Provider.profiles))
            providers_res = await session.execute(stmt)
            providers = providers_res.scalars().unique().all()

            for provider in providers:
                if provider.type == "simkl":
                    if not provider.client_id or not provider.access_token:
                        logger.warning(
                            "Provider %s lacks Simkl credentials, skipping.",
                            provider.name,
                        )
                        continue

                    movies = await simkl.get_watchlist(
                        "movies", provider.client_id, provider.access_token
                    )
                    shows = await simkl.get_watchlist(
                        "shows", provider.client_id, provider.access_token
                    )
                    anime = await simkl.get_watchlist(
                        "anime", provider.client_id, provider.access_token
                    )

                    await _sync_items(session, movies, MediaType.MOVIE, provider.id)
                    await _sync_items(session, shows, MediaType.SHOW, provider.id)
                    await _sync_items(session, anime, MediaType.ANIME, provider.id)

            await session.commit()
            from app.core.self_healing import (
                adopt_torbox_downloads_for_print,
                adopt_torbox_downloads_for_video,
            )

            await adopt_torbox_downloads_for_video(session)
            await adopt_torbox_downloads_for_print(session)
            logger.info("All provider watchlists synced successfully.")
        except Exception as e:  # noqa: BLE001
            logger.error("Provider sync failed: %s", e)
            await session.rollback()
        finally:
            log_process_end(logger, "Provider Sync Engine")


async def sync_simkl_watchlist() -> None:
    """Sync the Simkl 'plan to watch' list to the local SQLite database."""
    await sync_all_providers()


async def _sync_items(
    session: AsyncSession,
    simkl_items: list[dict[str, Any]],
    media_type: MediaType,
    provider_id: int,
) -> None:
    for item_data in simkl_items:
        movie_data = (
            item_data.get("movie") or item_data.get("show") or item_data.get("anime")
        )
        if not movie_data:
            continue

        simkl_id = movie_data.get("ids", {}).get("simkl")
        if not simkl_id:
            continue

        # Check if item exists
        stmt = (
            select(MediaItem)
            .where(MediaItem.simkl_id == simkl_id)
            .options(selectinload(MediaItem.seasons))
        )
        result = await session.execute(stmt)
        item = result.scalar_one_or_none()

        ids = movie_data.get("ids", {})
        tmdb_id_str = ids.get("tmdb")
        tmdb_id = int(tmdb_id_str) if tmdb_id_str else None

        tvdb_id_str = ids.get("tvdb")
        tvdb_id = int(tvdb_id_str) if tvdb_id_str else None

        mal_id_str = ids.get("mal")
        mal_id = int(mal_id_str) if mal_id_str else None

        anilist_id_str = ids.get("anilist")
        anilist_id = int(anilist_id_str) if anilist_id_str else None

        if not item:
            item = MediaItem(
                provider_id=provider_id,
                simkl_id=simkl_id,
                imdb_id=ids.get("imdb"),
                tmdb_id=tmdb_id,
                tvdb_id=tvdb_id,
                mal_id=mal_id,
                anilist_id=anilist_id,
                title=movie_data.get("title"),
                year=movie_data.get("year"),
                media_type=media_type,
                poster_url=f"https://simkl.in/posters/{movie_data.get('poster')}_m.webp"
                if movie_data.get("poster")
                else None,
                status=MediaStatus.PENDING,
            )
            session.add(item)
            # Need to flush to get item.id for seasons
            await session.flush()
            is_new = True
        else:
            is_new = False

        # Fetch metadata from TMDB if available (for new items, or items missing release date)
        if (tmdb_id or item.imdb_id) and (is_new or not item.release_date):
            details = None
            if tmdb_id:
                if media_type in (MediaType.SHOW, MediaType.ANIME):
                    details = await tmdb.get_show_details(tmdb_id)
                    if not details and media_type == MediaType.ANIME:
                        # Might be an anime movie
                        details_movie = await tmdb.get_movie_details(tmdb_id)
                        if details_movie:
                            logger.info(
                                "    🔄 '%s' is identified as a movie by TMDB, updating media type to MOVIE.",
                                item.title,
                            )
                            item.media_type = MediaType.MOVIE
                            media_type = MediaType.MOVIE
                            details = details_movie

                if media_type == MediaType.MOVIE and not details:
                    details = await tmdb.get_movie_details(tmdb_id)

            # IMDB Fallback if TMDB ID failed or was missing
            if not details and item.imdb_id:
                logger.info(
                    "    🔍 TMDB lookup failed/missing. Using IMDB fallback for '%s' (%s)...",
                    item.title,
                    item.imdb_id,
                )
                fallback_res = await tmdb.find_by_external_id(item.imdb_id)
                if fallback_res:
                    tmdb_id = fallback_res["id"]
                    item.tmdb_id = tmdb_id

                    if fallback_res["type"] == "movie":
                        if media_type != MediaType.MOVIE:
                            logger.info(
                                "    🔄 Updating media type for '%s' to MOVIE via IMDB fallback.",
                                item.title,
                            )
                            item.media_type = MediaType.MOVIE
                            media_type = MediaType.MOVIE
                        details = await tmdb.get_movie_details(tmdb_id)
                    else:
                        if media_type == MediaType.MOVIE:
                            logger.info(
                                "    🔄 Updating media type for '%s' to SHOW via IMDB fallback.",
                                item.title,
                            )
                            item.media_type = MediaType.SHOW
                            media_type = MediaType.SHOW
                        details = await tmdb.get_show_details(tmdb_id)

            if media_type == MediaType.MOVIE:
                if details:
                    # Update title to TMDB's english/default title if it differs from the romanji one
                    if details.get("title") and details.get("title") != item.title:
                        if not item.alt_title:
                            item.alt_title = item.title  # Backup romaji title
                        item.title = str(details.get("title"))

                    # Extract German alt_title
                    translations = details.get("translations", {}).get(
                        "translations", []
                    )
                    de_trans = next(
                        (t for t in translations if t.get("iso_3166_1") == "DE"), None
                    )
                    if de_trans and de_trans.get("data", {}).get("title"):
                        item.alt_title = de_trans["data"]["title"]
                    elif (
                        not item.alt_title
                        and details.get("original_title")
                        and details.get("original_title") != item.title
                    ):
                        item.alt_title = details.get("original_title")

                release_date = (
                    await tmdb.get_digital_release_date(tmdb_id) if tmdb_id else None
                )
                if not release_date and details and details.get("release_date"):
                    try:
                        release_date = datetime.strptime(
                            details["release_date"], "%Y-%m-%d"
                        ).replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass

                item.release_date = release_date

                # Determine status
                if release_date and release_date > datetime.now(timezone.utc):
                    item.status = MediaStatus.FUTURE
                elif item.year and item.year > datetime.now().year:
                    # Fallback if no release date but year is in the future
                    item.status = MediaStatus.FUTURE
                else:
                    item.status = MediaStatus.SEARCHING

            elif media_type in (MediaType.SHOW, MediaType.ANIME):
                if details:
                    item.overview = details.get("overview")

                    if details.get("name") and details.get("name") != item.title:
                        if not item.alt_title:
                            item.alt_title = item.title
                        item.title = str(details.get("name"))

                    translations = details.get("translations", {}).get(
                        "translations", []
                    )
                    de_trans = next(
                        (t for t in translations if t.get("iso_3166_1") == "DE"), None
                    )
                    if de_trans and de_trans.get("data", {}).get("name"):
                        item.alt_title = de_trans["data"]["name"]
                    elif (
                        not item.alt_title
                        and details.get("original_name")
                        and details.get("original_name") != item.title
                    ):
                        item.alt_title = details.get("original_name")

                    # Extract first air date / release date
                    show_release_date = None
                    if details.get("first_air_date"):
                        try:
                            show_release_date = datetime.strptime(
                                details["first_air_date"], "%Y-%m-%d"
                            ).replace(tzinfo=timezone.utc)
                        except ValueError:
                            pass
                    elif details.get("release_date"):
                        try:
                            show_release_date = datetime.strptime(
                                details["release_date"], "%Y-%m-%d"
                            ).replace(tzinfo=timezone.utc)
                        except ValueError:
                            pass

                    if show_release_date:
                        item.release_date = show_release_date

                    # Determine status
                    if item.release_date and item.release_date > datetime.now(
                        timezone.utc
                    ):
                        item.status = MediaStatus.FUTURE
                    elif item.year and item.year > datetime.now().year:
                        item.status = MediaStatus.FUTURE
                    else:
                        item.status = MediaStatus.SEARCHING

                    for s in details.get("seasons", []):
                        s_num = s.get("season_number")
                        if s_num is not None and s_num > 0:
                            s_air_date = None
                            if s.get("air_date"):
                                try:
                                    s_air_date = datetime.strptime(
                                        s["air_date"], "%Y-%m-%d"
                                    ).replace(tzinfo=timezone.utc)
                                except ValueError:
                                    pass

                            is_season_future = (
                                s_air_date and s_air_date > datetime.now(timezone.utc)
                            ) or (item.status == MediaStatus.FUTURE)

                            # Check if season already exists to avoid UNIQUE constraint error
                            existing_season_stmt = select(Season).where(
                                Season.media_item_id == item.id,
                                Season.season_number == s_num,
                            )
                            existing_season = (
                                await session.execute(existing_season_stmt)
                            ).scalar_one_or_none()
                            if not existing_season:
                                season_obj = Season(
                                    media_item_id=item.id,
                                    season_number=s_num,
                                    monitored=(s_num == 1),
                                    episode_count=s.get("episode_count"),
                                    air_date=s_air_date,
                                    status=SeasonStatus.FUTURE
                                    if is_season_future
                                    else (
                                        SeasonStatus.SEARCHING
                                        if (s_num == 1)
                                        else SeasonStatus.PENDING
                                    ),
                                )
                                session.add(season_obj)
                                await session.flush()
                                await _sync_season_episodes(
                                    session, season_obj, tmdb_id=item.tmdb_id
                                )
                            else:
                                if s_air_date:
                                    existing_season.air_date = s_air_date
                                await _sync_season_episodes(
                                    session, existing_season, tmdb_id=item.tmdb_id
                                )
                                if existing_season.status in (
                                    SeasonStatus.PENDING,
                                    SeasonStatus.SEARCHING,
                                    SeasonStatus.FUTURE,
                                ):
                                    if is_season_future:
                                        existing_season.status = SeasonStatus.FUTURE
                                    elif existing_season.status == SeasonStatus.FUTURE:
                                        existing_season.status = (
                                            SeasonStatus.SEARCHING
                                            if existing_season.monitored
                                            else SeasonStatus.PENDING
                                        )

        else:
            # Update existing
            item.title = movie_data.get("title", item.title)
            if not item.tvdb_id:
                item.tvdb_id = tvdb_id
            if not item.mal_id:
                item.mal_id = mal_id
            if not item.anilist_id:
                item.anilist_id = anilist_id
            # Ensure provider matches
            item.provider_id = provider_id

            # Sync status based on release date / year for existing items
            if item.status in (
                MediaStatus.PENDING,
                MediaStatus.SEARCHING,
                MediaStatus.FUTURE,
            ):
                rd = item.release_date
                if rd and rd.tzinfo is None:
                    rd = rd.replace(tzinfo=timezone.utc)
                if rd and rd > datetime.now(timezone.utc):
                    item.status = MediaStatus.FUTURE
                elif not rd and item.year and item.year > datetime.now().year:
                    item.status = MediaStatus.FUTURE
                else:
                    item.status = MediaStatus.SEARCHING

        item.simkl_synced_at = datetime.now(timezone.utc)


async def _sync_season_episodes(
    session: AsyncSession, season: Season, tmdb_id: int | None = None
) -> None:
    from app.db.models import Episode, EpisodeStatus

    effective_tmdb_id = tmdb_id or (
        season.media_item.tmdb_id if season.media_item else None
    )
    if not effective_tmdb_id:
        return

    from sqlalchemy import inspect

    # Avoid N+1 query if episodes are already eager-loaded
    if "episodes" in inspect(season).unloaded:
        await session.refresh(season, ["episodes"])

    # Optimization: if we already have all episodes and none are FUTURE, skip TMDB API call
    expected_eps = season.episode_count or 0
    if expected_eps > 0 and len(season.episodes) >= expected_eps:
        # Check if any episode is in FUTURE state
        has_future_eps = any(
            ep.status == EpisodeStatus.FUTURE for ep in season.episodes
        )
        if not has_future_eps:
            return

    season_details = await tmdb.get_season_details(
        effective_tmdb_id, season.season_number
    )
    if not season_details:
        return

    episodes_data = season_details.get("episodes", [])

    # Check existing episodes
    existing_eps = {ep.episode_number: ep for ep in season.episodes}

    for ep_data in episodes_data:
        ep_num = ep_data.get("episode_number")
        if not ep_num:
            continue

        air_date_str = ep_data.get("air_date")
        air_date = None
        if air_date_str:
            try:
                air_date = datetime.strptime(air_date_str, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                pass

        ep_is_future = air_date is not None and air_date > datetime.now(timezone.utc)

        if ep_num in existing_eps:
            existing_eps[ep_num].air_date = air_date
            if existing_eps[ep_num].status in (
                EpisodeStatus.PENDING,
                EpisodeStatus.SEARCHING,
                EpisodeStatus.FUTURE,
            ):
                if ep_is_future:
                    existing_eps[ep_num].status = EpisodeStatus.FUTURE
                elif existing_eps[ep_num].status == EpisodeStatus.FUTURE:
                    existing_eps[ep_num].status = (
                        EpisodeStatus.SEARCHING
                        if season.monitored
                        else EpisodeStatus.PENDING
                    )
        else:
            new_ep = Episode(
                season_id=season.id,
                episode_number=ep_num,
                air_date=air_date,
                status=EpisodeStatus.FUTURE if ep_is_future else EpisodeStatus.PENDING,
            )
            session.add(new_ep)


async def run_automation_cycle(force: bool = False) -> None:
    """Background job that searches for NZBs and pushes them to TorBox.

    Args:
        force: If True, bypass the per-provider cycle skip throttle and always run.
    """
    automation_state_manager.set_running(AutomationStatus.RUNNING_VIDEO)
    log_process_start(logger, "Automation Cycle")
    try:
        if force:
            logger.info(
                "🔍 Manual search run started (bypassing interval multiplier)..."
            )
        else:
            logger.info("🔄 Starting automation cycle...")

        # 0. Self-healing: fix failed downloads before searching
        if force:
            await run_self_healing_cycle()
            await run_download_check_cycle()

        if automation_state_manager.is_aborting():
            logger.info("🛑 Automation cycle abort requested early. Halting.")
            return

        # 1. Sync watchlist first
        await sync_simkl_watchlist()

        if automation_state_manager.is_aborting():
            logger.info("🛑 Automation cycle abort requested after sync. Halting.")
            return

        async with async_session_factory() as session:
            # 1.5 Sync episodes for all seasons (optimized to skip TMDB if already loaded)
            stmt_s = select(Season).options(selectinload(Season.media_item))
            seasons_result = await session.execute(stmt_s)
            for season in seasons_result.scalars().unique():
                if automation_state_manager.is_aborting():
                    logger.info(
                        "🛑 Automation cycle abort requested. Halting season sync."
                    )
                    return
                if season.media_item and season.media_item.tmdb_id:
                    await _sync_season_episodes(session, season)

            # Update pending/future items that have reached their release date
            stmt_reached = select(MediaItem).where(
                MediaItem.status.in_([MediaStatus.PENDING, MediaStatus.FUTURE]),
                MediaItem.release_date.isnot(None),
                MediaItem.release_date <= datetime.now(timezone.utc),
            )
            reached_result = await session.execute(stmt_reached)
            for m in reached_result.scalars():
                m.status = MediaStatus.SEARCHING
                logger.info(
                    "🔄 Item '%s' has reached its release date and is now being searched.",
                    m.title,
                )

            # Revert searching/pending items that have a future release date or future year
            stmt_future = select(MediaItem).where(
                MediaItem.status.in_([MediaStatus.SEARCHING, MediaStatus.PENDING]),
            )
            items_to_check = (await session.execute(stmt_future)).scalars().all()
            for m in items_to_check:
                # Ensure release_date is timezone-aware for comparison
                rd = m.release_date
                if rd and rd.tzinfo is None:
                    rd = rd.replace(tzinfo=timezone.utc)
                if rd and rd > datetime.now(timezone.utc):
                    m.status = MediaStatus.FUTURE
                    m.fail_count = 0
                    logger.info(
                        "    🔄 Item '%s' set to FUTURE (Release Date: %s is in the future)",
                        m.title,
                        rd.strftime("%Y-%m-%d"),
                    )
                elif not m.release_date and m.year and m.year > datetime.now().year:
                    m.status = MediaStatus.FUTURE
                    m.fail_count = 0
                    logger.info(
                        "    🔄 Item '%s' set to FUTURE (Year %s is in the future)",
                        m.title,
                        m.year,
                    )

            # Sync season status with parent item if parent is FUTURE
            stmt_s_future = (
                select(Season)
                .join(MediaItem)
                .where(
                    Season.status.in_([SeasonStatus.SEARCHING, SeasonStatus.PENDING]),
                    MediaItem.status == MediaStatus.FUTURE,
                )
            )
            for s in (await session.execute(stmt_s_future)).scalars().all():
                s.status = SeasonStatus.FUTURE

            await session.commit()

            # Update pending/future episodes that have reached their air date
            from app.db.models import Episode, EpisodeStatus

            stmt_e = select(Episode).where(
                Episode.status.in_([EpisodeStatus.PENDING, EpisodeStatus.FUTURE]),
                Episode.air_date.isnot(None),
                Episode.air_date <= datetime.now(timezone.utc),
            )
            result_e = await session.execute(stmt_e)
            for pending_ep in result_e.scalars():
                pending_ep.status = EpisodeStatus.SEARCHING

            # Set episodes with future air date to FUTURE
            stmt_e_future = select(Episode).where(
                Episode.status.in_([EpisodeStatus.PENDING, EpisodeStatus.SEARCHING]),
                Episode.air_date.isnot(None),
                Episode.air_date > datetime.now(timezone.utc),
            )
            result_e_future = await session.execute(stmt_e_future)
            for future_ep in result_e_future.scalars():
                future_ep.status = EpisodeStatus.FUTURE

            await session.commit()

            # Active Provider Profiles
            from app.db.models import ProviderProfile

            profiles = (await session.execute(select(ProviderProfile))).scalars().all()
            active_movie_provider_ids = [
                p.provider_id for p in profiles if p.media_type == "movies"
            ]
            active_shows_provider_ids = [
                p.provider_id for p in profiles if p.media_type == "shows"
            ]

            if active_movie_provider_ids:
                # 2. Process Movies
                target_score = scoring_config.get("cutoffs", {}).get(
                    "target_score", 8000
                )
                stmt_movies = (
                    select(MediaItem)
                    .where(
                        MediaItem.media_type == MediaType.MOVIE,
                        MediaItem.provider_id.in_(active_movie_provider_ids),
                        or_(
                            MediaItem.status == MediaStatus.SEARCHING,
                            and_(
                                MediaItem.status == MediaStatus.DOWNLOADED,
                                MediaItem.upgrade_attempts_count
                                < settings.max_upgrade_attempts,
                            ),
                        ),
                    )
                    .options(selectinload(MediaItem.download_history))
                )

                movies_result = await session.execute(stmt_movies)
                for movie in movies_result.scalars():
                    if (
                        movie.status == MediaStatus.DOWNLOADED
                        and movie.best_score is not None
                        and movie.best_score >= target_score
                    ):
                        continue
                    if automation_state_manager.is_aborting():
                        logger.info(
                            "🛑 Automation cycle abort requested. Stopping movie processing."
                        )
                        break
                    await _process_movie(session, movie)
            else:
                logger.info("⏩ Skipping movie search in this cycle.")

            if active_shows_provider_ids and not automation_state_manager.is_aborting():
                # 3. Process Shows (Seasons)
                season_stmt = (
                    select(Season)
                    .join(MediaItem)
                    .where(
                        Season.monitored == True,
                        MediaItem.status != MediaStatus.IGNORED,
                        MediaItem.provider_id.in_(active_shows_provider_ids),
                        or_(
                            Season.status.in_(
                                [SeasonStatus.SEARCHING, SeasonStatus.PENDING]
                            ),
                            and_(
                                Season.status == SeasonStatus.DOWNLOADED,
                                Season.upgrade_attempts_count
                                < settings.max_upgrade_attempts,
                            ),
                        ),
                    )
                    .options(
                        selectinload(Season.media_item).selectinload(
                            MediaItem.provider
                        ),
                        selectinload(Season.download_history),
                    )
                )

                target_score = scoring_config.get("cutoffs", {}).get(
                    "target_score", 8000
                )
                seasons_result = await session.execute(season_stmt)
                for season in seasons_result.scalars().unique():
                    if (
                        season.status == SeasonStatus.DOWNLOADED
                        and season.best_score is not None
                        and season.best_score >= target_score
                    ):
                        continue
                    if automation_state_manager.is_aborting():
                        logger.info(
                            "🛑 Automation cycle abort requested. Stopping season processing."
                        )
                        break
                    await _process_season(session, season)
            else:
                logger.info("⏩ Skipping series search in this cycle.")

        # 4. Download-Check: update status for items already sent to TorBox
        if force and not automation_state_manager.is_aborting():
            await run_download_check_cycle()
    finally:
        automation_state_manager.reset()
        log_process_end(logger, "Automation Cycle")


async def _get_anime_aliases(item) -> list[str]:
    aliases = {item.title}
    if getattr(item, "alt_title", None):
        aliases.add(item.alt_title)

    if getattr(item, "anilist_id", None):
        from app.services import anilist

        anilist_aliases = await anilist.get_anime_aliases(item.anilist_id)
        for alias in anilist_aliases:
            if alias:
                aliases.add(alias)

    return list(aliases)


async def _process_movie(session: AsyncSession, movie: MediaItem) -> None:
    from app.db.models import MediaType, ProviderProfile

    logger.info("🎬 Processing movie: %s", movie.title)
    if not movie.imdb_id and not movie.tmdb_id:
        logger.warning("⚠️ Film %s has no IDs, attempting title search.", movie.title)

    # Load provider to get category ID
    await session.refresh(movie, ["provider"])
    cat_id = movie.provider.movie_category_id if movie.provider else None
    if (
        movie.media_type == MediaType.ANIME
        and movie.provider
        and movie.provider.anime_category_id
    ):
        cat_id = movie.provider.anime_category_id

    # Load profile to get reject words
    profile_stmt = select(ProviderProfile).where(
        ProviderProfile.provider_id == movie.provider_id,
        ProviderProfile.media_type == "movies",
    )
    profile = (await session.execute(profile_stmt)).scalar_one_or_none()
    reject_words = (
        [w.strip().lower() for w in profile.reject_words_csv.split(",") if w.strip()]
        if profile and profile.reject_words_csv
        else []
    )
    required_language = (
        profile.languages_csv.strip()
        if profile and profile.languages_csv and profile.languages_csv.strip()
        else None
    )

    filters = {}
    if profile:
        filters = {
            "resolution": profile.resolution or "any",
            "source": profile.source or "any",
            "video_codec": profile.video_codec or "any",
            "hdr": profile.hdr or "any",
            "audio_tier": profile.audio_tier or "any",
            "audio_channels": profile.audio_channels or "any",
        }

    active_indexers = await provider_service.get_active_indexers(session)
    if not active_indexers:
        logger.warning("    ⚠️ No active indexers configured.")
        await _evaluate_and_download(
            session,
            [],
            movie=movie,
            reject_words=reject_words,
            required_language=required_language,
            is_title_fallback=False,
            filters=filters,
        )
        return

    all_results: list[dict[str, Any]] = []
    is_title_fallback = False

    anime_aliases = []
    if movie.media_type == MediaType.MOVIE and getattr(movie, "is_anime_movie", False):
        anime_aliases = await _get_anime_aliases(movie)

    for indexer in active_indexers:
        logger.info(
            "    🔍 Searching indexer '%s' (priority %d) for movie '%s'...",
            indexer.name,
            indexer.priority,
            movie.title,
        )
        results = []

        if not anime_aliases:
            if movie.imdb_id:
                results = await treasure_maps.search_movie(
                    imdb_id=movie.imdb_id,
                    category=cat_id,
                    api_url=indexer.api_url,
                    api_key=indexer.api_key,
                    session=session,
                )
            if not results and movie.tmdb_id:
                results = await treasure_maps.search_movie(
                    tmdb_id=movie.tmdb_id,
                    category=cat_id,
                    api_url=indexer.api_url,
                    api_key=indexer.api_key,
                    session=session,
                )
            if not results:
                logger.warning(
                    "    ⚠️ No ID match for movie '%s' on '%s', falling back to title search.",
                    movie.title,
                    indexer.name,
                )
                results = await treasure_maps.search_movie(
                    title=movie.title,
                    category=cat_id,
                    api_url=indexer.api_url,
                    api_key=indexer.api_key,
                    session=session,
                )
                if results:
                    is_title_fallback = True
        else:
            # Anime Aggregation Flow
            seen_guids = set()
            agg_results = []
            if movie.imdb_id:
                r = await treasure_maps.search_movie(
                    imdb_id=movie.imdb_id,
                    category=cat_id,
                    api_url=indexer.api_url,
                    api_key=indexer.api_key,
                    session=session,
                )
                for x in r:
                    g = x.get("guid") or x.get("link", "")
                    if g and g not in seen_guids:
                        seen_guids.add(g)
                        agg_results.append(x)
            if movie.tmdb_id:
                r = await treasure_maps.search_movie(
                    tmdb_id=movie.tmdb_id,
                    category=cat_id,
                    api_url=indexer.api_url,
                    api_key=indexer.api_key,
                    session=session,
                )
                for x in r:
                    g = x.get("guid") or x.get("link", "")
                    if g and g not in seen_guids:
                        seen_guids.add(g)
                        agg_results.append(x)
            for alias in anime_aliases:
                r = await treasure_maps.search_movie(
                    title=alias,
                    category=cat_id,
                    api_url=indexer.api_url,
                    api_key=indexer.api_key,
                    session=session,
                )
                for x in r:
                    g = x.get("guid") or x.get("link", "")
                    if g and g not in seen_guids:
                        seen_guids.add(g)
                        agg_results.append(x)
            results = agg_results
            if results:
                is_title_fallback = True

        if not results:
            continue

        grabbed = await _evaluate_and_download(
            session,
            results,
            movie=movie,
            reject_words=reject_words,
            required_language=required_language,
            is_title_fallback=is_title_fallback,
            filters=filters,
            indexer_name=indexer.name,
            indexer_url=indexer.api_url,
            indexer_key=indexer.api_key,
            early_exit_on_cutoff=True,
        )
        if grabbed:
            logger.info(
                "    🎯 Target score cutoff met or release grabbed on indexer '%s'. Halting indexer search cascade.",
                indexer.name,
            )
            return

        all_results.extend(results)

    if not all_results:
        await _evaluate_and_download(
            session,
            [],
            movie=movie,
            reject_words=reject_words,
            required_language=required_language,
            is_title_fallback=is_title_fallback,
            filters=filters,
        )
    else:
        await _evaluate_and_download(
            session,
            all_results,
            movie=movie,
            reject_words=reject_words,
            required_language=required_language,
            is_title_fallback=is_title_fallback,
            filters=filters,
        )


async def _process_season(session: AsyncSession, season: Season) -> None:
    from app.db.models import (
        Episode,
        EpisodeStatus,
        MediaType,
        ProviderProfile,
        SeasonStatus,
    )

    logger.info(
        "📺 Loading episode data for series: %s S%02d",
        season.media_item.title,
        season.season_number,
    )
    tvdb_id = season.media_item.tvdb_id
    tmdb_id = season.media_item.tmdb_id
    cat_id = (
        season.media_item.provider.series_category_id
        if season.media_item.provider
        else None
    )

    if (
        season.media_item.media_type == MediaType.ANIME
        and season.media_item.provider
        and season.media_item.provider.anime_category_id
    ):
        cat_id = season.media_item.provider.anime_category_id

    profile_stmt = select(ProviderProfile).where(
        ProviderProfile.provider_id == season.media_item.provider_id,
        ProviderProfile.media_type == "shows",
    )
    profile = (await session.execute(profile_stmt)).scalar_one_or_none()

    reject_words = (
        [w.strip().lower() for w in profile.reject_words_csv.split(",") if w.strip()]
        if profile and profile.reject_words_csv
        else []
    )
    required_language = (
        profile.languages_csv.strip()
        if profile and profile.languages_csv and profile.languages_csv.strip()
        else None
    )
    prefer_seasons = profile.prefer_complete_seasons if profile else False

    filters = {}
    if profile:
        filters = {
            "resolution": profile.resolution or "any",
            "source": profile.source or "any",
            "video_codec": profile.video_codec or "any",
            "hdr": profile.hdr or "any",
            "audio_tier": profile.audio_tier or "any",
            "audio_channels": profile.audio_channels or "any",
        }

    target_score = scoring_config.get("cutoffs", {}).get("target_score", 8000)
    stmt = (
        select(Episode)
        .where(
            Episode.season_id == season.id,
            Episode.monitored == True,
            or_(
                Episode.status == EpisodeStatus.SEARCHING,
                and_(
                    Episode.status == EpisodeStatus.DOWNLOADED,
                    Episode.upgrade_attempts_count < settings.max_upgrade_attempts,
                ),
            ),
        )
        .order_by(Episode.episode_number)
        .options(
            selectinload(Episode.season).selectinload(Season.media_item),
            selectinload(Episode.download_history),
        )
    )
    episodes_res = await session.execute(stmt)
    missing_episodes = [
        ep
        for ep in episodes_res.scalars().all()
        if not (
            ep.status == EpisodeStatus.DOWNLOADED
            and ep.best_score is not None
            and ep.best_score >= target_score
        )
    ]

    if not missing_episodes:
        logger.info("    ✅ No pending episodes for S%02d.", season.season_number)
        return

    logger.info("    📺 Suche %d ausstehende Episoden.", len(missing_episodes))

    active_indexers = await provider_service.get_active_indexers(session)
    if not active_indexers:
        logger.warning("    ⚠️ No active indexers configured.")
        return

    anime_aliases = []
    if season.media_item.media_type == MediaType.ANIME:
        anime_aliases = await _get_anime_aliases(season.media_item)

    async def _search_show_id_first(
        s: int,
        ep: str | None = None,
        api_url: str | None = None,
        api_key: str | None = None,
    ) -> tuple[list, bool]:
        """Search by TVDB, then TMDB, then title fallback. For Anime, queries all aliases and aggregates."""
        seen_guids = set()
        agg_results = []

        if not anime_aliases:
            if tvdb_id:
                r = await treasure_maps.search_show(
                    tvdb_id=tvdb_id,
                    season=s,
                    ep=ep,
                    category=cat_id,
                    api_url=api_url,
                    api_key=api_key,
                    session=session,
                )
                if r:
                    return r, False
            if tmdb_id:
                r = await treasure_maps.search_show(
                    tmdb_id=tmdb_id,
                    season=s,
                    ep=ep,
                    category=cat_id,
                    api_url=api_url,
                    api_key=api_key,
                    session=session,
                )
                if r:
                    return r, False
            logger.warning(
                "    ⚠️ No ID match for S%02d%s, falling back to title search.",
                s,
                f"E{ep}" if ep else "",
            )
            r = await treasure_maps.search_show(
                title=season.media_item.title,
                season=s,
                ep=ep,
                category=cat_id,
                api_url=api_url,
                api_key=api_key,
                session=session,
            )
            return r, True

        # Anime Aggregation Flow
        if tvdb_id:
            r = await treasure_maps.search_show(
                tvdb_id=tvdb_id,
                season=s,
                ep=ep,
                category=cat_id,
                api_url=api_url,
                api_key=api_key,
                session=session,
            )
            for x in r:
                g = x.get("guid") or x.get("link", "")
                if g and g not in seen_guids:
                    seen_guids.add(g)
                    agg_results.append(x)
        if tmdb_id:
            r = await treasure_maps.search_show(
                tmdb_id=tmdb_id,
                season=s,
                ep=ep,
                category=cat_id,
                api_url=api_url,
                api_key=api_key,
                session=session,
            )
            for x in r:
                g = x.get("guid") or x.get("link", "")
                if g and g not in seen_guids:
                    seen_guids.add(g)
                    agg_results.append(x)

        for alias in anime_aliases:
            r = await treasure_maps.search_show(
                title=alias,
                season=s,
                ep=ep,
                category=cat_id,
                api_url=api_url,
                api_key=api_key,
                session=session,
            )
            for x in r:
                g = x.get("guid") or x.get("link", "")
                if g and g not in seen_guids:
                    seen_guids.add(g)
                    agg_results.append(x)

            if ep:
                try:
                    abs_ep = int(ep)
                    abs_title = f"{alias} {abs_ep:02d}"
                    r_abs = await treasure_maps.search_show(
                        title=abs_title,
                        category=cat_id,
                        api_url=api_url,
                        api_key=api_key,
                        session=session,
                    )
                    for x in r_abs:
                        g = x.get("guid") or x.get("link", "")
                        if g and g not in seen_guids:
                            seen_guids.add(g)
                            agg_results.append(x)
                except ValueError:
                    pass

        return agg_results, bool(anime_aliases)

    try:
        if prefer_seasons:
            logger.info(
                "    📦 Searching season pack for S%02d...", season.season_number
            )
            all_season_results: list[dict[str, Any]] = []
            is_season_fallback = False

            for indexer in active_indexers:
                results, is_fallback = await _search_show_id_first(
                    season.season_number,
                    api_url=indexer.api_url,
                    api_key=indexer.api_key,
                )
                if not results:
                    continue
                if is_fallback:
                    is_season_fallback = True

                grabbed = await _evaluate_and_download(
                    session,
                    results,
                    season=season,
                    target_episodes=missing_episodes,
                    reject_words=reject_words,
                    required_language=required_language,
                    is_title_fallback=is_fallback,
                    filters=filters,
                    indexer_name=indexer.name,
                    indexer_url=indexer.api_url,
                    indexer_key=indexer.api_key,
                    early_exit_on_cutoff=True,
                )
                if grabbed or season.status in [
                    SeasonStatus.DOWNLOADING,
                    SeasonStatus.DOWNLOADED,
                    SeasonStatus.COMPLETED,
                    SeasonStatus.MANUAL_GRAB,
                ]:
                    logger.info(
                        "    🎯 Season pack grabbed on indexer '%s'. Halting cascade.",
                        indexer.name,
                    )
                    if season.media_item.auto_monitor_next_season:
                        next_s_stmt = select(Season).where(
                            Season.media_item_id == season.media_item_id,
                            Season.season_number == season.season_number + 1,
                        )
                        next_s = (
                            await session.execute(next_s_stmt)
                        ).scalar_one_or_none()
                        if next_s and not next_s.monitored:
                            next_s.monitored = True
                            next_s.status = SeasonStatus.SEARCHING
                            logger.info(
                                "    🔄 Automatically enabling next season: S%02d",
                                next_s.season_number,
                            )
                            await session.commit()
                    return

                all_season_results.extend(results)

            if all_season_results:
                grabbed = await _evaluate_and_download(
                    session,
                    all_season_results,
                    season=season,
                    target_episodes=missing_episodes,
                    reject_words=reject_words,
                    required_language=required_language,
                    is_title_fallback=is_season_fallback,
                    filters=filters,
                )
                if grabbed or season.status in [
                    SeasonStatus.DOWNLOADING,
                    SeasonStatus.DOWNLOADED,
                    SeasonStatus.COMPLETED,
                    SeasonStatus.MANUAL_GRAB,
                ]:
                    if season.media_item.auto_monitor_next_season:
                        next_s_stmt = select(Season).where(
                            Season.media_item_id == season.media_item_id,
                            Season.season_number == season.season_number + 1,
                        )
                        next_s = (
                            await session.execute(next_s_stmt)
                        ).scalar_one_or_none()
                        if next_s and not next_s.monitored:
                            next_s.monitored = True
                            next_s.status = SeasonStatus.SEARCHING
                            logger.info(
                                "    🔄 Automatically enabling next season: S%02d",
                                next_s.season_number,
                            )
                            await session.commit()
                    return

        # Process individual episodes (up to 5 per cycle, configurable via block_size)
        block_size = (
            profile.episode_block_size if profile and profile.episode_block_size else 5
        )
        for ep in missing_episodes[:block_size]:
            if automation_state_manager.is_aborting():
                logger.info(
                    "🛑 Automation cycle abort requested. Stopping episode processing."
                )
                break
            try:
                logger.info(
                    "    📺 Suche Episode: S%02dE%02d",
                    season.season_number,
                    ep.episode_number,
                )
                all_ep_results: list[dict[str, Any]] = []
                is_ep_fallback = False
                early_grabbed = False

                for indexer in active_indexers:
                    ep_results, is_fallback = await _search_show_id_first(
                        season.season_number,
                        str(ep.episode_number),
                        api_url=indexer.api_url,
                        api_key=indexer.api_key,
                    )

                    # Absolute Episode Numbering is now handled internally by _search_show_id_first for Anime

                    if not ep_results:
                        continue
                    if is_fallback:
                        is_ep_fallback = True

                    grabbed = await _evaluate_and_download(
                        session,
                        ep_results,
                        episode=ep,
                        reject_words=reject_words,
                        required_language=required_language,
                        is_title_fallback=is_fallback,
                        filters=filters,
                        indexer_name=indexer.name,
                        indexer_url=indexer.api_url,
                        indexer_key=indexer.api_key,
                        early_exit_on_cutoff=True,
                    )
                    if grabbed:
                        logger.info(
                            "    🎯 Target cutoff met for S%02dE%02d on indexer '%s'.",
                            season.season_number,
                            ep.episode_number,
                            indexer.name,
                        )
                        early_grabbed = True
                        break

                    all_ep_results.extend(ep_results)

                if not early_grabbed:
                    if all_ep_results:
                        await _evaluate_and_download(
                            session,
                            all_ep_results,
                            episode=ep,
                            reject_words=reject_words,
                            required_language=required_language,
                            is_title_fallback=is_ep_fallback,
                            filters=filters,
                        )
                    else:
                        await _evaluate_and_download(
                            session,
                            [],
                            episode=ep,
                            reject_words=reject_words,
                            required_language=required_language,
                            is_title_fallback=is_ep_fallback,
                            filters=filters,
                        )
            except Exception as ep_err:  # noqa: BLE001
                logger.error(
                    "    ❌ Error searching S%02dE%02d: %s",
                    season.season_number,
                    ep.episode_number,
                    ep_err,
                )
            finally:
                # Politeness delay between consecutive indexer requests
                await asyncio.sleep(1.0)

        # Check if all monitored episodes are finished
        stmt_check = select(Episode).where(
            Episode.season_id == season.id,
            Episode.status.in_([EpisodeStatus.SEARCHING, EpisodeStatus.PENDING]),
            Episode.monitored == True,
        )
        if not (await session.execute(stmt_check)).scalars().all():
            # Season is done
            season.status = SeasonStatus.COMPLETED
            if season.media_item.auto_monitor_next_season:
                next_s_stmt = select(Season).where(
                    Season.media_item_id == season.media_item_id,
                    Season.season_number == season.season_number + 1,
                )
                next_s = (await session.execute(next_s_stmt)).scalar_one_or_none()
                if next_s and not next_s.monitored:
                    next_s.monitored = True
                    next_s.status = SeasonStatus.SEARCHING
                    logger.info(
                        "    🔄 All episodes downloaded. Enabling next season: S%02d",
                        next_s.season_number,
                    )
            await session.commit()

    except DownloaderNetworkError as e:
        logger.error("Network error during season grab, halting cascade: %s", e)
        return


async def _handle_upgrade_failure(
    session,
    target,
    movie,
    season,
    episode,
    is_manual,
    reason_msg,
) -> None:
    import logging

    from app.config import settings
    from app.db.models import (
        BlacklistedRelease,
        EpisodeStatus,
        MediaStatus,
        SeasonStatus,
    )

    logger = logging.getLogger(__name__)

    target.fail_count = 0
    if not is_manual:
        target.upgrade_attempts_count += 1

    if target.upgrade_attempts_count >= settings.max_upgrade_attempts:
        if movie:
            target.status = MediaStatus.COMPLETED
            from sqlalchemy import delete

            await session.execute(
                delete(BlacklistedRelease).where(
                    BlacklistedRelease.media_item_id == movie.id
                )
            )
        elif season:
            target.status = SeasonStatus.COMPLETED
            from sqlalchemy import delete

            await session.execute(
                delete(BlacklistedRelease).where(
                    BlacklistedRelease.media_item_id == season.media_item_id
                )
            )
        elif episode:
            target.status = EpisodeStatus.COMPLETED
            from sqlalchemy import delete

            await session.execute(
                delete(BlacklistedRelease).where(
                    BlacklistedRelease.media_item_id == episode.season.media_item_id
                )
            )
        target.last_error = (
            f"{reason_msg} Max upgrade attempts reached. (Status: COMPLETED)"
        )
    else:
        # Revert to downloaded if it wasn't already completed?
        # Actually, it's either DOWNLOADED or COMPLETED.
        # If it was DOWNLOADED, we keep it as DOWNLOADED.
        # If it was COMPLETED (e.g. manual search on completed), we keep it as COMPLETED.
        # wait! If it's manual, we don't increment, and we keep it what it was.
        # If it wasn't manual, it was DOWNLOADED. So we can just set it to DOWNLOADED safely,
        # UNLESS it's manual and it was COMPLETED.
        if is_manual:
            # Do not change the status, it stays whatever it was
            target.last_error = f"{reason_msg} (Manual search, state unchanged)"
        else:
            if movie:
                target.status = MediaStatus.DOWNLOADED
            elif season:
                target.status = SeasonStatus.DOWNLOADED
            elif episode:
                target.status = EpisodeStatus.DOWNLOADED
            target.last_error = f"{reason_msg} Attempt {target.upgrade_attempts_count}/{settings.max_upgrade_attempts}."

    logger.info("    ❌ %s", target.last_error)
    await session.commit()


async def _evaluate_and_download(
    session: AsyncSession,
    search_results: list[dict[str, Any]],
    movie: MediaItem | None = None,
    season: Season | None = None,
    episode: Any = None,
    target_episodes: Sequence[Any] | None = None,
    reject_words: list[str] | None = None,
    required_language: str | None = None,
    is_title_fallback: bool = False,
    filters: dict[str, Any] | None = None,
    indexer_name: str | None = None,
    indexer_url: str | None = None,
    indexer_key: str | None = None,
    early_exit_on_cutoff: bool = False,
    is_manual: bool = False,
) -> bool:
    from app.db.models import (
        BlacklistedRelease,
        EpisodeStatus,
        MediaStatus,
        ProviderProfile,
        SeasonStatus,
    )
    from app.services import telegram

    if movie is None and season is None and episode is None:
        raise ValueError("Must provide either a movie, a season, or an episode")

    target: Any = episode if episode else (season if season else movie)

    if not search_results:
        if early_exit_on_cutoff:
            return False

        if target.best_score is not None:
            await _handle_upgrade_failure(
                session,
                target,
                movie,
                season,
                episode,
                is_manual,
                "No indexer results found.",
            )
        else:
            target.empty_search_count += 1

            target.last_error = f"Empty search count: {target.empty_search_count}"
            logger.info(
                "    ❌ No search results found (Empty Search Count: %d)",
                target.empty_search_count,
            )

        await session.commit()
        return False

    current_best_score = target.best_score or 0.0

    cutoffs = scoring_config.get("cutoffs", {})
    target_score = cutoffs.get("target_score", 8000)
    upgrade_threshold = cutoffs.get("upgrade_threshold", 500)

    # Fetch blacklisted guids and titles
    media_item_id = (
        movie.id
        if movie
        else (season.media_item_id if season else episode.season.media_item_id)
    )
    stmt = select(BlacklistedRelease).where(
        BlacklistedRelease.media_item_id == media_item_id
    )
    blacklist_res = await session.execute(stmt)
    blacklisted_items = blacklist_res.scalars().all()
    blacklisted_guids = {b.nzb_guid for b in blacklisted_items if b.nzb_guid}
    blacklisted_titles = {b.nzb_title for b in blacklisted_items if b.nzb_title}

    candidates = []

    for item in search_results:
        title = item.get("title", "")
        size_bytes = int(item.get("size", 0))
        guid = item.get("guid", "")

        # Skip blacklisted items
        if guid in blacklisted_guids or title in blacklisted_titles:
            continue

        # Reject if title contains any reject word
        if reject_words and any(rw in title.lower() for rw in reject_words):
            continue

        parsed = parse_release_name(title)

        if filters:

            def is_match(filter_val: str, parsed_val: str | None) -> bool:
                if filter_val == "any":
                    return True
                if not parsed_val:
                    return False
                return filter_val.lower() in parsed_val.lower()

            def check_audio_tier(filter_val: str, parsed_codec: str | None) -> bool:
                if filter_val == "any":
                    return True
                if not parsed_codec:
                    return False
                pc = parsed_codec.lower()
                if filter_val == "tier1" and any(
                    x in pc for x in ["truehd", "dts:x", "auro"]
                ):
                    return True
                if filter_val == "tier2" and any(
                    x in pc for x in ["dts-hd", "lpcm", "flac"]
                ):
                    return True
                if (
                    filter_val == "tier3"
                    and "atmos" in pc
                    and ("eac3" in pc or "dd+" in pc)
                ):
                    return True
                if filter_val == "tier4" and any(
                    x in pc for x in ["eac3", "dts", "ac3", "dolby digital"]
                ):
                    return True
                if filter_val == "tier5" and any(
                    x in pc for x in ["aac", "opus", "mp3"]
                ):
                    return True
                return filter_val == "tier1" and "truehd atmos" in pc

            if not is_match(filters.get("resolution", "any"), parsed.resolution):
                continue
            if not is_match(filters.get("source", "any"), parsed.source):
                continue
            if not is_match(filters.get("hdr", "any"), parsed.hdr):
                continue
            if not is_match(filters.get("video_codec", "any"), parsed.video_codec):
                continue
            if not is_match(
                filters.get("audio_channels", "any"), parsed.audio_channels
            ):
                continue
            if not check_audio_tier(
                filters.get("audio_tier", "any"), parsed.audio_codec
            ):
                continue

        runtime = movie.runtime_minutes if movie else None

        expected_title = None
        expected_year = None
        expected_alt_title = None
        expected_season = None
        expected_episode = None
        if movie:
            expected_title = movie.title
            expected_year = movie.year
            expected_alt_title = movie.alt_title
        elif season:
            expected_title = season.media_item.title
            expected_alt_title = season.media_item.alt_title
            expected_season = season.season_number
        elif episode:
            expected_title = episode.season.media_item.title
            expected_alt_title = episode.season.media_item.alt_title
            expected_season = episode.season.season_number
            expected_episode = episode.episode_number

        score_res = score_release(
            parsed,
            size_bytes,
            runtime,
            expected_title=expected_title,
            expected_year=expected_year,
            expected_alt_title=expected_alt_title,
            expected_season=expected_season,
            expected_episode=expected_episode,
            required_language=required_language,
            api_language=item.get("api_language"),
        )

        if score_res.is_rejected:
            continue

        candidates.append(
            {
                "title": title,
                "guid": guid,
                "size_bytes": size_bytes,
                "parsed": parsed,
                "score_res": score_res,
                "score": score_res.score,
            }
        )

    candidates.sort(key=lambda x: x["score"], reverse=True)

    if not candidates:
        if early_exit_on_cutoff:
            return False

        if target.best_score is not None:
            await _handle_upgrade_failure(
                session,
                target,
                movie,
                season,
                episode,
                is_manual,
                "No matching releases found.",
            )
        else:
            target.empty_search_count += 1
            target.last_error = f"Empty search count: {target.empty_search_count}"
            logger.info(
                "    ❌ No matching releases found (Empty Search Count: %d)",
                target.empty_search_count,
            )

        await session.commit()
        return False

    best_candidate = candidates[0]

    # If checking for early exit on target cutoff score
    if early_exit_on_cutoff and best_candidate["score"] < target_score:
        return False

    log_title = ""
    if episode and episode.season and episode.season.media_item:
        log_title = f"{episode.season.media_item.title} S{episode.season.season_number:02d}E{episode.episode_number:02d}"
    elif season and season.media_item:
        log_title = f"{season.media_item.title} S{season.season_number:02d}"
    elif movie:
        log_title = movie.title

    logger.info("    🏆 Top %d releases for %s:", min(5, len(candidates)), log_title)
    for i, c in enumerate(candidates[:5]):
        logger.info("       %d. [%.1f] %s", i + 1, c["score"], c["title"])

    if early_exit_on_cutoff and best_candidate["score"] >= target_score:
        logger.info(
            "    🎯 Target cutoff threshold reached (%.1f >= %d) on indexer '%s'. Triggering early exit.",
            best_candidate["score"],
            target_score,
            indexer_name or "active",
        )

    # If this was a title-search fallback, don't auto-send to TorBox.
    # Instead store the best candidate for manual approval.
    if is_title_fallback:
        from app.db.models import EpisodeStatus, MediaStatus, SeasonStatus

        logger.warning(
            "    ⚠️ Result from title search — requires manual confirmation before downloading."
        )
        pending = {
            "title": best_candidate["title"],
            "guid": best_candidate["guid"],
            "score": best_candidate["score"],
            "size_bytes": best_candidate["size_bytes"],
            "resolution": best_candidate["parsed"].resolution,
            "source": best_candidate["parsed"].source,
            "release_group": best_candidate["parsed"].release_group,
        }
        target.pending_candidate_json = pending
        new_manual_status = (
            MediaStatus.MANUAL_GRAB
            if movie
            else (SeasonStatus.MANUAL_GRAB if season else EpisodeStatus.MANUAL_GRAB)
        )
        target.status = new_manual_status
        target.fail_count = 0
        target.empty_search_count = 0
        target.last_error = None
        if target_episodes:
            for ep in target_episodes:
                ep.status = EpisodeStatus.MANUAL_GRAB
                ep.pending_candidate_json = pending
        await session.commit()
        return True

    # Check if we should download
    should_download = False

    if target.best_score is None or best_candidate["score"] >= (
        current_best_score + upgrade_threshold
    ):
        should_download = True

    if not should_download:
        if early_exit_on_cutoff:
            return False

        await _handle_upgrade_failure(
            session,
            target,
            movie,
            season,
            episode,
            is_manual,
            f"Best release (Score {best_candidate['score']}) is below upgrade threshold.",
        )
        return False

    from app.config import settings

    if should_download and scoring_config.get("automation", {}).get(
        "auto_send_to_torbox", True
    ):
        # Check hard daily grab limit (400 grabs/day)
        if not await can_grab_today(session, limit=400):
            from app.core.failure_logger import log_failure

            err_msg = "⚠️ Daily grab limit of 400 NZB downloads reached. Skipping grab."
            log_failure(session, target, "system_limit", err_msg)
            logger.warning(
                "    ⚠️ Daily grab limit of 400 reached. Skipping grab for '%s'.",
                best_candidate["title"],
            )
            await session.commit()
            return False

        if settings.dry_run:
            logger.info(
                "    🧪 [DRY RUN] Would send file '%s' (Score: %s) to TorBox.",
                best_candidate["title"],
                best_candidate["score"],
            )
            return True

        logger.info(
            "    📥 Sending to TorBox: %s (Score: %s)",
            best_candidate["title"],
            best_candidate["score"],
        )

        try:
            nzb_bytes, filename = await treasure_maps.fetch_nzb_bytes(
                best_candidate["guid"],
                api_url=indexer_url,
                api_key=indexer_key,
                session=session,
            )
            torbox_result = await torbox.send_nzb_file(
                nzb_bytes, filename=filename, session=session
            )
        except treasure_maps.IndexerError as e:
            from app.core.failure_logger import log_failure

            err_msg = f"NZB download failed: {e}"
            log_failure(session, target, "indexer_error", err_msg)
            logger.error("    ❌ %s", target.last_error)
            await session.commit()
            return False

        if not torbox_result or (
            not torbox_result.get("hash") and not torbox_result.get("id")
        ):
            from app.core.failure_logger import log_failure

            err_msg = str(
                torbox_result.get("error")
                if isinstance(torbox_result, dict) and torbox_result.get("error")
                else "Error sending to TorBox."
            )
            log_failure(session, target, "torbox_error", err_msg)
            logger.error("    ❌ %s", target.last_error)
            await session.commit()
            return False

        if torbox_result and (torbox_result.get("hash") or torbox_result.get("id")):
            await increment_today_grab_count(session)
            history = DownloadHistory(
                media_item_id=media_item_id,
                season_id=season.id
                if season
                else (episode.season_id if episode else None),
                episode_id=episode.id if episode else None,
                nzb_title=best_candidate["title"],
                nzb_guid=best_candidate["guid"],
                score=best_candidate["score"],
                size_bytes=best_candidate["size_bytes"],
                resolution=best_candidate["parsed"].resolution,
                video_codec=best_candidate["parsed"].video_codec,
                audio_codec=best_candidate["parsed"].audio_codec,
                source=best_candidate["parsed"].source,
                release_group=best_candidate["parsed"].release_group,
                bitrate_mbps=best_candidate["score_res"].bitrate_mbps,
                torbox_hash=str(torbox_result.get("hash"))
                if torbox_result.get("hash")
                else None,
                torbox_id=str(torbox_result.get("id"))
                if torbox_result.get("id")
                else None,
                torbox_sent_at=datetime.now(timezone.utc),
            )
            session.add(history)

            if best_candidate["score"] >= target_score:
                logger.info("    ✅ Target score reached.")

            new_status = (
                MediaStatus.DOWNLOADING
                if movie
                else (SeasonStatus.DOWNLOADING if season else EpisodeStatus.DOWNLOADING)
            )
            target.status = new_status
            target.fail_count = 0
            target.empty_search_count = 0
            target.last_error = None
            target.upgrade_attempts_count = 0

            # If target_episodes is passed (e.g. season pack downloaded), update all of them
            if target_episodes:
                for ep in target_episodes:
                    ep.status = new_status
                    ep.fail_count = 0
                    ep.empty_search_count = 0
                    ep.last_error = None
                    ep.upgrade_attempts_count = 0

            await session.commit()

            # Send Telegram notification
            media_item = (
                movie
                if movie
                else (season.media_item if season else episode.season.media_item)
            )
            profile_stmt = (
                select(ProviderProfile)
                .where(
                    ProviderProfile.provider_id == media_item.provider_id,
                    ProviderProfile.media_type
                    == ("movies" if media_item.media_type == "movie" else "shows"),
                )
                .options(selectinload(ProviderProfile.notification_channel))
            )
            profile_res = await session.execute(profile_stmt)
            profile = profile_res.scalar_one_or_none()

            if profile and profile.notification_channel:
                channel = profile.notification_channel
                if channel.type == "telegram" and channel.bot_token and channel.chat_id:
                    msg = f"✅ <b>Started Download</b>\n\n<b>{log_title}</b>\n<code>{best_candidate['title']}</code>\n\nScore: {best_candidate['score']}"
                    await telegram.send_notification(
                        msg, token=channel.bot_token, chat_id=channel.chat_id
                    )

            return True

    return False


async def manual_search_episode(session: AsyncSession, episode_id: int) -> bool:
    """Manually search and download a single episode synchronously."""
    from sqlalchemy.orm import selectinload

    from app.db.models import Episode, EpisodeStatus, MediaType, ProviderProfile

    stmt = (
        select(Episode)
        .where(Episode.id == episode_id)
        .options(
            selectinload(Episode.season)
            .selectinload(Season.media_item)
            .selectinload(MediaItem.provider)
        )
    )
    episode = (await session.execute(stmt)).scalar_one_or_none()

    if not episode or not episode.season or not episode.season.media_item:
        logger.error("❌ Episode %s not found or incomplete.", episode_id)
        return False

    season = episode.season
    media_item = season.media_item

    logger.info(
        "🔍 Manual search started for: %s S%02dE%02d gestartet",
        media_item.title,
        season.season_number,
        episode.episode_number,
    )

    # Status update to searching
    episode.status = EpisodeStatus.SEARCHING
    await session.commit()
    from app.core.self_healing import match_and_adopt_target_from_cache

    await match_and_adopt_target_from_cache(session, episode.season.media_item)

    tvdb_id = media_item.tvdb_id
    tmdb_id = media_item.tmdb_id
    cat_id = media_item.provider.series_category_id if media_item.provider else None

    if (
        media_item.media_type == MediaType.ANIME
        and media_item.provider
        and media_item.provider.anime_category_id
    ):
        cat_id = media_item.provider.anime_category_id

    profile_stmt = select(ProviderProfile).where(
        ProviderProfile.provider_id == media_item.provider_id,
        ProviderProfile.media_type == "shows",
    )
    profile = (await session.execute(profile_stmt)).scalar_one_or_none()
    reject_words = (
        [w.strip().lower() for w in profile.reject_words_csv.split(",") if w.strip()]
        if profile and profile.reject_words_csv
        else []
    )

    ep_str = str(episode.episode_number)
    results = await treasure_maps.search_show(
        tvdb_id=tvdb_id,
        tmdb_id=tmdb_id,
        title=media_item.title,
        season=season.season_number,
        ep=ep_str,
        category=cat_id,
    )

    # Fallback for Anime Absolute Episode Numbering
    if not results and media_item.media_type == MediaType.ANIME:
        ep_title_search = f"{media_item.title} {episode.episode_number:02d}"
        results = await treasure_maps.search_show(
            title=ep_title_search, category=cat_id
        )

    if not results:
        episode.fail_count += 1
        episode.last_error = "No matching releases found for this episode."
        episode.status = EpisodeStatus.PENDING  # Revert back
        logger.warning("❌ %s", episode.last_error)
        await session.commit()
        return False

    # Evaluate and download
    # We pass target_episodes=[episode] so the status is updated to downloaded/completed correctly
    await _evaluate_and_download(
        session,
        results,
        episode=episode,
        target_episodes=[episode],
        reject_words=reject_words,
    )

    # Refresh to see if status changed
    await session.refresh(episode)
    return episode.status in [EpisodeStatus.DOWNLOADED, EpisodeStatus.COMPLETED]


async def manual_search_movie(session: AsyncSession, item_id: int) -> bool:
    """Manually search and download a single movie synchronously."""
    from sqlalchemy.orm import selectinload

    from app.db.models import MediaItem, MediaStatus, MediaType, ProviderProfile

    stmt = (
        select(MediaItem)
        .where(MediaItem.id == item_id)
        .options(selectinload(MediaItem.provider))
    )
    media_item = (await session.execute(stmt)).scalar_one_or_none()

    if not media_item or media_item.media_type != MediaType.MOVIE:
        logger.error("❌ Movie %s not found or is not a movie.", item_id)
        return False

    logger.info("🔍 Manual search started for movie: %s gestartet", media_item.title)

    media_item.status = MediaStatus.SEARCHING
    await session.commit()
    from app.core.self_healing import match_and_adopt_target_from_cache

    await match_and_adopt_target_from_cache(session, media_item)

    imdb_id = media_item.imdb_id
    tmdb_id = media_item.tmdb_id
    cat_id = media_item.provider.movie_category_id if media_item.provider else None

    profile_stmt = select(ProviderProfile).where(
        ProviderProfile.provider_id == media_item.provider_id,
        ProviderProfile.media_type == "movies",
    )
    profile = (await session.execute(profile_stmt)).scalar_one_or_none()
    reject_words = (
        [w.strip().lower() for w in profile.reject_words_csv.split(",") if w.strip()]
        if profile and profile.reject_words_csv
        else []
    )

    results = await treasure_maps.search_movie(
        imdb_id=imdb_id, tmdb_id=tmdb_id, title=media_item.title, category=cat_id
    )

    if not results:
        media_item.fail_count += 1
        media_item.last_error = "No matching releases found for this movie."
        rd = media_item.release_date
        if rd and rd.tzinfo is None:
            rd = rd.replace(tzinfo=timezone.utc)
        if (rd and rd > datetime.now(timezone.utc)) or (
            not rd and media_item.year and media_item.year > datetime.now().year
        ):
            media_item.status = MediaStatus.FUTURE
        else:
            media_item.status = MediaStatus.SEARCHING
        logger.warning("❌ %s", media_item.last_error)
        await session.commit()
        return False

    await _evaluate_and_download(
        session, results, movie=media_item, reject_words=reject_words, is_manual=True
    )

    await session.refresh(media_item)
    return media_item.status in [MediaStatus.DOWNLOADED, MediaStatus.COMPLETED]


async def _handle_print_upgrade_failure(
    session: AsyncSession,
    target: Any,
    reason_msg: str,
) -> None:
    from app.config import settings
    from app.db.models import EpisodeStatus, MediaStatus

    if getattr(target, "status", None) not in (
        MediaStatus.DOWNLOADED,
        EpisodeStatus.DOWNLOADED,
    ):
        target.empty_search_count += 1
        target.last_error = reason_msg
        await session.commit()
        return

    target.upgrade_attempts_count += 1

    if target.upgrade_attempts_count >= settings.max_upgrade_attempts:
        if hasattr(target, "manga_id"):  # MangaVolume
            target.status = EpisodeStatus.COMPLETED
        else:
            target.status = MediaStatus.COMPLETED
        target.last_error = (
            f"{reason_msg} Max upgrade attempts reached. (Status: COMPLETED)"
        )
    else:
        target.last_error = f"{reason_msg} Attempt {target.upgrade_attempts_count}/{settings.max_upgrade_attempts}."

    logger.info("    ❌ %s", target.last_error)
    await session.commit()


async def process_print_book(session: AsyncSession, book: BookItem) -> bool:
    """Process an individual BookItem: search indexers, evaluate, score, and dispatch to TorBox."""
    from datetime import datetime, timezone

    from app.config import settings
    from app.core.fake_detector import is_indexer_metadata_fake, is_nzb_content_fake
    from app.core.reading_scorer import detect_print_format, score_print_release
    from app.db.grab_tracker import can_grab_today, increment_today_grab_count
    from app.db.models import MediaStatus
    from app.services import torbox, treasure_maps

    if (
        book.status == MediaStatus.DOWNLOADED
        and book.best_score is not None
        and book.best_score >= 1000
    ):
        # Already at top tier, no need to search for upgrade
        return False

    logger.info("  📖 Searching indexers for Book: %s", book.title)

    # Categories 7000 (General Books), 7020 (E-Books)
    cat_ids = [7000, 7020]
    queries = []
    if book.author:
        queries.append(f"{book.title} {book.author}".strip())
    queries.append(book.title.strip())

    active_indexers = await provider_service.get_active_indexers(session)
    if not active_indexers:
        logger.warning("  ⚠️ No active indexers configured.")
        book.last_error = "No active indexers configured."
        await session.commit()
        return False

    results: list[dict] = []
    seen_keys: set[str] = set()

    for indexer in active_indexers:
        for q in queries:
            for cid in cat_ids:
                try:
                    raw_res = await treasure_maps.search_raw(
                        query=q,
                        category=cid,
                        api_url=indexer.api_url,
                        api_key=indexer.api_key,
                        session=session,
                    )
                    for r in raw_res:
                        dedup_key = (
                            r.get("guid")
                            or (
                                f"{r.get('title', '').strip().lower()}_{r.get('size', 0)}"
                                if r.get("title")
                                else None
                            )
                            or r.get("link", "")
                        )
                        if dedup_key and dedup_key not in seen_keys:
                            seen_keys.add(dedup_key)
                            r["_indexer_url"] = indexer.api_url
                            r["_indexer_key"] = indexer.api_key
                            results.append(r)
                except Exception as e:
                    logger.warning(
                        "  ⚠️ Indexer '%s' search failed for query '%s': %s",
                        indexer.name,
                        q,
                        e,
                    )

    if not results:
        book.last_searched_at = datetime.now(timezone.utc)
        await _handle_print_upgrade_failure(
            session, book, "No results found on indexer."
        )
        logger.info("  ℹ️ No results found for Book: %s", book.title)
        return False

    # Filter and score candidates
    blacklisted_formats = [
        f.strip().lower()
        for f in settings.book_blacklisted_formats.split(",")
        if f.strip()
    ]
    candidates: list[dict] = []

    for item in results:
        is_fake, fake_reason = is_indexer_metadata_fake(item, media_type="book")
        if is_fake:
            continue

        fmt = detect_print_format(
            title=item.get("title", ""),
            description=item.get("description", ""),
            category_id=item.get("category_id"),
            media_type="book",
        )

        if fmt in blacklisted_formats:
            logger.info(
                "  ⚠️ Skipping release with blacklisted format '%s': %s",
                fmt,
                item.get("title"),
            )
            continue

        scored = score_print_release(fmt, media_type="book")
        candidates.append(
            {
                "item": item,
                "format": fmt,
                "score": scored["score"],
                "title": item.get("title", ""),
                "guid": item.get("guid") or item.get("link", ""),
            }
        )

    if not candidates:
        book.last_searched_at = datetime.now(timezone.utc)
        await _handle_print_upgrade_failure(
            session,
            book,
            "All results were excluded by fake detection or format blacklists.",
        )
        return False

    candidates.sort(key=lambda c: c["score"], reverse=True)
    best_candidate = candidates[0]

    logger.info(
        "  🏆 Best candidate for Book '%s': %s (Score: %s, Format: %s)",
        book.title,
        best_candidate["title"],
        best_candidate["score"],
        best_candidate["format"],
    )

    if book.status == MediaStatus.DOWNLOADED and book.best_score is not None:
        if best_candidate["score"] <= book.best_score:
            book.last_searched_at = datetime.now(timezone.utc)
            await _handle_print_upgrade_failure(
                session,
                book,
                f"Best release score {best_candidate['score']} is not higher than current best {book.best_score}.",
            )
            return False

    if not await can_grab_today(session, limit=400):
        book.last_error = "Daily grab limit of 400 reached."
        logger.warning(
            "  ⚠️ Daily grab limit reached. Skipping grab for Book '%s'.", book.title
        )
        await session.commit()
        return False

    try:
        idx_url = best_candidate["item"].get("_indexer_url")
        idx_key = best_candidate["item"].get("_indexer_key")
        nzb_bytes, filename = await treasure_maps.fetch_nzb_bytes(
            best_candidate["guid"],
            api_url=idx_url,
            api_key=idx_key,
            session=session,
        )
        is_fake_content, fake_content_reason = is_nzb_content_fake(
            nzb_bytes, media_type="book"
        )
        if is_fake_content:
            logger.warning(
                "  ❌ Fake NZB content detected for Book '%s': %s",
                book.title,
                fake_content_reason,
            )
            book.last_error = f"Fake content: {fake_content_reason}"
            await session.commit()
            return False

        torbox_res = await torbox.send_nzb_file(
            nzb_bytes, filename=filename or f"{book.title}.nzb", session=session
        )
    except Exception as e:
        book.last_error = f"NZB fetch or dispatch error: {e}"
        logger.error("  ❌ %s", book.last_error)
        await session.commit()
        return False

    if not torbox_res or (not torbox_res.get("hash") and not torbox_res.get("id")):
        err_val = (
            torbox_res.get("error") if isinstance(torbox_res, dict) else "TorBox error"
        )
        book.last_error = str(err_val) if err_val else "TorBox error"
        logger.error("  ❌ Failed to send book to TorBox: %s", book.last_error)
        await session.commit()
        return False

    await increment_today_grab_count(session)
    book.status = MediaStatus.DOWNLOADING
    book.best_score = best_candidate["score"]
    book.upgrade_attempts_count = 0
    book.empty_search_count = 0
    book.last_error = None
    book.last_searched_at = datetime.now(timezone.utc)
    await session.commit()
    logger.info("  📥 Book '%s' dispatched to TorBox successfully.", book.title)
    return True


async def process_print_manga(session: AsyncSession, manga: MangaItem) -> bool:
    """
    Process Manga series using Pack-First search cascade:
    1. Check for complete packs or volume batches covering wanted volumes.
    2. If pack grabbed, SUPPRESS all individual searches for included volumes.
    3. If no pack found, fall back to searching individual wanted volumes.
    """
    import re
    from datetime import datetime, timezone

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.config import settings
    from app.core.fake_detector import is_indexer_metadata_fake, is_nzb_content_fake
    from app.core.reading_scorer import (
        detect_print_format,
        match_volume_or_issue,
        score_print_release,
    )
    from app.db.grab_tracker import can_grab_today, increment_today_grab_count
    from app.db.models import EpisodeStatus, MangaItem, MediaStatus
    from app.services import torbox, treasure_maps

    stmt = (
        select(MangaItem)
        .options(selectinload(MangaItem.volumes))
        .where(MangaItem.id == manga.id)
    )
    manga_obj = (await session.execute(stmt)).scalar_one_or_none()
    if not manga_obj:
        return False
    manga = manga_obj

    wanted_vols = []
    for v in manga.volumes:
        if v.status in [EpisodeStatus.PENDING, EpisodeStatus.SEARCHING]:
            wanted_vols.append(v)
        elif v.status == EpisodeStatus.DOWNLOADED:
            best_score_val = getattr(v, "best_score", None)
            if best_score_val is not None and best_score_val >= 1000:
                continue
            wanted_vols.append(v)
    if not wanted_vols:
        return False

    wanted_numbers = {v.volume_number for v in wanted_vols}
    blacklisted_formats = [
        f.strip().lower()
        for f in settings.manga_blacklisted_formats.split(",")
        if f.strip()
    ]
    cat_ids = [7030, 7000]  # Comics / Manga

    logger.info(
        "  📚 Processing Manga: %s (%d wanted volumes: %s)",
        manga.title,
        len(wanted_vols),
        sorted(wanted_numbers),
    )

    active_indexers = await provider_service.get_active_indexers(session)
    if not active_indexers:
        logger.warning("  ⚠️ No active indexers configured.")
        return False

    # -----------------------------------------------------------------------
    # Step 1: Pack-First Search Cascade
    # -----------------------------------------------------------------------
    pack_queries = [
        f"{manga.title} Complete",
        f"{manga.title} Vol 01-{max(wanted_numbers):02d}",
        f"{manga.title} Vol 1-{max(wanted_numbers)}",
        f"{manga.title}",
    ]

    pack_results: list[dict] = []
    seen_pack_keys: set[str] = set()

    for indexer in active_indexers:
        for pq in pack_queries:
            for cid in cat_ids:
                try:
                    raw_res = await treasure_maps.search_raw(
                        query=pq,
                        category=cid,
                        api_url=indexer.api_url,
                        api_key=indexer.api_key,
                        session=session,
                    )
                    for r in raw_res:
                        dedup_key = (
                            r.get("guid")
                            or (
                                f"{r.get('title', '').strip().lower()}_{r.get('size', 0)}"
                                if r.get("title")
                                else None
                            )
                            or r.get("link", "")
                        )
                        if dedup_key and dedup_key not in seen_pack_keys:
                            seen_pack_keys.add(dedup_key)
                            r["_indexer_url"] = indexer.api_url
                            r["_indexer_key"] = indexer.api_key
                            pack_results.append(r)
                except Exception as e:
                    logger.warning(
                        "  ⚠️ Indexer '%s' pack search failed for query '%s': %s",
                        indexer.name,
                        pq,
                        e,
                    )

    best_pack: dict | None = None
    best_pack_covered_vols: set[int] = set()

    for item in pack_results:
        item_title = item.get("title", "")
        is_fake, _ = is_indexer_metadata_fake(item, media_type="manga")
        if is_fake:
            continue

        fmt = detect_print_format(
            title=item_title,
            description=item.get("description", ""),
            category_id=item.get("category_id"),
            media_type="manga",
        )
        if fmt in blacklisted_formats:
            continue

        # Check if release is a multi-volume pack or complete series
        is_complete = bool(
            re.search(r"\b(complete|all\s+volumes|series)\b", item_title, re.IGNORECASE)
        )
        range_match = re.search(
            r"(?:vol(?:ume)?|v)?\.?\s*0*(\d+)\s*(?:-|–|to)\s*(?:vol(?:ume)?|v)?\.?\s*0*(\d+)",
            item_title,
            re.IGNORECASE,
        )

        covered: set[int] = set()
        if is_complete:
            covered = set(wanted_numbers)
        elif range_match:
            try:
                start_v, end_v = int(range_match.group(1)), int(range_match.group(2))
                covered = {
                    v_num for v_num in wanted_numbers if start_v <= v_num <= end_v
                }
            except (ValueError, IndexError):
                pass

        # We consider it a qualifying pack if it covers at least 2 wanted volumes or all wanted volumes
        if len(covered) >= 2 or (
            len(covered) == len(wanted_numbers) and len(wanted_numbers) > 0
        ):
            scored = score_print_release(fmt, media_type="manga")
            pack_score = scored["score"] + (
                len(covered) * 50
            )  # bonus for volume coverage

            if not best_pack or pack_score > best_pack["score"]:
                best_pack = {
                    "item": item,
                    "format": fmt,
                    "score": pack_score,
                    "title": item_title,
                    "guid": item.get("guid") or item.get("link", ""),
                }
                best_pack_covered_vols = covered

    # If a qualifying pack was found, grab it and SUPPRESS individual searches
    if best_pack and best_pack_covered_vols:
        logger.info(
            "  📦 Found qualifying Manga Pack for '%s': %s (Covers %d volumes: %s)",
            manga.title,
            best_pack["title"],
            len(best_pack_covered_vols),
            sorted(best_pack_covered_vols),
        )

        if await can_grab_today(session, limit=400):
            try:
                idx_url = best_pack["item"].get("_indexer_url")
                idx_key = best_pack["item"].get("_indexer_key")
                nzb_bytes, filename = await treasure_maps.fetch_nzb_bytes(
                    best_pack["guid"],
                    api_url=idx_url,
                    api_key=idx_key,
                    session=session,
                )
                is_fake_content, _ = is_nzb_content_fake(nzb_bytes, media_type="manga")
                if not is_fake_content:
                    torbox_res = await torbox.send_nzb_file(
                        nzb_bytes,
                        filename=filename or f"{manga.title}_Pack.nzb",
                        session=session,
                    )
                    if torbox_res and (torbox_res.get("hash") or torbox_res.get("id")):
                        await increment_today_grab_count(session)

                        # CRITICAL: Suppress individual searches by marking all covered volumes DOWNLOADING
                        for vol in manga.volumes:
                            if vol.volume_number in best_pack_covered_vols:
                                vol.status = EpisodeStatus.DOWNLOADING
                                vol.best_score = best_pack["score"]
                                vol.upgrade_attempts_count = 0
                                vol.last_searched_at = datetime.now(timezone.utc)
                                vol.empty_search_count = 0

                        manga.status = MediaStatus.DOWNLOADING
                        manga.last_searched_at = datetime.now(timezone.utc)
                        await session.commit()
                        logger.info(
                            "  🚀 Grabbed Manga Pack! Suppressed individual searches for volumes: %s",
                            sorted(best_pack_covered_vols),
                        )
                        return True
            except Exception as e:
                logger.error("  ❌ Failed to grab Manga Pack: %s", e)

    # -----------------------------------------------------------------------
    # Step 2: Individual Volume Fallback Search
    # -----------------------------------------------------------------------
    logger.info(
        "  🔍 Falling back to individual volume searches for Manga: %s", manga.title
    )
    any_volume_grabbed = False

    for vol in wanted_vols:
        if automation_state_manager.is_aborting():
            logger.info(
                "🛑 Print automation cycle abort requested. Stopping volume processing."
            )
            break
        n = vol.volume_number
        vol_queries = [
            f"{manga.title} v{n:02d}",
            f"{manga.title} vol {n}",
            f"{manga.title} Volume {n}",
            f"{manga.title} {n:02d}",
        ]

        vol_results: list[dict] = []
        vol_seen_keys: set[str] = set()

        for indexer in active_indexers:
            for vq in vol_queries:
                for cid in cat_ids:
                    try:
                        raw_res = await treasure_maps.search_raw(
                            query=vq,
                            category=cid,
                            api_url=indexer.api_url,
                            api_key=indexer.api_key,
                            session=session,
                        )
                        for r in raw_res:
                            dedup_key = (
                                r.get("guid")
                                or (
                                    f"{r.get('title', '').strip().lower()}_{r.get('size', 0)}"
                                    if r.get("title")
                                    else None
                                )
                                or r.get("link", "")
                            )
                            if dedup_key and dedup_key not in vol_seen_keys:
                                vol_seen_keys.add(dedup_key)
                                r["_indexer_url"] = indexer.api_url
                                r["_indexer_key"] = indexer.api_key
                                vol_results.append(r)
                    except Exception as e:
                        logger.warning(
                            "  ⚠️ Volume search failed for query '%s': %s", vq, e
                        )

        candidates: list[dict] = []
        for item in vol_results:
            item_title = item.get("title", "")
            is_fake, _ = is_indexer_metadata_fake(item, media_type="manga")
            if is_fake:
                continue

            is_match, _ = match_volume_or_issue(item_title, n)
            if not is_match:
                continue

            fmt = detect_print_format(
                title=item_title,
                description=item.get("description", ""),
                category_id=item.get("category_id"),
                media_type="manga",
            )
            if fmt in blacklisted_formats:
                continue

            scored = score_print_release(fmt, media_type="manga")
            candidates.append(
                {
                    "item": item,
                    "format": fmt,
                    "score": scored["score"],
                    "title": item_title,
                    "guid": item.get("guid") or item.get("link", ""),
                }
            )

        vol.last_searched_at = datetime.now(timezone.utc)

        if not candidates:
            await _handle_print_upgrade_failure(
                session,
                vol,
                "All results were excluded by fake detection or format blacklists.",
            )
            continue

        candidates.sort(key=lambda c: c["score"], reverse=True)
        best_vol_cand = candidates[0]

        if (
            vol.status == EpisodeStatus.DOWNLOADED
            and getattr(vol, "best_score", None) is not None
        ):
            if best_vol_cand["score"] <= vol.best_score:
                await _handle_print_upgrade_failure(
                    session,
                    vol,
                    f"Best release score {best_vol_cand['score']} is not higher than current best {vol.best_score}.",
                )
                continue

        if not await can_grab_today(session, limit=400):
            logger.warning("  ⚠️ Daily grab limit reached. Stopping volume grabs.")
            break

        try:
            idx_url = best_vol_cand["item"].get("_indexer_url")
            idx_key = best_vol_cand["item"].get("_indexer_key")
            nzb_bytes, filename = await treasure_maps.fetch_nzb_bytes(
                best_vol_cand["guid"],
                api_url=idx_url,
                api_key=idx_key,
                session=session,
            )
            is_fake_content, _ = is_nzb_content_fake(nzb_bytes, media_type="manga")
            if is_fake_content:
                continue

            torbox_res = await torbox.send_nzb_file(
                nzb_bytes,
                filename=filename or f"{manga.title}_Vol_{n}.nzb",
                session=session,
            )
            if torbox_res and (torbox_res.get("hash") or torbox_res.get("id")):
                await increment_today_grab_count(session)
                vol.status = EpisodeStatus.DOWNLOADING
                vol.best_score = best_vol_cand["score"]
                vol.upgrade_attempts_count = 0
                vol.empty_search_count = 0
                any_volume_grabbed = True
                logger.info(
                    "  📥 Manga Volume %d for '%s' dispatched to TorBox.",
                    n,
                    manga.title,
                )
        except Exception as e:
            logger.error(
                "  ❌ Failed to grab Volume %d for '%s': %s", n, manga.title, e
            )

    await session.commit()
    return any_volume_grabbed


async def run_print_automation_cycle(force: bool = False) -> None:
    """
    Background job for Print Media (Books & Manga).
    - Processes Books (Immediate Wanted search & grab).
    - Processes Manga (Pack-First search cascade with individual volume search suppression).
    """
    automation_state_manager.set_running(AutomationStatus.RUNNING_PRINT)
    logger.info("=== Start Print Media Automation Cycle ===")
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.db.database import async_session_factory
        from app.db.models import BookItem, MangaItem, MediaStatus

        async with async_session_factory() as session:
            # 1. Process Books in SEARCHING / PENDING state
            stmt_books = select(BookItem).where(
                BookItem.status.in_(
                    [MediaStatus.PENDING, MediaStatus.SEARCHING, MediaStatus.DOWNLOADED]
                )
            )
            books = (await session.execute(stmt_books)).scalars().all()
            for book in books:
                if automation_state_manager.is_aborting():
                    logger.info(
                        "🛑 Print automation cycle abort requested. Stopping book processing."
                    )
                    break
                try:
                    await process_print_book(session, book)
                except Exception as e:
                    logger.error("Error processing Book '%s': %s", book.title, e)

            # 2. Process Manga with wanted volumes
            if not automation_state_manager.is_aborting():
                stmt_manga = (
                    select(MangaItem)
                    .options(selectinload(MangaItem.volumes))
                    .where(
                        MangaItem.status.in_(
                            [
                                MediaStatus.PENDING,
                                MediaStatus.SEARCHING,
                                MediaStatus.DOWNLOADING,
                                MediaStatus.DOWNLOADED,
                            ]
                        )
                    )
                )
                mangas = (await session.execute(stmt_manga)).scalars().all()
                for manga in mangas:
                    if automation_state_manager.is_aborting():
                        logger.info(
                            "🛑 Print automation cycle abort requested. Stopping manga processing."
                        )
                        break
                    try:
                        await process_print_manga(session, manga)
                    except Exception as e:
                        logger.error("Error processing Manga '%s': %s", manga.title, e)
    finally:
        automation_state_manager.reset()
        logger.info("=== End Print Media Automation Cycle ===")
