"""Cross-provider watchlist ingestion and unification (m56, ADR-130).

Turns normalized :class:`WatchlistEntry` objects from any provider into
``MediaItem`` rows linked through ``MediaItemWatchlistSource`` junction rows,
deduplicating across providers by external ids.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    ACTIVE_LIST_STATUSES,
    Episode,
    EpisodeStatus,
    MediaItem,
    MediaItemWatchlistSource,
    MediaStatus,
    MediaType,
    Provider,
    Season,
    SeasonStatus,
)
from app.services.watchlist_providers import WatchlistEntry

logger = logging.getLogger(__name__)

_ANIME_KINDS = frozenset({"anime", "anime_movie"})

CATEGORY_TOGGLE_BY_KIND: dict[str, str] = {
    "movie": "sync_movies",
    "show": "sync_series",
    "anime": "sync_anime",
    "anime_movie": "sync_anime",
}
_TOGGLE_BY_MEDIA_TYPE: dict[MediaType, str] = {
    MediaType.MOVIE: "sync_movies",
    MediaType.SHOW: "sync_series",
    MediaType.ANIME: "sync_anime",
}


def filter_enabled_entries(
    provider: Provider, entries: list[WatchlistEntry]
) -> list[WatchlistEntry]:
    """Drop entries whose category sync toggle is off for *provider*."""
    cfg = provider.watchlist_config
    return [e for e in entries if cfg.get(CATEGORY_TOGGLE_BY_KIND[e.media_kind], True)]


async def seen_ids_with_disabled_categories(
    session: AsyncSession, provider: Provider, fetched_remote_ids: set[str]
) -> set[str]:
    """Fetched ids plus existing sources of disabled categories, so a toggle never completes them."""
    cfg = provider.watchlist_config
    disabled = [
        mt for mt, toggle in _TOGGLE_BY_MEDIA_TYPE.items() if not cfg.get(toggle, True)
    ]
    seen = set(fetched_remote_ids)
    if disabled:
        rows = await session.execute(
            select(MediaItemWatchlistSource.remote_id)
            .join(MediaItem, MediaItem.id == MediaItemWatchlistSource.media_item_id)
            .where(
                MediaItemWatchlistSource.provider_id == provider.id,
                MediaItem.media_type.in_(disabled),
            )
        )
        seen.update(rows.scalars().all())
    return seen


@dataclass
class IngestResult:
    ingested: int = 0
    added: int = 0
    linked: int = 0  # attached to an existing MediaItem
    transitioned: int = 0  # source status changed


def _has_usable_ids(entry: WatchlistEntry) -> bool:
    if entry.media_kind in _ANIME_KINDS:
        return bool(entry.anilist_id or entry.mal_id)
    return bool(entry.tmdb_id or entry.imdb_id)


async def _first_item(session: AsyncSession, *conditions) -> MediaItem | None:
    stmt = (
        select(MediaItem)
        .where(*conditions)
        .order_by(MediaItem.id.asc())
        .options(selectinload(MediaItem.seasons))
    )
    return (await session.execute(stmt)).scalars().first()


async def _anime_merged_into(
    session: AsyncSession, anilist_id: int
) -> MediaItem | None:
    """Anime franchise item that already absorbed AniList entry *anilist_id* (no Season row)."""
    stmt = (
        select(MediaItem)
        .where(
            MediaItem.media_type == MediaType.ANIME,
            # LIKE is only a prefilter; the exact id is confirmed below.
            MediaItem.associated_franchise_ids_json.like(f"%{anilist_id}%"),
        )
        .order_by(MediaItem.id.asc())
        .options(selectinload(MediaItem.seasons))
    )
    for item in (await session.execute(stmt)).scalars():
        if anilist_id in item.associated_franchise_ids:
            return item
    return None


async def resolve_existing_item(
    session: AsyncSession,
    entry: WatchlistEntry,
    mal_to_anilist: dict[int, int] | None = None,
) -> MediaItem | None:
    """Find the MediaItem an entry belongs to via external ids, or ``None``.

    Movies/shows match ``tmdb_id`` then ``imdb_id``; anime match ``anilist_id``
    (resolving ``mal_id`` through *mal_to_anilist* or a single AniList lookup)
    then ``mal_id``. Anime ids also match consolidated ``Season`` rows.
    """
    if entry.media_kind in _ANIME_KINDS:
        anilist_id = entry.anilist_id
        if not anilist_id and entry.mal_id:
            if mal_to_anilist is None:
                from app.services import anilist

                mal_to_anilist = await anilist.get_anime_ids_by_mal_ids([entry.mal_id])
            anilist_id = mal_to_anilist.get(entry.mal_id)
        if anilist_id:
            item = await _first_item(session, MediaItem.anilist_id == anilist_id)
            if item:
                return item
            season = (
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
            if season and season.media_item:
                return await _first_item(session, MediaItem.id == season.media_item_id)
            merged = await _anime_merged_into(session, anilist_id)
            if merged:
                return merged
        if entry.mal_id:
            item = await _first_item(
                session,
                MediaItem.media_type == MediaType.ANIME,
                MediaItem.mal_id == entry.mal_id,
            )
            if item:
                return item
            season = (
                (
                    await session.execute(
                        select(Season).where(Season.mal_id == entry.mal_id)
                    )
                )
                .scalars()
                .first()
            )
            if season:
                return await _first_item(session, MediaItem.id == season.media_item_id)
        return None

    media_type = MediaType.MOVIE if entry.media_kind == "movie" else MediaType.SHOW
    if entry.tmdb_id:
        item = await _first_item(
            session,
            MediaItem.media_type == media_type,
            MediaItem.tmdb_id == entry.tmdb_id,
        )
        if item:
            return item
    if entry.imdb_id:
        return await _first_item(
            session,
            MediaItem.media_type == media_type,
            MediaItem.imdb_id == entry.imdb_id,
        )
    return None


def _fill_missing_ids(
    item: MediaItem, entry: WatchlistEntry, anilist_id: int | None
) -> None:
    """Populate empty external ids on *item*; never overwrite or null a value."""
    for attr, value in (
        ("tmdb_id", entry.tmdb_id),
        ("imdb_id", entry.imdb_id),
        ("tvdb_id", entry.tvdb_id),
        ("anilist_id", anilist_id),
        ("mal_id", entry.mal_id),
    ):
        if value and not getattr(item, attr):
            setattr(item, attr, value)


def _matched_season(item: MediaItem, entry: WatchlistEntry) -> Season | None:
    """Season of *item* that the entry's AniList/MAL id points at, if any."""
    for season in item.seasons or []:
        if (entry.anilist_id and season.anilist_id == entry.anilist_id) or (
            entry.mal_id and season.mal_id == entry.mal_id
        ):
            return season
    return None


def _season_number_order(item: MediaItem, number: int) -> int:
    """Chronological watch order of provider season *number* (fallback: the number)."""
    seasons = sorted(item.seasons or [], key=lambda s: (s.watch_order, s.season_number))
    for match in (
        lambda s: (
            getattr(s, "entry_type", "season") == "season" and s.type_number == number
        ),
        lambda s: s.season_number == number,
        lambda s: s.type_number == number,
        lambda s: s.watch_order == number,
    ):
        for season in seasons:
            if match(season):
                return int(season.watch_order)
    return number


def _entry_order(item: MediaItem, entry: WatchlistEntry, matched: Season | None) -> int:
    """Return the watch_order reached by an entry with progress (furthest signal wins)."""
    orders = [int(matched.watch_order or 1)] if matched else []
    if entry.watched_season:
        orders.append(_season_number_order(item, entry.watched_season))
    return max(orders) if orders else 1


async def _monitor_matched_season(session: AsyncSession, season: Season) -> None:
    """A list entry that maps to a season makes that season (and its episodes) searchable."""
    season.monitored = True
    if season.status == SeasonStatus.PENDING:
        season.status = SeasonStatus.SEARCHING
    episodes = (
        (await session.execute(select(Episode).where(Episode.season_id == season.id)))
        .scalars()
        .all()
    )
    for ep in episodes:
        ep.monitored = True
        if ep.status == EpisodeStatus.PENDING:
            ep.status = EpisodeStatus.SEARCHING


async def _prefer_franchise_parent(
    session: AsyncSession, standalone: MediaItem, anilist_id: int
) -> MediaItem:
    """If another item tracks *anilist_id* as a season, retire the standalone and use that parent."""
    season = (
        (
            await session.execute(
                select(Season).where(
                    Season.anilist_id == anilist_id,
                    Season.media_item_id != standalone.id,
                )
            )
        )
        .scalars()
        .first()
    )
    if season is None:
        return standalone
    parent = await _first_item(session, MediaItem.id == season.media_item_id)
    if parent is None:
        return standalone
    standalone.status = MediaStatus.IGNORED
    return parent


async def _remote_id_taken(
    session: AsyncSession, provider: Provider, remote_id: str, own_source_id: int
) -> bool:
    """True if another source of *provider* already holds *remote_id* (unique per provider)."""
    return (
        await session.execute(
            select(MediaItemWatchlistSource.id).where(
                MediaItemWatchlistSource.provider_id == provider.id,
                MediaItemWatchlistSource.remote_id == remote_id,
                MediaItemWatchlistSource.id != own_source_id,
            )
        )
    ).first() is not None


async def _upsert_source(
    session: AsyncSession,
    item: MediaItem,
    provider: Provider,
    entry: WatchlistEntry,
    order: int,
    result: IngestResult,
) -> MediaItemWatchlistSource:
    source = (
        (
            await session.execute(
                select(MediaItemWatchlistSource).where(
                    MediaItemWatchlistSource.media_item_id == item.id,
                    MediaItemWatchlistSource.provider_id == provider.id,
                )
            )
        )
        .scalars()
        .first()
    )
    if source is None:
        source = MediaItemWatchlistSource(
            media_item_id=item.id,
            provider_id=provider.id,
            remote_id=entry.remote_id,
            list_status=entry.list_status,
            last_watched_order=order,
        )
        session.add(source)
        return source

    new_status: str
    if source.remote_id == entry.remote_id:
        new_status = entry.list_status
    else:
        # Several provider entries consolidated into one franchise item: stay active
        # while any of them is active.
        new_status = (
            entry.list_status
            if entry.list_status in ACTIVE_LIST_STATUSES
            or source.list_status not in ACTIVE_LIST_STATUSES
            else source.list_status
        )
        if entry.list_status in ACTIVE_LIST_STATUSES and not await _remote_id_taken(
            session, provider, entry.remote_id, source.id
        ):
            # Track an id that is currently on the list so absence detection stays correct.
            source.remote_id = entry.remote_id
    if new_status != source.list_status:
        source.list_status = new_status
        result.transitioned += 1
    source.last_watched_order = max(source.last_watched_order or 0, order)
    return source


async def _create_item(
    session: AsyncSession,
    provider: Provider,
    entry: WatchlistEntry,
    anilist_id: int | None,
) -> MediaItem:
    from app.core import automation

    is_anime = entry.media_kind in _ANIME_KINDS
    media_type = (
        MediaType.ANIME
        if is_anime
        else (MediaType.MOVIE if entry.media_kind == "movie" else MediaType.SHOW)
    )
    item = MediaItem(
        provider_id=provider.id,
        simkl_id=0,
        imdb_id=entry.imdb_id,
        tmdb_id=entry.tmdb_id,
        tvdb_id=entry.tvdb_id,
        mal_id=entry.mal_id,
        anilist_id=anilist_id,
        title=entry.title,
        year=entry.year,
        media_type=media_type,
        status=MediaStatus.PENDING,
    )
    session.add(item)
    # Commit (not flush) to release the SQLite write lock before slow HTTP enrichment.
    await session.commit()
    if is_anime:
        item = await automation.enrich_anime_metadata(session, item)
    else:
        await automation.enrich_movie_show_metadata(
            session, item, entry.tmdb_id, media_type
        )
    return item


async def ingest_entries(
    session: AsyncSession, provider: Provider, entries: list[WatchlistEntry]
) -> IngestResult:
    """Create or attach MediaItems for *entries* and upsert their source links."""
    from app.core.push_engine import evaluate_watch_progress_and_advance

    result = IngestResult(ingested=len(entries))

    mal_to_anilist: dict[int, int] = {}
    unresolved = sorted(
        {
            e.mal_id
            for e in entries
            if e.media_kind in _ANIME_KINDS and e.mal_id and not e.anilist_id
        }
    )
    if unresolved:
        # Ids already stored on a local item or season need no AniList round-trip.
        known = set(
            (
                await session.execute(
                    select(MediaItem.mal_id).where(MediaItem.mal_id.in_(unresolved))
                )
            ).scalars()
        ) | set(
            (
                await session.execute(
                    select(Season.mal_id).where(Season.mal_id.in_(unresolved))
                )
            ).scalars()
        )
        unresolved = [m for m in unresolved if m not in known]
    if unresolved:
        from app.services import anilist

        mal_to_anilist = await anilist.get_anime_ids_by_mal_ids(unresolved)

    for entry in entries:
        anilist_id = entry.anilist_id or (
            mal_to_anilist.get(entry.mal_id) if entry.mal_id else None
        )
        if not _has_usable_ids(entry):
            logger.debug(
                "[SYNC] Skipping watchlist entry '%s' (%s): no usable external ids.",
                entry.title,
                entry.remote_id,
            )
            continue

        item = await resolve_existing_item(session, entry, mal_to_anilist)
        if item is not None and anilist_id and item.anilist_id == anilist_id:
            item = await _prefer_franchise_parent(session, item, anilist_id)
        if item is None:
            item = await _create_item(session, provider, entry, anilist_id)
            result.added += 1
            created = True
        else:
            created = False
            _fill_missing_ids(item, entry, anilist_id)
            result.linked += 1

        has_progress = entry.watched_episodes > 0 or entry.list_status == "completed"
        matched = None
        if has_progress or entry.media_kind in _ANIME_KINDS:
            await session.refresh(item, ["seasons"])  # async-safe lazy load
            matched = _matched_season(item, entry)
        if matched is not None:
            await _monitor_matched_season(session, matched)
            if (
                item.status == MediaStatus.IGNORED
                and entry.list_status in ACTIVE_LIST_STATUSES
            ):
                item.status = MediaStatus.SEARCHING  # restore a consolidated parent
        elif (
            not created
            and entry.media_kind in _ANIME_KINDS
            and item.anilist_id
            and not item.seasons
            and item.last_metadata_refreshed_at is None
        ):
            # Existing anime that never got its season hierarchy.
            from app.core import automation

            await session.commit()
            item = await automation.enrich_anime_metadata(session, item)
        order = _entry_order(item, entry, matched) if has_progress else 0
        source = await _upsert_source(session, item, provider, entry, order, result)

        if has_progress:
            await evaluate_watch_progress_and_advance(
                session=session,
                item=item,
                item_data={"status": entry.list_status, "watch_order": order},
                matched_season=matched,
            )
        source.last_synced_at = datetime.now(timezone.utc)
        await session.commit()

    return result


async def mark_absent_sources_completed(
    session: AsyncSession, provider: Provider, seen_remote_ids: set[str]
) -> int:
    """Mark this provider's active sources missing from a successful fetch as completed.

    Must only be called after a full, successful fetch: list departure means inactive.
    """
    sources = (
        (
            await session.execute(
                select(MediaItemWatchlistSource).where(
                    MediaItemWatchlistSource.provider_id == provider.id,
                    MediaItemWatchlistSource.list_status.in_(ACTIVE_LIST_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    count = 0
    for source in sources:
        if source.remote_id not in seen_remote_ids:
            source.list_status = "completed"
            count += 1
    if count:
        await session.commit()
    return count


async def prune_inactive_media_items(session: AsyncSession) -> int:
    """Delete items whose watchlist sources are all inactive; push history is preserved.

    Items without any source row are never pruned. Items with an active transfer
    (DOWNLOADING / RATE_LIMITED) are deferred to a later sync.
    """
    from app.core.automation import _item_has_active_downloading_entity
    from app.core.push_engine import detach_item_download_history
    from app.db.models import Season as SeasonModel

    items = (
        (
            await session.execute(
                select(MediaItem)
                .where(MediaItem.watchlist_sources.any())
                .options(
                    selectinload(MediaItem.watchlist_sources),
                    selectinload(MediaItem.seasons).selectinload(SeasonModel.episodes),
                )
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    logger.debug(
        "[SYNC] [MAINT] [4/4] Evaluating %d sourced local items for pruning...",
        len(items),
    )
    pruned = 0
    for item in items:
        if any(s.list_status in ACTIVE_LIST_STATUSES for s in item.watchlist_sources):
            continue
        if _item_has_active_downloading_entity(item):
            logger.info(
                "    ⏳ Deferring prune for '%s' (ID %d) because a transfer is active.",
                item.title,
                item.id,
            )
            continue
        title = item.title
        await detach_item_download_history(session, item)
        await session.delete(item)
        pruned += 1
        logger.debug(
            "[SYNC] [MAINT] [4/4] Pruned '%s' (no longer in active watchlists, 0 active downloads).",
            title,
        )
        logger.info(
            "    🗑️ Pruned inactive watchlist item '%s'; push history preserved.", title
        )
    if pruned:
        await session.flush()
    return pruned
