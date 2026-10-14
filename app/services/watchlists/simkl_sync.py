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

from app.core.phase_stats import IngestStats
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
        "[SYNC] [2/5] Ingesting %d %s from Simkl provider '%s'...",
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
            "[SYNC] [2/5] (%d/%d) Processed %s '%s' (%s) -> Status: %s",
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
