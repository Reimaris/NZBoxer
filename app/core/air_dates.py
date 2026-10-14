"""Episode air-date refresh for upcoming TV shows and anime.

Lives outside any provider adapter so its sources can change independently.
Helpers from ``app.core.automation`` are imported lazily (automation re-exports
this module's entry point).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.phase_stats import AirDateRefreshStats
from app.db.models import Provider
from app.services import tmdb
from app.services.watchlists import simkl

logger = logging.getLogger("app.core.automation")


def keep_existing_air_date(existing: datetime | None, incoming: datetime) -> bool:
    """True when ``incoming`` would downgrade an exact air time to a date-only one.

    Only the same UTC calendar day counts: a date-only value for a different day
    is a genuine reschedule and must be written.
    """
    from app.core.automation import _as_utc, is_date_only_air_date

    if existing is None or not is_date_only_air_date(incoming):
        return False
    if is_date_only_air_date(existing):
        return False
    return _as_utc(existing).date() == _as_utc(incoming).date()


async def _fetch_tmdb_episode_dates(
    item: Any, log_prefix: str
) -> list[tuple[int, int, str]] | None:
    """Fetch (season, episode, YYYY-MM-DD) tuples for an item's unreleased seasons.

    Returns None when TMDB cannot be used (no id, unconfigured, HTTP failure).
    """
    from app.db.models import EpisodeStatus, SeasonStatus

    if not item.tmdb_id:
        return None
    rows: list[tuple[int, int, str]] = []
    for season in item.seasons or []:
        unreleased = season.status == SeasonStatus.FUTURE or any(
            ep.status == EpisodeStatus.FUTURE for ep in season.episodes or []
        )
        if not unreleased:
            continue
        try:
            data = await tmdb.get_season_details(
                int(item.tmdb_id), season.season_number
            )
        except Exception as e:
            logger.warning(
                "%s TMDB season air dates failed for item %s S%s: %s",
                log_prefix,
                item.id,
                season.season_number,
                e,
            )
            return None
        if data is None:
            logger.warning(
                "%s TMDB season air dates unavailable for item %s S%s.",
                log_prefix,
                item.id,
                season.season_number,
            )
            return None
        for ep in data.get("episodes") or []:
            e_num = ep.get("episode_number")
            date_raw = ep.get("air_date")
            if e_num is None or not date_raw:
                continue
            rows.append((season.season_number, int(e_num), str(date_raw)))
    return rows


def _apply_episode_dates(item: Any, rows: Any) -> int:
    """Write (season, episode, raw date) rows onto matching local episodes.

    Never downgrades an exact air time to a date-only one for the same day.
    Restores FUTURE on not-yet-released searchable episodes. Returns the number
    of changes made.
    """
    from app.core.automation import is_episode_aired, parse_broadcast_air_date
    from app.core.transfer_poller import _sync_season_status_from_episodes
    from app.db.models import EpisodeStatus

    local_episodes_map = {
        (s.season_number, ep.episode_number): ep
        for s in item.seasons or []
        for ep in s.episodes or []
    }
    changed = 0
    for s_num, e_num, date_raw in rows:
        if s_num is None or e_num is None or not date_raw:
            continue
        try:
            key = (int(s_num), int(e_num))
        except (ValueError, TypeError):
            continue

        local_ep = local_episodes_map.get(key)
        if local_ep is None:
            continue
        parsed_dt = parse_broadcast_air_date(date_raw)
        if (
            parsed_dt is not None
            and local_ep.air_date != parsed_dt
            and not keep_existing_air_date(local_ep.air_date, parsed_dt)
        ):
            local_ep.air_date = parsed_dt
            changed += 1

        # If episode is not yet released and is in a searchable/pending state, restore FUTURE
        now_utc = datetime.now(timezone.utc)
        if (
            local_ep.air_date is not None
            and not is_episode_aired(local_ep.air_date, now_utc)
            and local_ep.status in (EpisodeStatus.SEARCHING, EpisodeStatus.PENDING)
        ):
            local_ep.status = EpisodeStatus.FUTURE
            changed += 1
            if local_ep.season is not None:
                _sync_season_status_from_episodes(local_ep.season)
    return changed


async def enrich_active_shows_episode_air_dates(
    session: AsyncSession,
    now: datetime | None = None,
    *,
    log_prefix: str = "[SYNC] [3/5]",
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
    )
    from app.core.transfer_poller import (
        _recalculate_parent_status,
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
            if item.media_type != MediaType.SHOW:
                continue
            tmdb_rows = await _fetch_tmdb_episode_dates(item, log_prefix)
            item.last_air_dates_refreshed_at = now
            if not tmdb_rows:
                continue
            changed = _apply_episode_dates(item, tmdb_rows)
            if changed:
                titles_updated_count += 1
                updated_episodes_count += changed
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

        simkl_rows = [
            (ep_info.get("season"), ep_info.get("episode"), ep_info.get("date"))
            for ep_info in episodes_data
        ]
        item_episodes_updated = _apply_episode_dates(item, simkl_rows)
        updated_episodes_count += item_episodes_updated

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
