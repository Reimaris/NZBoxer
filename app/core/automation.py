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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import scoring_config
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
from app.services import simkl, tmdb, torbox, treasure_maps

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
                            else:
                                if s_air_date:
                                    existing_season.air_date = s_air_date
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


async def _sync_season_episodes(session: AsyncSession, season: Season) -> None:
    from app.db.models import Episode, EpisodeStatus

    if not season.media_item or not season.media_item.tmdb_id:
        return

    season_details = await tmdb.get_season_details(
        season.media_item.tmdb_id, season.season_number
    )
    if not season_details:
        return

    episodes_data = season_details.get("episodes", [])

    # Check existing episodes
    await session.refresh(season, ["episodes"])
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
    log_process_start(logger, "Automation Cycle")
    try:
        if force:
            logger.info(
                "🔍 Manual search run started (bypassing interval multiplier)..."
            )
        else:
            logger.info("🔄 Starting automation cycle...")

        # 0. Self-healing: fix failed downloads before searching
        await run_self_healing_cycle()
        await run_download_check_cycle()

        # 1. Sync watchlist first
        await sync_simkl_watchlist()

        async with async_session_factory() as session:
            # 1.5 Sync episodes for monitored seasons
            stmt_s = (
                select(Season)
                .where(
                    Season.monitored == True,
                    Season.status.in_(
                        [
                            SeasonStatus.SEARCHING,
                            SeasonStatus.PENDING,
                            SeasonStatus.FUTURE,
                        ]
                    ),
                )
                .options(selectinload(Season.media_item))
            )
            seasons_result = await session.execute(stmt_s)
            for season in seasons_result.scalars():
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

            # Provider Cycle Throttling
            from app.db.models import ProviderProfile

            profiles = (await session.execute(select(ProviderProfile))).scalars().all()
            active_movie_provider_ids = []
            active_shows_provider_ids = []

            for p in profiles:
                if force:
                    # Manual run: reset counter and always run all providers
                    p.current_cycle_count = 0
                    if p.media_type == "movies":
                        active_movie_provider_ids.append(p.provider_id)
                    elif p.media_type == "shows":
                        active_shows_provider_ids.append(p.provider_id)
                else:
                    p.current_cycle_count += 1
                    if p.current_cycle_count >= p.search_cycle_skip:
                        p.current_cycle_count = 0
                        if p.media_type == "movies":
                            active_movie_provider_ids.append(p.provider_id)
                        elif p.media_type == "shows":
                            active_shows_provider_ids.append(p.provider_id)

            await session.commit()

            if active_movie_provider_ids:
                # 2. Process Movies
                stmt_movies = (
                    select(MediaItem)
                    .where(
                        MediaItem.status == MediaStatus.SEARCHING,
                        MediaItem.media_type == MediaType.MOVIE,
                        MediaItem.provider_id.in_(active_movie_provider_ids),
                    )
                    .options(selectinload(MediaItem.download_history))
                )

                movies_result = await session.execute(stmt_movies)
                for movie in movies_result.scalars():
                    await _process_movie(session, movie)
            else:
                logger.info("⏩ Skipping movie search in this cycle.")

            if active_shows_provider_ids:
                # 3. Process Shows (Seasons)
                season_stmt = (
                    select(Season)
                    .join(MediaItem)
                    .where(
                        Season.monitored == True,
                        Season.status.in_(
                            [SeasonStatus.SEARCHING, SeasonStatus.PENDING]
                        ),
                        MediaItem.status != MediaStatus.IGNORED,
                        MediaItem.provider_id.in_(active_shows_provider_ids),
                    )
                    .options(
                        selectinload(Season.media_item).selectinload(
                            MediaItem.provider
                        ),
                        selectinload(Season.download_history),
                    )
                )

                seasons_result = await session.execute(season_stmt)
                for season in seasons_result.scalars():
                    await _process_season(session, season)
            else:
                logger.info("⏩ Skipping series search in this cycle.")

        # 4. Download-Check: update status for items already sent to TorBox
        await run_download_check_cycle()
    finally:
        log_process_end(logger, "Automation Cycle")


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

    results = []
    is_title_fallback = False
    if movie.imdb_id:
        results = await treasure_maps.search_movie(
            imdb_id=movie.imdb_id, category=cat_id
        )
    if not results and movie.tmdb_id:
        results = await treasure_maps.search_movie(
            tmdb_id=movie.tmdb_id, category=cat_id
        )
    if not results:
        logger.warning(
            "    ⚠️ No ID match for movie '%s', falling back to title search.",
            movie.title,
        )
        results = await treasure_maps.search_movie(title=movie.title, category=cat_id)
        is_title_fallback = True

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

    await _evaluate_and_download(
        session,
        results,
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

    stmt = (
        select(Episode)
        .where(
            Episode.season_id == season.id,
            Episode.status == EpisodeStatus.SEARCHING,
            Episode.monitored == True,
        )
        .order_by(Episode.episode_number)
        .options(
            selectinload(Episode.season).selectinload(Season.media_item),
            selectinload(Episode.download_history),
        )
    )
    episodes_res = await session.execute(stmt)
    missing_episodes = episodes_res.scalars().all()

    if not missing_episodes:
        logger.info("    ✅ No pending episodes for S%02d.", season.season_number)
        return

    logger.info("    📺 Suche %d ausstehende Episoden.", len(missing_episodes))

    async def _search_show_id_first(s: int, ep: str | None = None) -> tuple[list, bool]:
        """Search by TVDB, then TMDB, then title fallback. Returns (results, is_title_fallback)."""
        if tvdb_id:
            r = await treasure_maps.search_show(
                tvdb_id=tvdb_id, season=s, ep=ep, category=cat_id
            )
            if r:
                return r, False
        if tmdb_id:
            r = await treasure_maps.search_show(
                tmdb_id=tmdb_id, season=s, ep=ep, category=cat_id
            )
            if r:
                return r, False
        # Title fallback — warn operator
        logger.warning(
            "    ⚠️ No ID match for S%02d%s, falling back to title search.",
            s,
            f"E{ep}" if ep else "",
        )
        r = await treasure_maps.search_show(
            title=season.media_item.title, season=s, ep=ep, category=cat_id
        )
        return r, True

    if prefer_seasons:
        logger.info("    📦 Searching season pack for S%02d...", season.season_number)
        results, is_fallback = await _search_show_id_first(season.season_number)
        if results:
            grabbed = await _evaluate_and_download(
                session,
                results,
                season=season,
                target_episodes=missing_episodes,
                reject_words=reject_words,
                required_language=required_language,
                is_title_fallback=is_fallback,
                filters=filters,
            )
            await session.refresh(season)
            if grabbed or season.status in [
                SeasonStatus.DOWNLOADING,
                SeasonStatus.DOWNLOADED,
                SeasonStatus.COMPLETED,
                SeasonStatus.MANUAL_GRAB,
            ]:
                # Season Pack was successful, check auto-monitor for next season
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
        try:
            logger.info(
                "    📺 Suche Episode: S%02dE%02d",
                season.season_number,
                ep.episode_number,
            )
            ep_results, is_fallback = await _search_show_id_first(
                season.season_number, str(ep.episode_number)
            )

            # Additional fallback for Anime Absolute Episode Numbering
            if not ep_results and season.media_item.media_type == MediaType.ANIME:
                ep_title_search = f"{season.media_item.title} {ep.episode_number:02d}"
                abs_res = await treasure_maps.search_show(
                    title=ep_title_search, category=cat_id
                )
                ep_results = abs_res
                is_fallback = True

            await _evaluate_and_download(
                session,
                ep_results,
                episode=ep,
                reject_words=reject_words,
                required_language=required_language,
                is_title_fallback=is_fallback,
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
        if target.best_score is not None:
            from app.db.models import (
                BlacklistedRelease,
                EpisodeStatus,
                MediaStatus,
                SeasonStatus,
            )

            target.fail_count = 0
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
            target.last_error = "No indexer results found, keeping existing release (Status: COMPLETED)."
            logger.info("    ❌ %s", target.last_error)
        else:
            target.fail_count += 1
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
    target_score = cutoffs.get("target_score", 2500)
    upgrade_threshold = cutoffs.get("upgrade_threshold", 300)

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
        if target.best_score is not None:
            # We already have a downloaded release, but no upgrades (or even valid candidates) were found this time.
            # Revert to COMPLETE so we don't loop in SEARCHING forever.
            from app.db.models import (
                BlacklistedRelease,
                EpisodeStatus,
                MediaStatus,
                SeasonStatus,
            )

            target.fail_count = 0
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
                "Search yielded no hits, keeping existing release (Status: COMPLETED)."
            )
            logger.info("    ❌ %s", target.last_error)
        else:
            target.fail_count += 1
            target.empty_search_count += 1
            target.last_error = f"Empty search count: {target.empty_search_count}"
            logger.info(
                "    ❌ No matching releases found (Empty Search Count: %d)",
                target.empty_search_count,
            )

        await session.commit()
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

    best_candidate = candidates[0]

    if not best_candidate:
        return False

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
        target.fail_count = 0
        target.last_error = f"Bestes Release (Score {best_candidate['score']}) liegt unter dem Upgrade-Schwellenwert."
        logger.info("    ❌ %s", target.last_error)

        # If we didn't find an upgrade, but we already have a download (since should_download is False),
        # we must set the status back to COMPLETE so it doesn't stay in SEARCHING forever.
        from app.db.models import (
            BlacklistedRelease,
            EpisodeStatus,
            MediaStatus,
            SeasonStatus,
        )

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

        await session.commit()
        return False

    from app.config import settings

    if should_download and scoring_config.get("automation", {}).get(
        "auto_send_to_torbox", True
    ):
        # Check hard daily grab limit (400 grabs/day)
        if not await can_grab_today(session, limit=400):
            target.fail_count += 1
            target.last_error = (
                "⚠️ Daily grab limit of 400 NZB downloads reached. Skipping grab."
            )
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
                best_candidate["guid"]
            )
            torbox_result = await torbox.send_nzb_file(nzb_bytes, filename=filename)
        except treasure_maps.IndexerError as e:
            target.fail_count += 1
            target.last_error = f"NZB download failed: {e}"
            logger.error("    ❌ %s", target.last_error)
            await session.commit()
            return False

        if not torbox_result or (
            not torbox_result.get("hash") and not torbox_result.get("id")
        ):
            target.fail_count += 1
            target.last_error = (
                torbox_result.get("error")
                if isinstance(torbox_result, dict) and torbox_result.get("error")
                else "Error sending to TorBox."
            )
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
            target.last_error = None

            # If target_episodes is passed (e.g. season pack downloaded), update all of them
            if target_episodes:
                for ep in target_episodes:
                    ep.status = new_status
                    ep.fail_count = 0
                    ep.last_error = None

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
        session, results, movie=media_item, reject_words=reject_words
    )

    await session.refresh(media_item)
    return media_item.status in [MediaStatus.DOWNLOADED, MediaStatus.COMPLETED]


async def process_print_book(session: AsyncSession, book: BookItem) -> bool:
    """Process an individual BookItem: search indexers, evaluate, score, and dispatch to TorBox."""
    from datetime import datetime, timezone

    from app.config import settings
    from app.core.fake_detector import is_indexer_metadata_fake, is_nzb_content_fake
    from app.core.reading_scorer import detect_print_format, score_print_release
    from app.db.grab_tracker import can_grab_today, increment_today_grab_count
    from app.db.models import MediaStatus
    from app.services import torbox, treasure_maps

    logger.info("  📖 Searching indexers for Book: %s", book.title)

    # Categories 7000 (General Books), 7020 (E-Books)
    cat_ids = [7000, 7020]
    queries = []
    if book.author:
        queries.append(f"{book.title} {book.author}".strip())
    queries.append(book.title.strip())

    results: list[dict] = []
    seen_guids: set[str] = set()

    for q in queries:
        for cid in cat_ids:
            try:
                raw_res = await treasure_maps.search_raw(query=q, category=cid)
                for r in raw_res:
                    guid = r.get("guid") or r.get("link", "")
                    if guid and guid not in seen_guids:
                        seen_guids.add(guid)
                        results.append(r)
            except Exception as e:
                logger.warning("  ⚠️ Indexer search failed for query '%s': %s", q, e)

    if not results:
        book.empty_search_count += 1
        book.last_searched_at = datetime.now(timezone.utc)
        book.last_error = "No results found on indexer."
        logger.info("  ℹ️ No results found for Book: %s", book.title)
        await session.commit()
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
        book.empty_search_count += 1
        book.last_searched_at = datetime.now(timezone.utc)
        book.last_error = (
            "All results were excluded by fake detection or format blacklists."
        )
        await session.commit()
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

    if not await can_grab_today(session, limit=400):
        book.last_error = "Daily grab limit of 400 reached."
        logger.warning(
            "  ⚠️ Daily grab limit reached. Skipping grab for Book '%s'.", book.title
        )
        await session.commit()
        return False

    try:
        nzb_bytes, filename = await treasure_maps.fetch_nzb_bytes(
            best_candidate["guid"]
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
            nzb_bytes, filename=filename or f"{book.title}.nzb"
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

    wanted_vols = [
        v
        for v in manga.volumes
        if v.status in [EpisodeStatus.PENDING, EpisodeStatus.SEARCHING]
    ]
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
    seen_guids: set[str] = set()

    for pq in pack_queries:
        for cid in cat_ids:
            try:
                raw_res = await treasure_maps.search_raw(query=pq, category=cid)
                for r in raw_res:
                    guid = r.get("guid") or r.get("link", "")
                    if guid and guid not in seen_guids:
                        seen_guids.add(guid)
                        pack_results.append(r)
            except Exception as e:
                logger.warning(
                    "  ⚠️ Indexer pack search failed for query '%s': %s", pq, e
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
                nzb_bytes, filename = await treasure_maps.fetch_nzb_bytes(
                    best_pack["guid"]
                )
                is_fake_content, _ = is_nzb_content_fake(nzb_bytes, media_type="manga")
                if not is_fake_content:
                    torbox_res = await torbox.send_nzb_file(
                        nzb_bytes, filename=filename or f"{manga.title}_Pack.nzb"
                    )
                    if torbox_res and (torbox_res.get("hash") or torbox_res.get("id")):
                        await increment_today_grab_count(session)

                        # CRITICAL: Suppress individual searches by marking all covered volumes DOWNLOADING
                        for vol in manga.volumes:
                            if vol.volume_number in best_pack_covered_vols:
                                vol.status = EpisodeStatus.DOWNLOADING
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
        n = vol.volume_number
        vol_queries = [
            f"{manga.title} v{n:02d}",
            f"{manga.title} vol {n}",
            f"{manga.title} Volume {n}",
            f"{manga.title} {n:02d}",
        ]

        vol_results: list[dict] = []
        vol_seen_guids: set[str] = set()

        for vq in vol_queries:
            for cid in cat_ids:
                try:
                    raw_res = await treasure_maps.search_raw(query=vq, category=cid)
                    for r in raw_res:
                        guid = r.get("guid") or r.get("link", "")
                        if guid and guid not in vol_seen_guids:
                            vol_seen_guids.add(guid)
                            vol_results.append(r)
                except Exception as e:
                    logger.warning("  ⚠️ Volume search failed for query '%s': %s", vq, e)

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
            vol.empty_search_count += 1
            continue

        candidates.sort(key=lambda c: c["score"], reverse=True)
        best_vol_cand = candidates[0]

        if not await can_grab_today(session, limit=400):
            logger.warning("  ⚠️ Daily grab limit reached. Stopping volume grabs.")
            break

        try:
            nzb_bytes, filename = await treasure_maps.fetch_nzb_bytes(
                best_vol_cand["guid"]
            )
            is_fake_content, _ = is_nzb_content_fake(nzb_bytes, media_type="manga")
            if is_fake_content:
                continue

            torbox_res = await torbox.send_nzb_file(
                nzb_bytes, filename=filename or f"{manga.title}_Vol_{n}.nzb"
            )
            if torbox_res and (torbox_res.get("hash") or torbox_res.get("id")):
                await increment_today_grab_count(session)
                vol.status = EpisodeStatus.DOWNLOADING
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
    logger.info("=== Start Print Media Automation Cycle ===")

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.database import async_session_factory
    from app.db.models import BookItem, MangaItem, MediaStatus

    async with async_session_factory() as session:
        # 1. Process Books in SEARCHING / PENDING state
        stmt_books = select(BookItem).where(
            BookItem.status.in_([MediaStatus.PENDING, MediaStatus.SEARCHING])
        )
        books = (await session.execute(stmt_books)).scalars().all()
        for book in books:
            try:
                await process_print_book(session, book)
            except Exception as e:
                logger.error("Error processing Book '%s': %s", book.title, e)

        # 2. Process Manga with wanted volumes
        stmt_manga = (
            select(MangaItem)
            .options(selectinload(MangaItem.volumes))
            .where(
                MangaItem.status.in_(
                    [
                        MediaStatus.PENDING,
                        MediaStatus.SEARCHING,
                        MediaStatus.DOWNLOADING,
                    ]
                )
            )
        )
        mangas = (await session.execute(stmt_manga)).scalars().all()
        for manga in mangas:
            try:
                await process_print_manga(session, manga)
            except Exception as e:
                logger.error("Error processing Manga '%s': %s", manga.title, e)

    logger.info("=== End Print Media Automation Cycle ===")
