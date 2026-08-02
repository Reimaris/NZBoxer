"""
Automation Orchestrator
=======================
Handles syncing the Simkl watchlist to the local database, searching
Newznab indexers, evaluating scores, and sending releases to TorBox.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import scoring_config
from app.core.parser import parse_release_name
from app.core.scorer import score_release
from app.db.database import async_session_factory
from app.db.models import (
    DownloadHistory,
    MediaItem,
    MediaStatus,
    MediaType,
    Season,
    SeasonStatus,
)
from app.services import simkl, tmdb, torbox, treasure_maps

logger = logging.getLogger(__name__)


async def sync_simkl_watchlist() -> None:
    """Sync the Simkl 'plan to watch' list to the local SQLite database."""
    logger.info("Starting Simkl watchlist sync...")
    async with async_session_factory() as session:
        try:
            movies = await simkl.get_watchlist("movies")
            shows = await simkl.get_watchlist("shows")
            
            await _sync_items(session, movies, MediaType.MOVIE)
            await _sync_items(session, shows, MediaType.SHOW)
            
            await session.commit()
            logger.info("Simkl watchlist sync completed successfully.")
        except Exception as e:  # noqa: BLE001
            logger.error("Simkl sync failed: %s", e)
            await session.rollback()


async def _sync_items(session: AsyncSession, simkl_items: list[dict[str, Any]], media_type: MediaType) -> None:
    for item_data in simkl_items:
        movie_data = item_data.get("movie") or item_data.get("show")
        if not movie_data:
            continue

        simkl_id = movie_data.get("ids", {}).get("simkl")
        if not simkl_id:
            continue

        # Check if item exists
        stmt = select(MediaItem).where(MediaItem.simkl_id == simkl_id).options(selectinload(MediaItem.seasons))
        result = await session.execute(stmt)
        item = result.scalar_one_or_none()

        ids = movie_data.get("ids", {})
        tmdb_id_str = ids.get("tmdb")
        tmdb_id = int(tmdb_id_str) if tmdb_id_str else None

        if not item:
            item = MediaItem(
                simkl_id=simkl_id,
                imdb_id=ids.get("imdb"),
                tmdb_id=tmdb_id,
                title=movie_data.get("title"),
                year=movie_data.get("year"),
                media_type=media_type,
                poster_url=f"https://simkl.in/posters/{movie_data.get('poster')}_m.webp" if movie_data.get("poster") else None,
                status=MediaStatus.PENDING,
            )
            session.add(item)
            # Need to flush to get item.id for seasons, though sqlalchemy 2.0 does it automatically on commit/flush
            await session.flush()
            
            # Fetch metadata from TMDB if available
            if tmdb_id:
                if media_type == MediaType.MOVIE:
                    release_date = await tmdb.get_digital_release_date(tmdb_id)
                    item.release_date = release_date
                    if not release_date or release_date <= datetime.now(timezone.utc):
                        item.status = MediaStatus.SEARCHING
                
                elif media_type == MediaType.SHOW:
                    details = await tmdb.get_show_details(tmdb_id)
                    if details:
                        item.overview = details.get("overview")
                        for s in details.get("seasons", []):
                            s_num = s.get("season_number")
                            if s_num is not None and s_num > 0: # Skip specials (season 0) by default
                                season_obj = Season(
                                    media_item_id=item.id,
                                    season_number=s_num,
                                    monitored=(s_num == 1), # Default: monitor only season 1
                                    episode_count=s.get("episode_count"),
                                    status=SeasonStatus.SEARCHING if (s_num == 1) else SeasonStatus.PENDING
                                )
                                session.add(season_obj)
                        
        else:
            # Update existing
            item.title = movie_data.get("title", item.title)
            # We don't overwrite user-modified statuses here
        
        item.simkl_synced_at = datetime.now(timezone.utc)


async def run_automation_cycle() -> None:
    """Background job that searches for NZBs and pushes them to TorBox."""
    logger.info("Starting automation cycle...")
    
    # 1. Sync watchlist first
    await sync_simkl_watchlist()

    async with async_session_factory() as session:
        # Update pending movies that have reached their release date
        stmt = select(MediaItem).where(
            MediaItem.status == MediaStatus.PENDING,
            MediaItem.media_type == MediaType.MOVIE,
            MediaItem.release_date <= datetime.now(timezone.utc)
        )
        result = await session.execute(stmt)
        for pending_item in result.scalars():
            pending_item.status = MediaStatus.SEARCHING
        
        await session.commit()

        # 2. Process Movies
        stmt = select(MediaItem).where(
            MediaItem.status == MediaStatus.SEARCHING,
            MediaItem.media_type == MediaType.MOVIE
        ).options(selectinload(MediaItem.download_history))
        
        movies_result = await session.execute(stmt)
        for movie in movies_result.scalars():
            await _process_movie(session, movie)

        # 3. Process Shows (Seasons)
        season_stmt = select(Season).join(MediaItem).where(
            Season.monitored == True,
            Season.status.in_([SeasonStatus.SEARCHING, SeasonStatus.PENDING])
        ).options(selectinload(Season.media_item), selectinload(Season.download_history))
        
        seasons_result = await session.execute(season_stmt)
        for season in seasons_result.scalars():
            await _process_season(session, season)


async def _process_movie(session: AsyncSession, movie: MediaItem) -> None:
    logger.info("Processing movie: %s", movie.title)
    if not movie.imdb_id:
        logger.warning("Movie %s lacks IMDb ID, skipping search.", movie.title)
        return

    results = await treasure_maps.search_movie(movie.imdb_id)
    await _evaluate_and_download(session, results, movie=movie)


async def _process_season(session: AsyncSession, season: Season) -> None:
    logger.info("Processing season: %s S%02d", season.media_item.title, season.season_number)
    tvdb_id = None # We rely on q=Title + season if tvdb_id is absent.
    # Newznab indexers usually have good matching for title + season
    results = await treasure_maps.search_show(tvdb_id, season.media_item.title, season.season_number)
    await _evaluate_and_download(session, results, season=season)


async def _evaluate_and_download(session: AsyncSession, search_results: list[dict[str, Any]], movie: MediaItem | None = None, season: Season | None = None) -> None:
    best_candidate = None
    highest_score: float = -9999.0

    if not search_results:
        return

    if movie is None and season is None:
        raise ValueError("Must provide either a movie or a season")
    
    target: Any = movie if movie else season
    current_best_score = target.best_score or 0.0
    
    cutoffs = scoring_config.get("cutoffs", {})
    target_score = cutoffs.get("target_score", 2500)
    upgrade_threshold = cutoffs.get("upgrade_threshold", 300)

    for item in search_results:
        title = item.get("title", "")
        size_bytes = int(item.get("size", 0))
        guid = item.get("guid", "")
        
        # Calculate age if pubDate is available. Skipping for brevity, assuming 0.
        
        parsed = parse_release_name(title)
        runtime = movie.runtime_minutes if movie else None
        
        score_res = score_release(parsed, size_bytes, runtime)
        
        if score_res.is_rejected:
            continue
            
        if score_res.score > highest_score:
            highest_score = score_res.score
            best_candidate = {
                "title": title,
                "guid": guid,
                "size_bytes": size_bytes,
                "parsed": parsed,
                "score_res": score_res
            }

    if not best_candidate:
        return

    # Check if we should download
    should_download = False

    if target.best_score is None or highest_score >= (current_best_score + upgrade_threshold):
        should_download = True

    if should_download and scoring_config.get("automation", {}).get("auto_send_to_torbox", True):
        title_for_log = target.media_item.title if isinstance(target, Season) else target.title
        logger.info("Downloading %s (Score: %s) for %s", 
                    best_candidate["title"], highest_score, title_for_log)
        
        download_url = await treasure_maps.get_download_url(best_candidate["guid"])
        torbox_hash = await torbox.send_nzb_link(download_url)
        
        if torbox_hash:
            history = DownloadHistory(
                media_item_id=movie.id if movie else target.media_item_id,
                season_id=season.id if season else None,
                nzb_title=best_candidate["title"],
                nzb_guid=best_candidate["guid"],
                score=highest_score,
                size_bytes=best_candidate["size_bytes"],
                resolution=best_candidate["parsed"].resolution,
                video_codec=best_candidate["parsed"].video_codec,
                audio_codec=best_candidate["parsed"].audio_codec,
                source=best_candidate["parsed"].source,
                release_group=best_candidate["parsed"].release_group,
                bitrate_mbps=best_candidate["score_res"].bitrate_mbps,
                torbox_hash=torbox_hash,
                torbox_sent_at=datetime.now(timezone.utc)
            )
            session.add(history)
            
            target.status = MediaStatus.DOWNLOADED if isinstance(target, MediaItem) else SeasonStatus.DOWNLOADED
            if highest_score >= target_score:
                target.status = MediaStatus.COMPLETED if isinstance(target, MediaItem) else SeasonStatus.COMPLETED
                logger.info("Target score reached for %s. Automation complete.", title_for_log)
                
            await session.commit()
