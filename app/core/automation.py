"""
Automation Orchestrator
=======================
Handles syncing the Simkl watchlist to the local database, searching
Newznab indexers, evaluating scores, and sending releases to TorBox.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging_config import log_process_end, log_process_start
from app.db.database import async_session_factory
from app.db.models import (
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    MediaType,
    Provider,
    Season,
    SeasonStatus,
)
from app.services import simkl, tmdb

logger = logging.getLogger(__name__)


def is_future_or_tba(
    release_date: datetime | None,
    year: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Return True if an item or season is unreleased or TBA (missing release_date or year > current_year)."""
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if year is not None and year > now.year:
        return True
    if release_date is None:
        return True

    rd = (
        release_date.replace(tzinfo=timezone.utc)
        if release_date.tzinfo is None
        else release_date
    )
    return rd > now


def _parse_anilist_start_date(
    start_date: dict[str, Any] | None, now: datetime | None = None
) -> datetime | None:
    """Parse an AniList startDate dict into a UTC datetime, returning None if TBA/unannounced."""
    if not start_date or not start_date.get("year"):
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        yr = int(start_date["year"])
        mo = start_date.get("month")
        dy = start_date.get("day")
        if yr >= now.year and (not mo or not dy):
            # Unannounced month or day in current or future year -> TBA
            return None
        return datetime(yr, int(mo or 1), int(dy or 1), tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _backfill_item_release_date_from_seasons(
    item: MediaItem,
    now: datetime | None = None,
    seasons_iter: Any | None = None,
) -> None:
    """Backfill item.release_date from the earliest non-special Season.air_date when missing."""
    if item.release_date is None:
        from sqlalchemy import inspect as sa_inspect

        if seasons_iter is not None:
            seasons_list = list(seasons_iter)
        elif "seasons" not in sa_inspect(item).unloaded and item.seasons:
            seasons_list = list(item.seasons)
        else:
            seasons_list = []
        air_dates = [
            s.air_date
            for s in seasons_list
            if getattr(s, "season_number", 0) > 0 and s.air_date is not None
        ]
        if air_dates:
            earliest = min(air_dates)
            item.release_date = earliest
            if item.year is None:
                item.year = earliest.year
    if item.release_date is not None and item.status in (
        MediaStatus.PENDING,
        MediaStatus.SEARCHING,
        MediaStatus.FUTURE,
    ):
        item.status = (
            MediaStatus.FUTURE
            if is_future_or_tba(item.release_date, item.year, now)
            else MediaStatus.SEARCHING
        )


def _parse_iso_utc(ts_str: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a timezone-aware UTC datetime."""
    if not ts_str or not isinstance(ts_str, str):
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def is_simkl_sync_due_on_load(
    provider: Provider,
    now: datetime | None = None,
    cooldown_minutes: int = 15,
) -> bool:
    """Return True if a Simkl provider is active, configured, and >15m have elapsed since last_synced_at."""
    if getattr(provider, "type", None) != "simkl":
        return False
    if not getattr(provider, "is_active", True):
        return False
    cfg = provider.simkl_config
    if not (provider.client_id or cfg.get("client_id")) or not (
        provider.access_token or cfg.get("access_token") or cfg.get("refresh_token")
    ):
        return False

    last_synced = _parse_iso_utc(cfg.get("last_synced_at"))
    if last_synced is None:
        return True

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    return (now - last_synced) > timedelta(minutes=cooldown_minutes)


def is_simkl_periodic_sync_due(
    provider: Provider,
    now: datetime | None = None,
) -> bool:
    """Return True if a Simkl provider has periodic sync enabled (sync_interval_minutes > 0) and interval has elapsed."""
    if getattr(provider, "type", None) != "simkl":
        return False
    if not getattr(provider, "is_active", True):
        return False
    cfg = provider.simkl_config
    if not (provider.client_id or cfg.get("client_id")) or not (
        provider.access_token or cfg.get("access_token") or cfg.get("refresh_token")
    ):
        return False

    try:
        interval = int(cfg.get("sync_interval_minutes", 60) or 0)
    except (ValueError, TypeError):
        interval = 60

    if interval <= 0:
        return False

    last_synced = _parse_iso_utc(cfg.get("last_synced_at"))
    if last_synced is None:
        return True

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    return (now - last_synced) >= timedelta(minutes=interval)


async def should_trigger_dashboard_simkl_sync(
    session: AsyncSession,
    now: datetime | None = None,
    cooldown_minutes: int = 15,
) -> bool:
    """Check if any active Simkl provider is due for an automatic page-load sync (>15m cooldown)."""
    stmt = select(Provider).where(Provider.type == "simkl")
    providers = (await session.execute(stmt)).scalars().all()
    return any(
        is_simkl_sync_due_on_load(p, now=now, cooldown_minutes=cooldown_minutes)
        for p in providers
    )


async def run_periodic_simkl_sync_if_due(now: datetime | None = None) -> bool:
    """Run sync_all_providers if any active Simkl provider's periodic sync interval is due."""
    async with async_session_factory() as session:
        stmt = select(Provider).where(Provider.type == "simkl")
        providers = (await session.execute(stmt)).scalars().all()
        due = any(is_simkl_periodic_sync_due(p, now=now) for p in providers)
    if due:
        await sync_all_providers()
        return True
    return False


async def _fetch_simkl_category_with_retry(
    session: AsyncSession,
    provider: Provider,
    media_type_key: str,
    client_id: str,
    access_token: str,
) -> tuple[list[dict[str, Any]], str, str]:
    """Fetch a Simkl watchlist category, retrying once with token refresh on HTTP 401."""
    try:
        items = await simkl.get_watchlist(media_type_key, client_id, access_token)
        return items, client_id, access_token
    except simkl.SimklUnauthorizedError:
        if provider.simkl_config.get("refresh_token"):
            client_id, access_token = await simkl.refresh_provider_simkl_token(
                session, provider
            )
            items = await simkl.get_watchlist(media_type_key, client_id, access_token)
            return items, client_id, access_token
        raise


def _extract_active_simkl_ids(raw_items: list[dict[str, Any]]) -> set[int]:
    """Extract all integer Simkl IDs from a Simkl watchlist payload."""
    active_ids: set[int] = set()
    for item_data in raw_items:
        media_obj = (
            item_data.get("movie") or item_data.get("show") or item_data.get("anime")
        )
        if not isinstance(media_obj, dict):
            continue
        raw_sid = (media_obj.get("ids") or {}).get("simkl")
        if raw_sid is not None:
            try:
                active_ids.add(int(raw_sid))
            except (TypeError, ValueError):
                pass
    return active_ids


def _item_has_active_downloading_entity(item: MediaItem) -> bool:
    """Return True if the MediaItem or any of its child Seasons/Episodes is currently DOWNLOADING."""
    if item.status == MediaStatus.DOWNLOADING:
        return True
    for s in item.seasons or []:
        if s.status == SeasonStatus.DOWNLOADING:
            return True
        for ep in s.episodes or []:
            if ep.status == EpisodeStatus.DOWNLOADING:
                return True
    return False


async def _prune_completed_or_dropped_simkl_items(
    session: AsyncSession,
    provider_id: int,
    active_simkl_ids: set[int],
    *,
    sync_movies: bool,
    sync_series: bool,
    sync_anime: bool,
) -> int:
    """Prune local Simkl MediaItems absent from Simkl plantowatch + watching after detaching their DownloadHistory."""
    from app.core.push_engine import detach_item_download_history

    stmt = (
        select(MediaItem)
        .where(MediaItem.provider_id == provider_id)
        .options(
            selectinload(MediaItem.seasons).selectinload(Season.episodes),
        )
    )
    local_items = (await session.execute(stmt)).scalars().all()
    pruned_count = 0

    for item in local_items:
        if item.media_type == MediaType.SHOW and not sync_series:
            continue
        if item.media_type == MediaType.ANIME and not sync_anime:
            continue
        if item.media_type == MediaType.MOVIE:
            is_anime_m = bool(getattr(item, "is_anime_movie", False))
            if is_anime_m and not (sync_anime or sync_movies):
                continue
            if not is_anime_m and not sync_movies:
                continue

        item_simkl_ids: set[int] = set()
        if item.simkl_id:
            item_simkl_ids.add(int(item.simkl_id))
        for s in item.seasons or []:
            if s.simkl_id:
                item_simkl_ids.add(int(s.simkl_id))

        if not item_simkl_ids:
            continue
        if item_simkl_ids & active_simkl_ids:
            continue
        if _item_has_active_downloading_entity(item):
            logger.info(
                "    ⏳ Deferring Simkl prune for '%s' (ID %d) because a transfer is actively DOWNLOADING.",
                item.title,
                item.id,
            )
            continue

        item_title = item.title
        item_simkl = item.simkl_id
        await detach_item_download_history(session, item)
        await session.delete(item)
        pruned_count += 1
        logger.info(
            "    🗑️ Pruned completed/dropped Simkl watchlist item '%s' (Simkl ID %s); push history preserved.",
            item_title,
            item_simkl,
        )

    if pruned_count > 0:
        await session.flush()
    return pruned_count


async def sync_all_providers() -> None:
    """Sync watchlists and libraries from all configured providers (Simkl, etc.)."""
    import json

    log_process_start(logger, "Provider Sync Engine")
    from app.db.models import Provider

    async with async_session_factory() as session:
        try:
            stmt = select(Provider)
            providers_res = await session.execute(stmt)
            provider_ids = [
                p.id
                for p in providers_res.scalars().unique().all()
                if p.type == "simkl" and getattr(p, "is_active", True)
            ]

            any_synced = False
            for prov_id in provider_ids:
                provider = await session.get(Provider, prov_id)
                if provider is None or not getattr(provider, "is_active", True):
                    continue

                cfg = provider.simkl_config
                has_cid = bool(provider.client_id or cfg.get("client_id"))
                has_tok = bool(
                    provider.access_token
                    or cfg.get("access_token")
                    or cfg.get("refresh_token")
                )
                if not has_cid or not has_tok:
                    logger.warning(
                        "Provider %s lacks Simkl credentials, skipping.",
                        provider.name,
                    )
                    continue

                try:
                    client_id, access_token = await simkl.ensure_valid_simkl_token(
                        session, provider
                    )
                    cfg = provider.simkl_config
                    sync_movies = bool(cfg.get("sync_movies", True))
                    sync_series = bool(cfg.get("sync_series", True))
                    sync_anime = bool(cfg.get("sync_anime", True))
                    active_simkl_ids: set[int] = set()

                    if sync_movies:
                        (
                            movies,
                            client_id,
                            access_token,
                        ) = await _fetch_simkl_category_with_retry(
                            session,
                            provider,
                            "movies",
                            client_id,
                            access_token,
                        )
                        active_simkl_ids.update(_extract_active_simkl_ids(movies))
                        await _sync_items(session, movies, MediaType.MOVIE, provider.id)

                    if sync_series:
                        (
                            shows,
                            client_id,
                            access_token,
                        ) = await _fetch_simkl_category_with_retry(
                            session,
                            provider,
                            "shows",
                            client_id,
                            access_token,
                        )
                        active_simkl_ids.update(_extract_active_simkl_ids(shows))
                        await _sync_items(session, shows, MediaType.SHOW, provider.id)

                    if sync_anime:
                        (
                            anime,
                            client_id,
                            access_token,
                        ) = await _fetch_simkl_category_with_retry(
                            session,
                            provider,
                            "anime",
                            client_id,
                            access_token,
                        )
                        active_simkl_ids.update(_extract_active_simkl_ids(anime))
                        await _sync_items(session, anime, MediaType.ANIME, provider.id)
                        await consolidate_standalone_anime_sequels(session)

                    await _prune_completed_or_dropped_simkl_items(
                        session,
                        provider.id,
                        active_simkl_ids,
                        sync_movies=sync_movies,
                        sync_series=sync_series,
                        sync_anime=sync_anime,
                    )

                    cfg = provider.simkl_config
                    cfg["last_synced_at"] = datetime.now(timezone.utc).isoformat()
                    provider.config_json = json.dumps(cfg)
                    await session.commit()
                    any_synced = True
                except Exception as prov_err:  # noqa: BLE001
                    logger.error(
                        "Provider sync failed for %s: %s",
                        provider.name,
                        prov_err,
                    )
                    await session.rollback()
                    prov_after_rb = await session.get(Provider, prov_id)
                    if prov_after_rb is not None:
                        rb_cfg = prov_after_rb.simkl_config
                        rb_cfg["last_synced_at"] = datetime.now(
                            timezone.utc
                        ).isoformat()
                        prov_after_rb.config_json = json.dumps(rb_cfg)
                        await session.commit()

            if any_synced:
                logger.info("All provider watchlists synced successfully.")
        except Exception as e:  # noqa: BLE001
            logger.error("Provider sync failed: %s", e)
            await session.rollback()
        finally:
            log_process_end(logger, "Provider Sync Engine")


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
        ids = movie_data.get("ids", {})

        tmdb_id_str = ids.get("tmdb")
        tmdb_id = int(tmdb_id_str) if tmdb_id_str else None

        tvdb_id_str = ids.get("tvdb")
        tvdb_id = int(tvdb_id_str) if tvdb_id_str else None

        mal_id_str = ids.get("mal")
        mal_id = int(mal_id_str) if mal_id_str else None

        anilist_id_str = ids.get("anilist")
        anilist_id = int(anilist_id_str) if anilist_id_str else None

        from sqlalchemy import and_, or_

        item_conditions = [MediaItem.simkl_id == simkl_id]
        if anilist_id:
            item_conditions.append(
                and_(
                    MediaItem.anilist_id.isnot(None), MediaItem.anilist_id == anilist_id
                )
            )

        stmt = (
            select(MediaItem)
            .where(or_(*item_conditions))
            .options(selectinload(MediaItem.seasons))
        )
        result = await session.execute(stmt)
        item = result.scalars().first()

        # Check if incoming item is actually a consolidated season
        season_conditions = [Season.simkl_id == simkl_id]
        if anilist_id:
            season_conditions.append(
                and_(Season.anilist_id.isnot(None), Season.anilist_id == anilist_id)
            )

        season_stmt = (
            select(Season)
            .where(or_(*season_conditions))
            .options(selectinload(Season.media_item))
        )
        existing_season = (await session.execute(season_stmt)).scalars().first()
        if existing_season:
            parent_item = existing_season.media_item
            if parent_item:
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
                    evaluate_simkl_watch_progress_and_advance,
                )

                parent_item.simkl_synced_at = datetime.now(timezone.utc)
                await evaluate_simkl_watch_progress_and_advance(
                    session=session,
                    item=parent_item,
                    item_data=item_data,
                    matched_season=existing_season,
                )
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
            session.add(item)
            # Need to flush to get item.id for seasons
            await session.flush()
            is_new = True
        else:
            is_new = False
            # PRESERVE existing DB media_type for sync logic to prevent watchlist sync from overwriting user classifications
            media_type = item.media_type
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
            if not item.anilist_id:
                from app.services import anilist

                item.anilist_id = await anilist.search_anime_id_by_title(
                    item.title, item.year
                )
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
                    if not parent_season.simkl_id and item.simkl_id:
                        parent_season.simkl_id = item.simkl_id
                    if not parent_season.monitored:
                        parent_season.monitored = True
                        if parent_season.status != SeasonStatus.FUTURE:
                            parent_season.status = SeasonStatus.SEARCHING
                elif is_new or not item.seasons:
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
            details = None
            if tmdb_id:
                if media_type == MediaType.SHOW:
                    details = await tmdb.get_show_details(tmdb_id)
                elif media_type == MediaType.MOVIE and not details:
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
                if item.status in (
                    MediaStatus.PENDING,
                    MediaStatus.SEARCHING,
                    MediaStatus.FUTURE,
                ):
                    if is_future_or_tba(release_date, item.year):
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
                    if item.status in (
                        MediaStatus.PENDING,
                        MediaStatus.SEARCHING,
                        MediaStatus.FUTURE,
                    ):
                        if is_future_or_tba(item.release_date, item.year):
                            item.status = MediaStatus.FUTURE
                        else:
                            item.status = MediaStatus.SEARCHING

                    synced_seasons: list[Season] = []
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

                            is_season_future = is_future_or_tba(s_air_date) or (
                                item.status == MediaStatus.FUTURE
                            )

                            # Check if season already exists to avoid UNIQUE constraint error
                            existing_season_stmt = select(Season).where(
                                Season.media_item_id == item.id,
                                Season.season_number == s_num,
                            )
                            existing_season = (
                                (await session.execute(existing_season_stmt))
                                .scalars()
                                .first()
                            )
                            if not existing_season:
                                season_obj = Season(
                                    media_item_id=item.id,
                                    season_number=s_num,
                                    watch_order=s_num,
                                    type_number=s_num,
                                    entry_type="season",
                                    monitored=True,
                                    episode_count=s.get("episode_count"),
                                    air_date=s_air_date,
                                    status=SeasonStatus.FUTURE
                                    if is_season_future
                                    else SeasonStatus.SEARCHING,
                                )
                                session.add(season_obj)
                                await session.flush()
                                synced_seasons.append(season_obj)
                                await _sync_season_episodes(
                                    session, season_obj, tmdb_id=item.tmdb_id
                                )
                            else:
                                existing_season.monitored = True
                                if s_air_date:
                                    existing_season.air_date = s_air_date
                                synced_seasons.append(existing_season)
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
                                    else:
                                        existing_season.status = SeasonStatus.SEARCHING
                    _backfill_item_release_date_from_seasons(
                        item, seasons_iter=synced_seasons
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

        else:
            # Update existing
            item.title = movie_data.get("title", item.title)
            if not item.tvdb_id:
                item.tvdb_id = tvdb_id
            if not item.mal_id:
                item.mal_id = mal_id
            if not item.anilist_id:
                item.anilist_id = anilist_id
                if not item.anilist_id and (
                    media_type == MediaType.ANIME
                    or getattr(item, "is_anime_movie", False)
                ):
                    from app.services import anilist

                    item.anilist_id = await anilist.search_anime_id_by_title(
                        item.title, item.year
                    )
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

        _backfill_item_release_date_from_seasons(item)
        item.simkl_synced_at = datetime.now(timezone.utc)
        from app.core.push_engine import evaluate_simkl_watch_progress_and_advance

        await evaluate_simkl_watch_progress_and_advance(
            session=session,
            item=item,
            item_data=item_data,
            matched_season=None,
        )


async def _sync_season_episodes(
    session: AsyncSession,
    season: Season,
    tmdb_id: int | None = None,
    force_refresh: bool = False,
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

    # If season already has episodes, skip external TMDB API call unless forced
    if len(season.episodes) > 0 and not force_refresh:
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
            existing_eps[ep_num].monitored = True
            if existing_eps[ep_num].status in (
                EpisodeStatus.PENDING,
                EpisodeStatus.SEARCHING,
                EpisodeStatus.FUTURE,
            ):
                if ep_is_future:
                    existing_eps[ep_num].status = EpisodeStatus.FUTURE
                else:
                    existing_eps[ep_num].status = EpisodeStatus.SEARCHING
        else:
            initial_status = (
                EpisodeStatus.FUTURE if ep_is_future else EpisodeStatus.SEARCHING
            )

            new_ep = Episode(
                season_id=season.id,
                episode_number=ep_num,
                monitored=True,
                air_date=air_date,
                status=initial_status,
            )
            session.add(new_ep)


async def enrich_anime_metadata(session: AsyncSession, item: MediaItem) -> None:
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.db.models import MediaStatus, Season, SeasonStatus
    from app.services import anilist

    logger.info(
        "    ℹ️ No TMDB seasons found for Anime '%s'. Falling back to AniList Sequel Consolidation.",
        item.title,
    )

    original_anilist_id = item.anilist_id
    original_simkl_id = item.simkl_id
    if not original_anilist_id:
        return
    hierarchy_data = await anilist.get_anime_root_and_hierarchy(original_anilist_id)

    if hierarchy_data:
        root_node = hierarchy_data["root"]
        hierarchy_list = hierarchy_data["hierarchy"]

        root_id = root_node.get("id")
        if root_id:
            other_root_stmt = select(MediaItem).where(
                MediaItem.anilist_id == root_id,
                MediaItem.id != item.id,
            )
            other_root = (await session.execute(other_root_stmt)).scalars().first()
            if other_root:
                logger.info(
                    "    🗑️ Auto-consolidating duplicate franchise MediaItem '%s' (ID %d) into existing root ID %d.",
                    item.title,
                    item.id,
                    other_root.id,
                )
                if not other_root.tmdb_id and item.tmdb_id:
                    other_root.tmdb_id = item.tmdb_id
                if not other_root.imdb_id and item.imdb_id:
                    other_root.imdb_id = item.imdb_id
                if not other_root.tvdb_id and item.tvdb_id:
                    other_root.tvdb_id = item.tvdb_id
                if not other_root.mal_id and item.mal_id:
                    other_root.mal_id = item.mal_id
                if not other_root.poster_url and item.poster_url:
                    other_root.poster_url = item.poster_url
                await session.delete(item)
                await session.flush()
                return

        was_out_of_order = bool(
            root_node.get("id") and root_node.get("id") != original_anilist_id
        )
        if was_out_of_order:
            logger.info(
                "    🔄 Out-of-order sequel detected. Updating MediaItem '%s' to root franchise metadata.",
                item.title,
            )
            item.anilist_id = root_node.get("id")
            item.mal_id = root_node.get("idMal") or item.mal_id
            titles = root_node.get("title", {})
            item.title = titles.get("english") or titles.get("romaji") or item.title

        now = datetime.now(timezone.utc)
        root_air_date = _parse_anilist_start_date(root_node.get("startDate"), now)
        if item.release_date is None and root_air_date is not None:
            item.release_date = root_air_date

        if item.status in (
            MediaStatus.PENDING,
            MediaStatus.SEARCHING,
            MediaStatus.FUTURE,
        ):
            item.status = (
                MediaStatus.FUTURE
                if is_future_or_tba(item.release_date, item.year, now)
                else MediaStatus.SEARCHING
            )

        season_counter = 0
        movie_counter = 0
        created_or_updated_seasons: list[Season] = []

        async def _create_anilist_season(
            s_num: int,
            anilist_node: dict,
            entry_type: str,
            watch_order: int,
            type_number: int,
        ):
            ep_count = anilist_node.get("episodes")
            start_date = anilist_node.get("startDate")
            s_air_date = _parse_anilist_start_date(start_date, now)
            start_year = (
                start_date.get("year") if isinstance(start_date, dict) else None
            )

            if s_air_date is not None or start_year is not None or watch_order > 1:
                is_season_future = is_future_or_tba(s_air_date, start_year, now)
            else:
                is_season_future = is_future_or_tba(item.release_date, item.year, now)
            node_id = anilist_node.get("id")
            is_monitored = True

            existing_season_stmt = select(Season).where(
                Season.media_item_id == item.id,
                Season.season_number == s_num,
            )
            existing_season = (
                (await session.execute(existing_season_stmt)).scalars().first()
            )
            season_title = anilist_node.get("title", {})
            resolved_title = (
                season_title.get("english")
                or season_title.get("romaji")
                or season_title.get("native")
            )
            if not existing_season:
                season_obj = Season(
                    media_item_id=item.id,
                    season_number=s_num,
                    watch_order=watch_order,
                    type_number=type_number,
                    entry_type=entry_type,
                    monitored=is_monitored,
                    episode_count=ep_count,
                    air_date=s_air_date,
                    anilist_id=node_id,
                    simkl_id=original_simkl_id
                    if (was_out_of_order and node_id == original_anilist_id)
                    else None,
                    title=resolved_title,
                    status=SeasonStatus.FUTURE
                    if is_season_future
                    else SeasonStatus.SEARCHING,
                )
                session.add(season_obj)
                await session.flush()
                created_or_updated_seasons.append(season_obj)

                # Create episodes manually since TMDB sync won't work
                from app.db.models import Episode, EpisodeStatus

                if ep_count:
                    for ep_num in range(1, ep_count + 1):
                        new_ep = Episode(
                            season_id=season_obj.id,
                            episode_number=ep_num,
                            monitored=True,
                            air_date=s_air_date if ep_num == 1 else None,
                            status=EpisodeStatus.FUTURE
                            if is_season_future
                            else EpisodeStatus.SEARCHING,
                        )
                        session.add(new_ep)
            else:
                existing_season.monitored = True
                if s_air_date and not existing_season.air_date:
                    existing_season.air_date = s_air_date
                if node_id:
                    existing_season.anilist_id = node_id
                if resolved_title and not existing_season.title:
                    existing_season.title = resolved_title
                if was_out_of_order:
                    if (
                        node_id == original_anilist_id
                        and original_simkl_id
                        and not existing_season.simkl_id
                    ):
                        existing_season.simkl_id = original_simkl_id
                    elif (
                        node_id != original_anilist_id
                        and existing_season.simkl_id == original_simkl_id
                    ):
                        existing_season.simkl_id = None
                existing_season.watch_order = watch_order
                existing_season.type_number = type_number
                existing_season.entry_type = entry_type
                if (
                    existing_season.status == SeasonStatus.PENDING
                    and not is_season_future
                ):
                    existing_season.status = SeasonStatus.SEARCHING
                created_or_updated_seasons.append(existing_season)

        for idx, node in enumerate(hierarchy_list):
            node_format = str(node.get("format") or "").upper()
            resolved_entry_type = node.get("entry_type") or (
                "movie" if node_format == "MOVIE" else "season"
            )
            if resolved_entry_type == "movie":
                movie_counter += 1
                default_type_num = movie_counter
            else:
                season_counter += 1
                default_type_num = season_counter
            resolved_watch_order = int(node.get("watch_order") or (idx + 1))
            resolved_type_number = int(node.get("type_number") or default_type_num)
            await _create_anilist_season(
                idx + 1,
                node,
                resolved_entry_type,
                resolved_watch_order,
                resolved_type_number,
            )

        _backfill_item_release_date_from_seasons(
            item, now, seasons_iter=created_or_updated_seasons
        )
        await consolidate_standalone_anime_sequels(session)


async def consolidate_standalone_anime_sequels(session: AsyncSession) -> None:
    """Detects standalone MediaItems that are tracked as child Seasons on parent series, and marks them IGNORED."""
    import re

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.models import MediaItem, MediaStatus, MediaType, SeasonStatus

    def _normalize_title(t: str | None) -> str:
        if not t:
            return ""
        # Lowercase, replace punctuation/dashes with space, strip extra whitespace
        t = re.sub(r"[:\-_,.]", " ", t.lower())
        return re.sub(r"\s+", " ", t).strip()

    ROMAN_SUFFIXES: dict[int, list[str]] = {
        2: ["ii", "2", "2nd", "second", "season 2", "s2", "2nd season", "season ii"],
        3: ["iii", "3", "3rd", "third", "season 3", "s3", "3rd season", "season iii"],
        4: ["iv", "4", "4th", "fourth", "season 4", "s4", "4th season", "season iv"],
        5: ["v", "5", "5th", "fifth", "season 5", "s5", "5th season", "season v"],
        6: ["vi", "6", "6th", "sixth", "season 6", "s6", "6th season", "season vi"],
        7: ["vii", "7", "7th", "seventh", "season 7", "s7", "7th season", "season vii"],
        8: [
            "viii",
            "8",
            "8th",
            "eighth",
            "season 8",
            "s8",
            "8th season",
            "season viii",
        ],
    }

    # Fetch all active anime items with their seasons
    anime_stmt = (
        select(MediaItem)
        .where(
            MediaItem.media_type == MediaType.ANIME,
            MediaItem.status != MediaStatus.IGNORED,
        )
        .options(selectinload(MediaItem.seasons))
    )
    all_anime = (await session.execute(anime_stmt)).scalars().unique().all()

    # Parents are anime items with >= 2 seasons
    parents = [item for item in all_anime if len(item.seasons) >= 2]
    candidates = [item for item in all_anime]

    for parent in parents:
        for season in parent.seasons:
            if season.season_number <= 1:
                continue

            s_num = season.season_number
            suffixes = ROMAN_SUFFIXES.get(
                s_num, [str(s_num), f"season {s_num}", f"s{s_num}"]
            )

            parent_titles = [
                _normalize_title(parent.title),
                _normalize_title(parent.alt_title),
            ]
            stripped_parent_titles = set()
            for pt in parent_titles:
                if pt:
                    stripped_parent_titles.add(pt)
                    for sub in ("trouble", "season 1", "1st season", "tv", "part 1"):
                        if sub in pt:
                            stripped = pt.replace(sub, "").strip()
                            if stripped:
                                stripped_parent_titles.add(stripped)

            s_title_norm = _normalize_title(season.title)
            is_generic_season_title = s_title_norm in (
                "season 1",
                "season 2",
                "season 3",
                "season 4",
                "season 5",
                "staffel 1",
                "staffel 2",
                "staffel 3",
                "staffel 4",
                "staffel 5",
                "specials",
            )

            for cand in candidates:
                if cand.id == parent.id or cand.status == MediaStatus.IGNORED:
                    continue

                matched = False

                # 1. Direct ID matches
                if (
                    season.anilist_id
                    and cand.anilist_id
                    and season.anilist_id == cand.anilist_id
                ):
                    matched = True
                elif (
                    season.simkl_id
                    and cand.simkl_id
                    and season.simkl_id == cand.simkl_id
                ):
                    matched = True

                # 2. Season title match
                cand_titles = [
                    _normalize_title(cand.title),
                    _normalize_title(cand.alt_title),
                ]
                cand_titles = [t for t in cand_titles if t]

                if not matched and season.title and not is_generic_season_title:
                    for ct in cand_titles:
                        if ct == s_title_norm:
                            matched = True
                            break

                # 3. Roman Numeral / Suffix / Title Matching
                if not matched:
                    for pt in stripped_parent_titles:
                        for ct in cand_titles:
                            for sfx in suffixes:
                                if ct == f"{pt} {sfx}" or ct == f"{pt}{sfx}":
                                    matched = True
                                    break
                                if sfx in ct and pt in ct:
                                    matched = True
                                    break
                            if matched:
                                break

                            # Check base title containment with air date year match
                            if len(pt) >= 4 and pt in ct:
                                if (
                                    cand.year
                                    and season.air_date
                                    and cand.year == season.air_date.year
                                ):
                                    matched = True
                                    break
                        if matched:
                            break

                if matched:
                    logger.info(
                        "    ⏩ Auto-consolidating sequel MediaItem '%s' (ID %d) -> Soft-ignored (tracked as Season %d of '%s' ID %d).",
                        cand.title,
                        cand.id,
                        season.season_number,
                        parent.title,
                        parent.id,
                    )
                    cand.status = MediaStatus.IGNORED
                    for s in cand.seasons:
                        s.status = SeasonStatus.IGNORED

                    if not season.simkl_id and cand.simkl_id:
                        season.simkl_id = cand.simkl_id
                    if not season.anilist_id and cand.anilist_id:
                        season.anilist_id = cand.anilist_id
                    if (not season.title or is_generic_season_title) and cand.title:
                        season.title = cand.title

    await session.commit()


async def reset_anime_metadata(session: AsyncSession, item_id: int) -> MediaItem | None:
    """Rebuilds anime metadata and franchise season/episode hierarchy cleanly from AniList.

    - Purges invalid/phantom episodes and seasons.
    - Preserves existing DOWNLOADED and COMPLETED statuses and DownloadHistory.
    - Resets last_searched_at timestamps.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.models import (
        Episode,
        EpisodeStatus,
        MediaItem,
        MediaStatus,
        MediaType,
        Season,
        SeasonStatus,
    )
    from app.services import anilist

    stmt = (
        select(MediaItem)
        .where(MediaItem.id == item_id)
        .options(selectinload(MediaItem.seasons).selectinload(Season.episodes))
        .execution_options(populate_existing=True)
    )
    item = (await session.execute(stmt)).scalars().first()
    if not item:
        logger.warning("reset_anime_metadata: Item ID %d not found.", item_id)
        return None

    if item.media_type != MediaType.ANIME and not getattr(
        item, "is_anime_movie", False
    ):
        logger.warning(
            "reset_anime_metadata: Item ID %d is not an Anime (type=%s).",
            item_id,
            item.media_type,
        )
        return item

    if not item.anilist_id:
        resolved_id = await anilist.search_anime_id_by_title(item.title, item.year)
        if resolved_id:
            item.anilist_id = resolved_id
        else:
            logger.warning(
                "reset_anime_metadata: Unable to resolve AniList ID for '%s'.",
                item.title,
            )
            return item

    # Query AniList hierarchy
    hierarchy_data = await anilist.get_anime_root_and_hierarchy(item.anilist_id)
    if hierarchy_data:
        root_node = hierarchy_data["root"]
        hierarchy_list = hierarchy_data["hierarchy"]

        root_id = root_node.get("id")
        if root_id:
            other_root_stmt = select(MediaItem).where(
                MediaItem.anilist_id == root_id,
                MediaItem.id != item.id,
            )
            other_root = (await session.execute(other_root_stmt)).scalars().first()
            if other_root:
                logger.info(
                    "    🗑️ Auto-consolidating duplicate franchise MediaItem '%s' (ID %d) into existing root ID %d.",
                    item.title,
                    item.id,
                    other_root.id,
                )
                if not other_root.tmdb_id and item.tmdb_id:
                    other_root.tmdb_id = item.tmdb_id
                if not other_root.imdb_id and item.imdb_id:
                    other_root.imdb_id = item.imdb_id
                if not other_root.tvdb_id and item.tvdb_id:
                    other_root.tvdb_id = item.tvdb_id
                if not other_root.mal_id and item.mal_id:
                    other_root.mal_id = item.mal_id
                if not other_root.poster_url and item.poster_url:
                    other_root.poster_url = item.poster_url
                await session.delete(item)
                await session.commit()
                return other_root

        if root_node.get("id") != item.anilist_id:
            logger.info(
                "    🔄 Out-of-order sequel detected during reset. Updating MediaItem '%s' to root franchise metadata.",
                item.title,
            )
            item.anilist_id = root_node.get("id")
            item.mal_id = root_node.get("idMal") or item.mal_id
            titles = root_node.get("title", {})
            item.title = titles.get("english") or titles.get("romaji") or item.title
        else:
            titles = root_node.get("title", {})
            if titles.get("english"):
                item.title = titles["english"]
            elif titles.get("romaji"):
                item.title = titles["romaji"]
    else:
        single_details = await anilist.get_anime_season_details(item.anilist_id)
        if single_details:
            hierarchy_list = [single_details]
            root_node = single_details
            titles = single_details.get("title", {})
            if titles.get("english"):
                item.title = titles["english"]
            elif titles.get("romaji"):
                item.title = titles["romaji"]
        else:
            logger.warning(
                "reset_anime_metadata: No AniList data returned for ID %d.",
                item.anilist_id,
            )
            return item

    now = datetime.now(timezone.utc)
    if root_node:
        root_air_date = _parse_anilist_start_date(root_node.get("startDate"), now)
        if item.release_date is None and root_air_date is not None:
            item.release_date = root_air_date
        if item.status in (
            MediaStatus.PENDING,
            MediaStatus.SEARCHING,
            MediaStatus.FUTURE,
        ):
            item.status = (
                MediaStatus.FUTURE
                if is_future_or_tba(item.release_date, item.year, now)
                else MediaStatus.SEARCHING
            )

    # Map existing seasons and episodes
    existing_seasons_map = {s.season_number: s for s in item.seasons}
    existing_episodes_map = {
        (s.season_number, ep.episode_number): ep
        for s in item.seasons
        for ep in s.episodes
    }

    # Reconcile seasons according to AniList hierarchy
    valid_season_numbers = set()
    season_counter = 0
    movie_counter = 0
    for idx, node in enumerate(hierarchy_list):
        s_num = idx + 1
        valid_season_numbers.add(s_num)
        node_format = str(node.get("format") or "").upper()
        entry_type = node.get("entry_type") or (
            "movie" if node_format == "MOVIE" else "season"
        )
        if entry_type == "movie":
            movie_counter += 1
            default_type_num = movie_counter
        else:
            season_counter += 1
            default_type_num = season_counter
        watch_order = int(node.get("watch_order") or s_num)
        type_number = int(node.get("type_number") or default_type_num)

        ep_count = node.get("episodes")
        start_date = node.get("startDate")
        s_air_date = _parse_anilist_start_date(start_date, now)
        start_year = start_date.get("year") if isinstance(start_date, dict) else None

        if s_air_date is not None or start_year is not None or watch_order > 1:
            is_season_future = is_future_or_tba(s_air_date, start_year, now)
        else:
            is_season_future = is_future_or_tba(item.release_date, item.year, now)

        season_obj = existing_seasons_map.get(s_num)
        if not season_obj:
            # Create new season
            season_title = node.get("title", {})
            is_monitored = True
            season_obj = Season(
                media_item_id=item.id,
                season_number=s_num,
                watch_order=watch_order,
                type_number=type_number,
                entry_type=entry_type,
                monitored=is_monitored,
                episode_count=ep_count,
                air_date=s_air_date,
                anilist_id=node.get("id"),
                title=season_title.get("english")
                or season_title.get("romaji")
                or season_title.get("native"),
                status=SeasonStatus.FUTURE
                if is_season_future
                else SeasonStatus.SEARCHING,
            )
            session.add(season_obj)
            await session.flush()
            existing_seasons_map[s_num] = season_obj

        else:
            # Update existing season metadata
            season_obj.monitored = True
            season_title = node.get("title", {})
            season_obj.title = (
                season_title.get("english")
                or season_title.get("romaji")
                or season_title.get("native")
                or season_obj.title
            )
            season_obj.episode_count = ep_count or season_obj.episode_count
            season_obj.air_date = s_air_date or season_obj.air_date
            season_obj.anilist_id = node.get("id") or season_obj.anilist_id
            season_obj.watch_order = watch_order
            season_obj.type_number = type_number
            season_obj.entry_type = entry_type

            if season_obj.status in (
                SeasonStatus.PENDING,
                SeasonStatus.SEARCHING,
                SeasonStatus.CANCELED,
                SeasonStatus.FUTURE,
            ):
                season_obj.status = (
                    SeasonStatus.FUTURE if is_season_future else SeasonStatus.SEARCHING
                )

        season_obj.last_searched_at = None

        # Reconcile episodes for this season
        if ep_count:
            for ep_num in range(1, ep_count + 1):
                ep_obj = existing_episodes_map.pop((s_num, ep_num), None)
                if ep_obj:
                    ep_obj.monitored = True
                    # Episode exists: preserve downloaded/completed/downloading state
                    if ep_obj.status in (
                        EpisodeStatus.DOWNLOADED,
                        EpisodeStatus.COMPLETED,
                        EpisodeStatus.DOWNLOADING,
                    ):
                        pass  # Preserve downloaded file details & status
                    else:
                        # Reset unfulfilled episode status
                        ep_obj.status = (
                            EpisodeStatus.FUTURE
                            if is_season_future
                            else EpisodeStatus.SEARCHING
                        )
                    if s_air_date and ep_num == 1 and not ep_obj.air_date:
                        ep_obj.air_date = s_air_date
                else:
                    # Newly added episode from AniList
                    new_ep = Episode(
                        season_id=season_obj.id,
                        episode_number=ep_num,
                        monitored=True,
                        air_date=s_air_date if ep_num == 1 else None,
                        status=EpisodeStatus.FUTURE
                        if is_season_future
                        else EpisodeStatus.SEARCHING,
                    )
                    session.add(new_ep)

    # Purge remaining phantom episodes
    for phantom_ep in existing_episodes_map.values():
        await session.delete(phantom_ep)

    # Purge obsolete seasons
    for s_num, old_season in existing_seasons_map.items():
        if s_num not in valid_season_numbers:
            await session.delete(old_season)

    active_seasons = [
        s for s_num, s in existing_seasons_map.items() if s_num in valid_season_numbers
    ]
    _backfill_item_release_date_from_seasons(item, now, seasons_iter=active_seasons)

    item.last_searched_at = None
    item.last_metadata_refreshed_at = now

    item_title = item.title
    item_id = item.id

    await session.commit()
    session.expire_all()
    logger.info(
        "✅ Anime metadata reset completed for '%s' (ID %d).", item_title, item_id
    )
    return item


def classify_v3_status_tier(item: MediaItem) -> str:
    """Classify an active MediaItem into one of the 3 v3.0.0 dashboard horizontal sections:
    - 'in_progress'   (Section 1: Active / In Progress & Downloaded Watchlist Items)
    - 'ready_to_push' (Section 2: Ready to Push / Wanted & Unpushed)
    - 'upcoming'      (Section 3: Upcoming / Future & TBA)
    """
    from sqlalchemy import inspect as sa_inspect

    if item.status in (
        MediaStatus.DOWNLOADING,
        MediaStatus.FAILED,
        MediaStatus.DOWNLOADED,
        MediaStatus.COMPLETED,
    ):
        return "in_progress"

    seasons: list[Season] = []
    if "seasons" not in sa_inspect(item).unloaded:
        seasons = list(item.seasons or [])

    if seasons:
        for s in seasons:
            if s.status in (
                SeasonStatus.DOWNLOADING,
                SeasonStatus.FAILED,
                SeasonStatus.DOWNLOADED,
                SeasonStatus.COMPLETED,
            ):
                return "in_progress"
            if "episodes" not in sa_inspect(s).unloaded:
                for ep in s.episodes or []:
                    if ep.status in (
                        EpisodeStatus.DOWNLOADING,
                        EpisodeStatus.FAILED,
                        EpisodeStatus.DOWNLOADED,
                        EpisodeStatus.COMPLETED,
                    ):
                        return "in_progress"

        if (item.downloaded_seasons + item.downloaded_movies) > 0:
            return "in_progress"

        non_special = [s for s in seasons if s.season_number > 0]
        if non_special:
            has_released_entry = False
            for s in non_special:
                if s.status == SeasonStatus.FUTURE:
                    continue
                if s.air_date is not None:
                    if not is_future_or_tba(s.air_date):
                        has_released_entry = True
                        break
                elif not is_future_or_tba(item.effective_release_date, item.year):
                    has_released_entry = True
                    break
            if not has_released_entry:
                return "upcoming"
            return "ready_to_push"

    if item.status == MediaStatus.FUTURE or is_future_or_tba(
        item.effective_release_date, item.year
    ):
        return "upcoming"

    return "ready_to_push"
