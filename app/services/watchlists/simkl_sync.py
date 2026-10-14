"""Simkl watchlist sync pipeline (m59-32).

The Simkl adapter owns the anime/franchise-aware ingest, air-date enrichment and
absent-source bookkeeping that the generic ``ingest_entries`` path does not cover.
Helpers shared with the rest of the orchestrator are reached through the
``app.core.automation`` namespace (lazily, to avoid an import cycle).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.phase_stats import AirDateRefreshStats, IngestStats
from app.db.database import async_session_factory
from app.db.models import (
    MediaItem,
    MediaItemWatchlistSource,
    MediaStatus,
    MediaType,
    Provider,
    Season,
    SeasonStatus,
)
from app.services.watchlist_ingest import IngestResult, _upsert_source
from app.services.watchlist_providers import WatchlistEntry
from app.services.watchlists import simkl

# Pipeline phase logs keep the orchestrator logger name (log filters, tests).
logger = logging.getLogger("app.core.automation")


async def upsert_simkl_source(
    session: AsyncSession,
    item: MediaItem,
    provider: Provider,
    simkl_id: int,
    list_status: str,
) -> MediaItemWatchlistSource:
    """Upsert the source row linking *item* to a Simkl provider entry."""
    entry = WatchlistEntry(
        remote_id=str(simkl_id),
        media_kind="movie" if item.media_type == MediaType.MOVIE else "show",
        title=item.title or "",
        list_status=list_status if list_status else "plantowatch",  # type: ignore[arg-type]
    )
    source = await _upsert_source(session, item, provider, entry, 0, IngestResult())
    source.last_synced_at = datetime.now(timezone.utc)
    return source


async def _simkl_seen_remote_ids(
    session: AsyncSession,
    provider_id: int,
    active_simkl_ids: set[int],
    *,
    sync_movies: bool,
    sync_series: bool,
    sync_anime: bool,
) -> set[str]:
    """Remote ids still present on the Simkl list; disabled categories count as seen."""

    seen = {str(i) for i in active_simkl_ids}
    disabled = [
        mt
        for mt, enabled in (
            (MediaType.MOVIE, sync_movies),
            (MediaType.SHOW, sync_series),
            (MediaType.ANIME, sync_anime),
        )
        if not enabled
    ]
    if disabled:
        rows = await session.execute(
            select(MediaItemWatchlistSource.remote_id)
            .join(MediaItem, MediaItem.id == MediaItemWatchlistSource.media_item_id)
            .where(
                MediaItemWatchlistSource.provider_id == provider_id,
                MediaItem.media_type.in_(disabled),
            )
        )
        seen.update(rows.scalars().all())
    return seen


async def _upsert_simkl_source_for_item(
    session: AsyncSession,
    item: MediaItem,
    provider_id: int,
    simkl_id: int,
    item_data: dict[str, Any],
) -> None:
    """Record the Simkl list membership of *item* as a watchlist source row."""

    provider = await session.get(Provider, provider_id)
    if provider is None:
        return
    status = str(item_data.get("status") or "plantowatch").lower()
    await upsert_simkl_source(session, item, provider, simkl_id, status)


async def _sync_items(
    session: AsyncSession,
    simkl_items: list[dict[str, Any]],
    media_type: MediaType,
    provider_id: int,
    provider_name: str,
    *,
    stats: IngestStats | None = None,
) -> None:
    from sqlalchemy import and_, or_

    from app.core.automation import (
        _backfill_item_release_date_from_seasons,
        enrich_anime_metadata,
        enrich_movie_show_metadata,
        is_future_or_tba,
    )

    category_label = (
        "movies"
        if media_type == MediaType.MOVIE
        else ("shows" if media_type == MediaType.SHOW else "anime")
    )
    logger.debug(
        "[SYNC] [2/7] Ingesting %d %s from Simkl provider '%s'...",
        len(simkl_items),
        category_label,
        provider_name,
    )
    if stats is not None:
        stats.ingested += len(simkl_items)

    mal_to_anilist: dict[int, int] = {}
    if media_type == MediaType.ANIME and simkl_items:
        # Pre-load existing known Simkl IDs, MAL IDs, and AniList IDs across MediaItems and Seasons
        known_simkl_ids: set[int] = set()
        known_mal_ids: set[int] = set()
        existing_items_res = await session.execute(
            select(MediaItem).options(selectinload(MediaItem.seasons))
        )
        for existing_mi in existing_items_res.scalars().unique().all():
            if existing_mi.simkl_id:
                known_simkl_ids.add(int(existing_mi.simkl_id))
            known_simkl_ids.update(existing_mi.associated_franchise_ids)
            if existing_mi.mal_id:
                known_mal_ids.add(int(existing_mi.mal_id))
            for existing_s in existing_mi.seasons or []:
                if existing_s.simkl_id:
                    known_simkl_ids.add(int(existing_s.simkl_id))
                if existing_s.mal_id:
                    known_mal_ids.add(int(existing_s.mal_id))

        unmatched_mal_ids: list[int] = []
        for entry in simkl_items:
            m_data = entry.get("anime") or entry.get("show") or entry.get("movie")
            if not isinstance(m_data, dict):
                continue
            entry_ids = m_data.get("ids") or {}
            raw_sid = entry_ids.get("simkl")
            raw_mal = entry_ids.get("mal")
            raw_ani = entry_ids.get("anilist")
            if not raw_sid or raw_ani:
                continue
            try:
                sid_val = int(raw_sid)
                mal_val = int(raw_mal) if raw_mal else None
            except (TypeError, ValueError):
                continue
            if sid_val in known_simkl_ids:
                continue
            if mal_val and mal_val not in known_mal_ids:
                unmatched_mal_ids.append(mal_val)

        if unmatched_mal_ids:
            from app.services import anilist

            await session.commit()
            mal_to_anilist = await anilist.get_anime_ids_by_mal_ids(unmatched_mal_ids)

    total_simkl_items = len(simkl_items)
    for item_idx, item_data in enumerate(simkl_items, start=1):
        movie_data = (
            item_data.get("movie") or item_data.get("show") or item_data.get("anime")
        )
        if not movie_data:
            continue

        simkl_id = movie_data.get("ids", {}).get("simkl")
        if not simkl_id:
            continue
        simkl_id = int(simkl_id)

        # Check if item exists
        ids = movie_data.get("ids", {})

        tmdb_id_str = ids.get("tmdb")
        tmdb_id = int(tmdb_id_str) if tmdb_id_str else None

        tvdb_id_str = ids.get("tvdb")
        tvdb_id = int(tvdb_id_str) if tvdb_id_str else None

        mal_id_str = ids.get("mal")
        mal_id = int(mal_id_str) if mal_id_str else None

        anilist_id_str = ids.get("anilist")
        anilist_id = int(anilist_id_str) if anilist_id_str else None
        if not anilist_id and mal_id and mal_id in mal_to_anilist:
            anilist_id = mal_to_anilist[mal_id]

        imdb_id = ids.get("imdb")

        item_conditions = [MediaItem.simkl_id == simkl_id]
        if anilist_id:
            item_conditions.append(
                and_(
                    MediaItem.anilist_id.isnot(None), MediaItem.anilist_id == anilist_id
                )
            )
        if media_type == MediaType.ANIME and mal_id:
            item_conditions.append(
                and_(
                    MediaItem.media_type == MediaType.ANIME,
                    MediaItem.mal_id.isnot(None),
                    MediaItem.mal_id == mal_id,
                )
            )
        if media_type in (MediaType.MOVIE, MediaType.SHOW):
            if tmdb_id:
                item_conditions.append(
                    and_(
                        MediaItem.media_type == media_type,
                        MediaItem.tmdb_id.isnot(None),
                        MediaItem.tmdb_id == tmdb_id,
                    )
                )
            if imdb_id:
                item_conditions.append(
                    and_(
                        MediaItem.media_type == media_type,
                        MediaItem.imdb_id.isnot(None),
                        MediaItem.imdb_id == imdb_id,
                    )
                )

        stmt = (
            select(MediaItem)
            .where(or_(*item_conditions))
            .order_by(MediaItem.id.asc())
            .options(selectinload(MediaItem.seasons))
        )
        result = await session.execute(stmt)
        item = result.scalars().first()

        # Also check associated_franchise_ids for merged multi-cour/bridge anime entries
        if not item and media_type == MediaType.ANIME:
            assoc_stmt = (
                select(MediaItem)
                .where(
                    MediaItem.media_type == MediaType.ANIME,
                    MediaItem.associated_franchise_ids_json != "[]",
                )
                .order_by(MediaItem.id.asc())
                .options(selectinload(MediaItem.seasons))
            )
            for cand_item in (
                (await session.execute(assoc_stmt)).scalars().unique().all()
            ):
                if simkl_id in cand_item.associated_franchise_ids:
                    item = cand_item
                    break

        # Check if incoming item is actually a consolidated season
        season_conditions = [Season.simkl_id == simkl_id]
        if anilist_id:
            season_conditions.append(
                and_(Season.anilist_id.isnot(None), Season.anilist_id == anilist_id)
            )
        if mal_id:
            season_conditions.append(
                and_(Season.mal_id.isnot(None), Season.mal_id == mal_id)
            )

        season_stmt = (
            select(Season)
            .where(or_(*season_conditions))
            .options(selectinload(Season.media_item))
        )
        existing_season = (await session.execute(season_stmt)).scalars().first()

        # Pre-creation fallback for Anime: if still unmatched and lacking anilist_id, resolve anilist_id
        # BEFORE inserting a new MediaItem so we can attach to an existing Season or MediaItem in place.
        if (
            not item
            and not existing_season
            and media_type == MediaType.ANIME
            and not anilist_id
        ):
            from app.services import anilist

            await session.commit()
            if mal_id and mal_id not in unmatched_mal_ids:
                anilist_id = await anilist.get_anime_id_by_mal_id(mal_id)
            if not anilist_id and movie_data.get("title"):
                anilist_id = await anilist.search_anime_id_by_title(
                    str(movie_data.get("title")), movie_data.get("year")
                )
            if anilist_id:
                existing_season = (
                    (
                        await session.execute(
                            select(Season)
                            .where(Season.anilist_id == anilist_id)
                            .options(selectinload(Season.media_item))
                        )
                    )
                    .scalars()
                    .first()
                )
                if not existing_season:
                    item = (
                        (
                            await session.execute(
                                select(MediaItem)
                                .where(MediaItem.anilist_id == anilist_id)
                                .order_by(MediaItem.id.asc())
                                .options(selectinload(MediaItem.seasons))
                            )
                        )
                        .scalars()
                        .first()
                    )

        if existing_season:
            parent_item = existing_season.media_item
            if parent_item:
                parent_item.add_associated_franchise_id(simkl_id)
                if not parent_item.tmdb_id and tmdb_id:
                    parent_item.tmdb_id = tmdb_id
                if not parent_item.imdb_id and ids.get("imdb"):
                    parent_item.imdb_id = ids.get("imdb")
                if not parent_item.tvdb_id and tvdb_id:
                    parent_item.tvdb_id = tvdb_id
                if not parent_item.mal_id and mal_id:
                    parent_item.mal_id = mal_id
                if parent_item.status == MediaStatus.IGNORED:
                    parent_item.status = (
                        MediaStatus.FUTURE
                        if is_future_or_tba(parent_item.release_date, parent_item.year)
                        else MediaStatus.SEARCHING
                    )
                    logger.info(
                        "    🔄 Restored previously ignored parent franchise '%s' (ID %d) because Season %d is active on watchlist.",
                        parent_item.title,
                        parent_item.id,
                        existing_season.season_number,
                    )

            if (
                item
                and item.id != existing_season.media_item_id
                and item.status != MediaStatus.IGNORED
            ):
                logger.info(
                    "    ⏩ Soft-ignoring standalone sequel MediaItem '%s' (ID %d) as it is consolidated under parent series ID %d Season %d.",
                    item.title,
                    item.id,
                    existing_season.media_item_id,
                    existing_season.season_number,
                )
                item.status = MediaStatus.IGNORED
            elif not item:
                logger.debug(
                    "    ⏩ Item '%s' is already tracked as Season %d of a consolidated parent series. Skipping standalone creation.",
                    movie_data.get("title"),
                    existing_season.season_number,
                )
            if not existing_season.simkl_id:
                existing_season.simkl_id = simkl_id
            if anilist_id and not existing_season.anilist_id:
                existing_season.anilist_id = anilist_id
            if mal_id and not existing_season.mal_id:
                existing_season.mal_id = mal_id

            existing_season.monitored = True
            if existing_season.status == SeasonStatus.PENDING:
                existing_season.status = SeasonStatus.SEARCHING
            from app.db.models import Episode, EpisodeStatus

            ep_stmt = select(Episode).where(Episode.season_id == existing_season.id)
            eps = (await session.execute(ep_stmt)).scalars().all()
            for ep in eps:
                ep.monitored = True
                if ep.status == EpisodeStatus.PENDING:
                    ep.status = EpisodeStatus.SEARCHING
            if parent_item is not None:
                from app.core.push_engine import (
                    evaluate_watch_progress_and_advance,
                )

                parent_item.watchlist_synced_at = datetime.now(timezone.utc)
                await _upsert_simkl_source_for_item(
                    session, parent_item, provider_id, simkl_id, item_data
                )
                await evaluate_watch_progress_and_advance(
                    session=session,
                    item=parent_item,
                    item_data=item_data,
                    matched_season=existing_season,
                )
                await session.commit()
            continue
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
            item.add_associated_franchise_id(simkl_id)
            session.add(item)
            if stats is not None:
                stats.added += 1
            # Commit (not just flush) to get item.id AND release the SQLite write lock
            # before any slow outbound AniList/TMDB HTTP calls that follow.
            await session.commit()
            is_new = True
        else:
            is_new = False
            # PRESERVE existing DB media_type for sync logic to prevent watchlist sync from overwriting user classifications
            media_type = item.media_type
            item.add_associated_franchise_id(simkl_id)
            if not item.tmdb_id and tmdb_id:
                item.tmdb_id = tmdb_id
            if not item.imdb_id and ids.get("imdb"):
                item.imdb_id = ids.get("imdb")
            if not item.tvdb_id and tvdb_id:
                item.tvdb_id = tvdb_id
            if not item.mal_id and mal_id:
                item.mal_id = mal_id
            if not item.anilist_id and anilist_id:
                item.anilist_id = anilist_id

        # Fetch metadata from TMDB/AniList if available (for new items, items missing release date, or items missing seasons)
        if media_type == MediaType.ANIME or getattr(item, "is_anime_movie", False):
            needs_initial_enrichment = is_new or (
                item.last_metadata_refreshed_at is None
                and (media_type != MediaType.ANIME or not item.seasons)
            )
            if not item.anilist_id and needs_initial_enrichment:
                from app.services import anilist

                await session.commit()
                if item.mal_id:
                    item.anilist_id = await anilist.get_anime_id_by_mal_id(item.mal_id)
                if not item.anilist_id:
                    item.anilist_id = await anilist.search_anime_id_by_title(
                        item.title, item.year
                    )
                if not item.anilist_id:
                    item.last_metadata_refreshed_at = datetime.now(timezone.utc)
            if item.anilist_id and media_type == MediaType.ANIME:
                # Check if item.anilist_id matches an existing Season on another active MediaItem
                dup_stmt = (
                    select(Season)
                    .join(MediaItem, Season.media_item_id == MediaItem.id)
                    .where(
                        Season.anilist_id == item.anilist_id,
                        Season.media_item_id != item.id,
                        MediaItem.status != MediaStatus.IGNORED,
                    )
                    .options(selectinload(Season.media_item))
                )
                parent_season = (await session.execute(dup_stmt)).scalars().first()
                if parent_season:
                    if item.status != MediaStatus.IGNORED:
                        logger.info(
                            "    ⏩ Soft-ignoring MediaItem '%s' (ID %d) as AniList ID %d is tracked as Season %d of parent series ID %d.",
                            item.title,
                            item.id,
                            item.anilist_id,
                            parent_season.season_number,
                            parent_season.media_item_id,
                        )
                    item.status = MediaStatus.IGNORED
                    for s in item.seasons:
                        s.status = SeasonStatus.IGNORED
                    if parent_season.media_item and item.simkl_id:
                        parent_season.media_item.add_associated_franchise_id(
                            item.simkl_id
                        )
                    if not parent_season.simkl_id and item.simkl_id:
                        parent_season.simkl_id = item.simkl_id
                    if not parent_season.mal_id and item.mal_id:
                        parent_season.mal_id = item.mal_id
                    if not parent_season.monitored:
                        parent_season.monitored = True
                        if parent_season.status != SeasonStatus.FUTURE:
                            parent_season.status = SeasonStatus.SEARCHING
                elif needs_initial_enrichment:
                    # Commit any pending dirty state before slow AniList API calls
                    await session.commit()
                    await enrich_anime_metadata(session, item)
                else:
                    logger.debug(
                        "    ⏩ Skipping AniList hierarchy enrichment for existing anime '%s' with established seasons.",
                        item.title,
                    )
                    if item.status in (
                        MediaStatus.PENDING,
                        MediaStatus.SEARCHING,
                        MediaStatus.FUTURE,
                    ):
                        item.status = (
                            MediaStatus.FUTURE
                            if is_future_or_tba(item.release_date, item.year)
                            else MediaStatus.SEARCHING
                        )
            elif item.status in (
                MediaStatus.PENDING,
                MediaStatus.SEARCHING,
                MediaStatus.FUTURE,
            ):
                item.status = (
                    MediaStatus.FUTURE
                    if is_future_or_tba(item.release_date, item.year)
                    else MediaStatus.SEARCHING
                )

        elif (tmdb_id or item.imdb_id) and (
            is_new
            or not item.release_date
            or (not item.seasons and item.media_type != MediaType.MOVIE)
        ):
            media_type = await enrich_movie_show_metadata(
                session, item, tmdb_id, media_type
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
                if (
                    not item.anilist_id
                    and item.last_metadata_refreshed_at is None
                    and (
                        media_type == MediaType.ANIME
                        or getattr(item, "is_anime_movie", False)
                    )
                ):
                    from app.services import anilist

                    if item.mal_id:
                        item.anilist_id = await anilist.get_anime_id_by_mal_id(
                            item.mal_id
                        )
                    if not item.anilist_id:
                        item.anilist_id = await anilist.search_anime_id_by_title(
                            item.title, item.year
                        )
                    item.last_metadata_refreshed_at = datetime.now(timezone.utc)
            # Ensure provider matches
            item.provider_id = provider_id

            # Sync status based on release date / year for existing items
            if item.status in (
                MediaStatus.PENDING,
                MediaStatus.SEARCHING,
                MediaStatus.FUTURE,
            ):
                if is_future_or_tba(item.release_date, item.year):
                    item.status = MediaStatus.FUTURE
                else:
                    item.status = MediaStatus.SEARCHING

        from sqlalchemy import inspect as sa_inspect

        item_state = sa_inspect(item)
        if item_state.deleted or item_state.was_deleted:
            await session.commit()
            continue

        _backfill_item_release_date_from_seasons(item)
        item.watchlist_synced_at = datetime.now(timezone.utc)
        await _upsert_simkl_source_for_item(
            session, item, provider_id, simkl_id, item_data
        )
        from app.core.push_engine import evaluate_watch_progress_and_advance

        await evaluate_watch_progress_and_advance(
            session=session,
            item=item,
            item_data=item_data,
            matched_season=None,
        )
        logger.debug(
            "[SYNC] [2/7] (%d/%d) Processed %s '%s' (%s) -> Status: %s",
            item_idx,
            total_simkl_items,
            item.media_type.value
            if hasattr(item.media_type, "value")
            else str(item.media_type),
            item.title,
            str(item.simkl_id),
            item.status.value if hasattr(item.status, "value") else str(item.status),
        )
        await session.commit()


async def enrich_active_shows_episode_air_dates(
    session: AsyncSession,
    now: datetime | None = None,
    *,
    log_prefix: str = "[SYNC] [4/7]",
    stats: AirDateRefreshStats | None = None,
) -> int:
    """Update episode air dates for upcoming TV shows and anime from Simkl.

    Queries MediaItem records where:
    - media_type in (SHOW, ANIME)
    - status != IGNORED
    - simkl_id is not None
    - At least one season or episode has status FUTURE
    Items are then gated by ``is_air_date_refresh_due`` (m49 pacing policy) and
    stamped with ``last_air_dates_refreshed_at`` after every Simkl attempt.

    For each matching item, retrieves active Simkl provider credentials, calls
    simkl.get_episodes(item.simkl_id, client_id, media_type), matches returned
    episodes by (season, episode), updates Episode.air_date using parse_broadcast_air_date,
    and returns the total count of updated episodes (committing if updates occurred).
    """
    from app.core.automation import (
        AIR_DATE_REFRESH_INTERVAL,
        _as_utc,
        _refresh_future_movie_release_dates,
        is_air_date_refresh_due,
        is_episode_aired,
        parse_broadcast_air_date,
    )
    from app.core.transfer_poller import (
        _recalculate_parent_status,
        _sync_season_status_from_episodes,
    )
    from app.db.models import (
        Episode,
        EpisodeStatus,
        MediaItem,
        MediaStatus,
        MediaType,
        Season,
        SeasonStatus,
    )

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    stmt = (
        select(MediaItem)
        .where(
            MediaItem.media_type.in_((MediaType.SHOW, MediaType.ANIME)),
            MediaItem.status != MediaStatus.IGNORED,
            MediaItem.simkl_id.is_not(None),
            (
                MediaItem.seasons.any(Season.status == SeasonStatus.FUTURE)
                | MediaItem.seasons.any(
                    Season.episodes.any(Episode.status == EpisodeStatus.FUTURE)
                )
            ),
        )
        .options(
            selectinload(MediaItem.provider),
            selectinload(MediaItem.download_history),
            selectinload(MediaItem.seasons).selectinload(Season.episodes),
            selectinload(MediaItem.seasons).selectinload(Season.download_history),
        )
    )

    candidates = list((await session.execute(stmt)).scalars().unique().all())
    items: list[MediaItem] = []
    skipped_frozen = 0
    skipped_paced = 0
    for cand in candidates:
        if is_air_date_refresh_due(cand, now):
            items.append(cand)
        elif cand.last_air_dates_refreshed_at is not None and (
            now - _as_utc(cand.last_air_dates_refreshed_at) < AIR_DATE_REFRESH_INTERVAL
        ):
            skipped_paced += 1
        else:
            skipped_frozen += 1
    logger.debug(
        "%s Air date refresh: checked=%d skipped(frozen)=%d skipped(paced)=%d.",
        log_prefix,
        len(items),
        skipped_frozen,
        skipped_paced,
    )
    if stats is not None:
        stats.skipped_frozen = skipped_frozen
        stats.skipped_paced = skipped_paced
    movie_stmt = select(MediaItem).where(
        MediaItem.media_type == MediaType.MOVIE,
        MediaItem.status == MediaStatus.FUTURE,
    )
    movie_candidates = list((await session.execute(movie_stmt)).scalars().all())
    due_movies = [m for m in movie_candidates if is_air_date_refresh_due(m, now)]
    if due_movies:
        await _refresh_future_movie_release_dates(session, due_movies, now)
        await session.commit()

    if not items:
        return 0

    # Cache provider credentials across items: provider_id -> (client_id, access_token)
    provider_creds: dict[int | None, tuple[str, str] | None] = {}
    default_simkl_creds: tuple[str, str] | None = None
    default_creds_checked = False
    updated_episodes_count = 0
    titles_updated_count = 0

    total_items = len(items)
    for item_idx, item in enumerate(items, start=1):
        if not item.simkl_id:
            continue

        prov_id = item.provider_id
        if prov_id not in provider_creds:
            provider = item.provider
            if provider is None and prov_id is not None:
                provider = await session.get(Provider, prov_id)
            provider_creds[prov_id] = await simkl._resolve_simkl_creds(
                session, provider
            )

        creds = provider_creds.get(prov_id)
        if not creds:
            if not default_creds_checked:
                active_simkl_stmt = select(Provider).where(
                    Provider.type == "simkl",
                    Provider.is_active == True,  # noqa: E712
                )
                simkl_provs = list(
                    (await session.execute(active_simkl_stmt)).scalars().all()
                )
                for sp in simkl_provs:
                    c = await simkl._resolve_simkl_creds(session, sp)
                    if c:
                        default_simkl_creds = c
                        break
                default_creds_checked = True
            creds = default_simkl_creds

        if not creds:
            continue

        client_id, _ = creds
        media_type_str = "anime" if item.media_type == MediaType.ANIME else "tv"
        logger.debug(
            "%s (%d/%d) Querying Simkl episodes for '%s' (Simkl ID: %s)...",
            log_prefix,
            item_idx,
            total_items,
            item.title,
            str(item.simkl_id),
        )
        try:
            episodes_data = await simkl.get_episodes(
                simkl_id=int(item.simkl_id),
                client_id=client_id,
                media_type=media_type_str,
            )
        except Exception as e:
            logger.warning("Error fetching Simkl episodes for item %s: %s", item.id, e)
            item.last_air_dates_refreshed_at = now
            continue

        item.last_air_dates_refreshed_at = now
        if not episodes_data:
            continue

        # Map local episodes by (season_number, episode_number)
        local_episodes_map: dict[tuple[int, int], Episode] = {}
        for s in item.seasons or []:
            for ep in s.episodes or []:
                local_episodes_map[(s.season_number, ep.episode_number)] = ep

        item_episodes_updated = 0
        for ep_info in episodes_data:
            s_num = ep_info.get("season")
            e_num = ep_info.get("episode")
            date_raw = ep_info.get("date")
            if s_num is None or e_num is None or not date_raw:
                continue

            try:
                key = (int(s_num), int(e_num))
            except (ValueError, TypeError):
                continue

            local_ep = local_episodes_map.get(key)
            if local_ep is not None:
                parsed_dt = parse_broadcast_air_date(date_raw)
                if parsed_dt is not None and local_ep.air_date != parsed_dt:
                    local_ep.air_date = parsed_dt
                    item_episodes_updated += 1
                    updated_episodes_count += 1

                # If episode is not yet released and is in a searchable/pending state, restore FUTURE
                now_utc = datetime.now(timezone.utc)
                if (
                    local_ep.air_date is not None
                    and not is_episode_aired(local_ep.air_date, now_utc)
                    and local_ep.status
                    in (
                        EpisodeStatus.SEARCHING,
                        EpisodeStatus.PENDING,
                    )
                ):
                    local_ep.status = EpisodeStatus.FUTURE
                    item_episodes_updated += 1
                    updated_episodes_count += 1
                    season_obj = local_ep.season
                    if season_obj is not None:
                        _sync_season_status_from_episodes(season_obj)

        if item_episodes_updated > 0:
            titles_updated_count += 1
            logger.debug(
                "%s (%d/%d) '%s': %d episode air dates updated.",
                log_prefix,
                item_idx,
                total_items,
                item.title,
                item_episodes_updated,
            )

    if stats is not None:
        stats.episodes_updated = updated_episodes_count
        stats.titles_updated = titles_updated_count

    if updated_episodes_count > 0:
        # Reconcile parent media item statuses for any affected items
        for item in items:
            await _recalculate_parent_status(session, item)
    await session.commit()

    return updated_episodes_count


async def sync_provider(
    session: AsyncSession, provider: Provider, result: dict[str, Any]
) -> None:
    """Run the full Simkl watchlist sync pipeline for *provider* (7 phases)."""
    import json

    from app.core import automation as au
    from app.core.phase_stats import TbaDiscoveryStats

    client_id, access_token = await simkl.ensure_valid_simkl_token(session, provider)
    cfg = provider.watchlist_config
    sync_movies = bool(cfg.get("sync_movies", True))
    sync_series = bool(cfg.get("sync_series", True))
    sync_anime = bool(cfg.get("sync_anime", True))
    active_simkl_ids: set[int] = set()

    # Release any read transaction snapshot before outbound HTTP calls
    await session.commit()

    fetched: dict[str, list[dict[str, Any]]] = {}
    for category, enabled in (
        ("movies", sync_movies),
        ("shows", sync_series),
        ("anime", sync_anime),
    ):
        fetched[category] = []
        if not enabled:
            continue
        (
            fetched[category],
            client_id,
            access_token,
        ) = await simkl._fetch_simkl_category_with_retry(
            session,
            provider,
            category,
            client_id,
            access_token,
        )
        active_simkl_ids.update(simkl._extract_active_simkl_ids(fetched[category]))
    logger.info(
        "[SYNC] [1/7] Fetched %d movies, %d shows, %d anime",
        len(fetched["movies"]),
        len(fetched["shows"]),
        len(fetched["anime"]),
    )

    ingest_stats = IngestStats()
    for category, media_type in (
        ("movies", MediaType.MOVIE),
        ("shows", MediaType.SHOW),
        ("anime", MediaType.ANIME),
    ):
        await au._sync_items(
            session,
            fetched[category],
            media_type,
            provider.id,
            provider_name=provider.name,
            stats=ingest_stats,
        )
    logger.info(
        "[SYNC] [2/7] Ingested %d items (%d newly added)",
        ingest_stats.ingested,
        ingest_stats.added,
    )

    sequels_consolidated = 0
    anime_seasons_updated = 0
    if sync_anime:
        sequels_consolidated = await au.consolidate_standalone_anime_sequels(session)
        anime_seasons_updated = await au.refresh_due_future_anime_seasons(session)
    logger.info(
        "[SYNC] [3/7] Reconciled anime hierarchies (%d sequels consolidated, %d TBA seasons updated)",
        sequels_consolidated,
        anime_seasons_updated,
    )

    air_stats = AirDateRefreshStats()
    await au.enrich_active_shows_episode_air_dates(session, stats=air_stats)
    logger.info(
        "[SYNC] [4/7] Air date refresh: %d episode dates updated across %d titles",
        air_stats.episodes_updated,
        air_stats.titles_updated,
    )

    rolled = await au.rollover_due_future_episodes_and_seasons(session)
    logger.info(
        "[SYNC] [5/7] Rollover: %d items transitioned to searching",
        rolled["episodes_rolled_over"] + rolled["seasons_rolled_over"],
    )

    tba_stats = TbaDiscoveryStats()
    await au.refresh_due_future_tv_seasons(session, stats=tba_stats)
    logger.info(
        "[SYNC] [6/7] TMDB TBA season discovery: %d TV shows evaluated, %d seasons discovered",
        tba_stats.shows_evaluated,
        tba_stats.seasons_discovered,
    )

    from app.services.watchlist_ingest import (
        mark_absent_sources_completed,
    )

    await mark_absent_sources_completed(
        session,
        provider,
        await _simkl_seen_remote_ids(
            session,
            provider.id,
            active_simkl_ids,
            sync_movies=sync_movies,
            sync_series=sync_series,
            sync_anime=sync_anime,
        ),
    )
    duplicates_merged = await au.deduplicate_media_items(session)
    pruned = await au.prune_inactive_media_items(session)
    logger.info(
        "[SYNC] [7/7] Maintenance: %d duplicates merged, %d completed/dropped titles pruned",
        duplicates_merged,
        pruned,
    )

    result.update(
        ingested=ingest_stats.ingested,
        added=ingest_stats.added,
        pruned=pruned,
    )
    cfg = provider.watchlist_config
    cfg["last_synced_at"] = datetime.now(timezone.utc).isoformat()
    cfg.pop("last_error", None)
    provider.config_json = json.dumps(cfg)
    await session.commit()


_KIND_BY_KEY = {"movie": "movie", "show": "show", "anime": "anime"}


class SimklWatchlistAdapter:
    """Simkl watchlist adapter with a native, franchise-aware sync pipeline."""

    # A failed sync still stamps ``last_synced_at`` so the periodic poller backs off.
    stamp_sync_on_error = True

    async def fetch_entries(self, provider: Provider) -> list[WatchlistEntry]:
        async with async_session_factory() as session:
            target = (
                await session.get(Provider, provider.id) if provider.id else None
            ) or provider
            client_id, access_token = await simkl.ensure_valid_simkl_token(
                session, target
            )
            entries: list[WatchlistEntry] = []
            for category in ("movies", "shows", "anime"):
                (
                    raw,
                    client_id,
                    access_token,
                ) = await simkl._fetch_simkl_category_with_retry(
                    session, target, category, client_id, access_token
                )
                for item in raw:
                    key = next((k for k in _KIND_BY_KEY if item.get(k)), None)
                    media = item.get(key) if key else None
                    ids = (media or {}).get("ids") or {}
                    if not isinstance(media, dict) or ids.get("simkl") is None:
                        continue
                    entries.append(
                        WatchlistEntry(
                            remote_id=str(ids["simkl"]),
                            media_kind=_KIND_BY_KEY[key or "show"],  # type: ignore[arg-type]
                            title=str(media.get("title") or ""),
                            year=media.get("year"),
                            list_status=str(item.get("status") or "plantowatch"),  # type: ignore[arg-type]
                            tmdb_id=_int_or_none(ids.get("tmdb")),
                            imdb_id=ids.get("imdb"),
                            anilist_id=_int_or_none(ids.get("anilist")),
                            mal_id=_int_or_none(ids.get("mal")),
                        )
                    )
        return entries

    sync_provider = staticmethod(sync_provider)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


ADAPTER = SimklWatchlistAdapter()
