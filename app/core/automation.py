"""
Automation Orchestrator
=======================
Handles syncing the Simkl watchlist to the local database, searching
Newznab indexers, evaluating scores, and sending releases to the downloader.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.logging_config import log_process_end, log_process_start
from app.core.phase_stats import (
    AirDateRefreshStats,
    IngestStats,
    SimklPeriodicSyncStatus,
    TbaDiscoveryStats,
)
from app.core.time_utils import format_local_datetime
from app.db.database import async_session_factory
from app.db.models import (
    EpisodeStatus,
    MediaItem,
    MediaItemWatchlistSource,
    MediaStatus,
    MediaType,
    Provider,
    Season,
    SeasonStatus,
)
from app.services import tmdb
from app.services.watchlist_ingest import prune_inactive_media_items
from app.services.watchlists import simkl

logger = logging.getLogger(__name__)

# Global mutex preventing concurrent Simkl watchlist sync executions
_sync_lock = asyncio.Lock()


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


DATE_ONLY_FALLBACK_MICROSECOND = 1
EXACT_MIDNIGHT_BROADCAST_MICROSECOND = 2

AIR_DATE_REFRESH_INTERVAL = timedelta(hours=24)
AIR_DATE_IMMINENT_WINDOW = timedelta(hours=48)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def is_air_date_refresh_due(item: MediaItem, now: datetime) -> bool:
    """Return True if the item needs an upstream air date check under the m49 policy.

    Only unreleased content is checked (FUTURE episodes, or a FUTURE movie item).
    Checks are paced to once per ``AIR_DATE_REFRESH_INTERVAL``; TBA dates are always
    due, known dates only within ``AIR_DATE_IMMINENT_WINDOW`` of release.
    Requires ``item.seasons`` and ``Season.episodes`` to be eagerly loaded.
    """
    now = _as_utc(now)
    air_dates: list[datetime | None] = []
    if item.media_type == MediaType.MOVIE:
        if item.status == MediaStatus.FUTURE:
            air_dates.append(item.release_date)
    else:
        for season in item.seasons:
            for ep in season.episodes:
                if ep.status == EpisodeStatus.FUTURE:
                    air_dates.append(ep.air_date)

    if not air_dates:
        return False

    stamp = item.last_air_dates_refreshed_at
    if stamp is not None and now - _as_utc(stamp) < AIR_DATE_REFRESH_INTERVAL:
        return False

    known = [_as_utc(d) for d in air_dates if d is not None]
    if len(known) < len(air_dates):
        return True  # at least one TBA entity

    earliest = min(known)
    return now < earliest <= now + AIR_DATE_IMMINENT_WINDOW


def parse_broadcast_air_date(date_str: str | None) -> datetime | None:
    """Parse an ISO broadcast timestamp or date string into a timezone-aware UTC datetime.

    - ISO strings with timezone offsets (e.g. "2026-10-11T20:00:00-04:00") are converted to UTC.
      If the resulting UTC time is exactly midnight (00:00:00), microsecond=2 is tagged to distinguish
      it from date-only midnight fallbacks.
    - Date-only strings (e.g. "2026-10-11") default to midnight UTC with microsecond=1 as date-only marker.
    - Returns None if the string is empty or invalid.
    """
    if not date_str or not isinstance(date_str, str):
        return None
    raw = date_str.strip()
    if not raw:
        return None

    try:
        # datetime.fromisoformat handles ISO-8601 strings and YYYY-MM-DD
        dt = datetime.fromisoformat(raw)
        is_date_only = "T" not in raw and " " not in raw
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)

        if is_date_only:
            dt = dt.replace(microsecond=DATE_ONLY_FALLBACK_MICROSECOND)
        elif dt.hour == 0 and dt.minute == 0 and dt.second == 0 and dt.microsecond == 0:
            dt = dt.replace(microsecond=EXACT_MIDNIGHT_BROADCAST_MICROSECOND)
        return dt
    except (ValueError, TypeError):
        return None


def is_episode_aired(
    air_date: datetime,
    now_utc: datetime,
    exact_time: bool | None = None,
) -> bool:
    """Determine whether an episode has aired relative to now_utc.

    - If exact_time is False, or if exact_time is None and air_date has microsecond=1
      (indicating date-only fallback), an end-of-broadcast-day buffer of 24 hours is enforced:
      (air_date.replace(microsecond=0) + 24h) <= now_utc.
    - If air_date contains a specific broadcast time (or exact_time is True), evaluates air_date <= now_utc directly.
    """
    if air_date.tzinfo is None:
        air_date = air_date.replace(tzinfo=timezone.utc)
    else:
        air_date = air_date.astimezone(timezone.utc)

    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    if exact_time is True:
        is_fallback = False
    elif exact_time is False:
        is_fallback = True
    else:
        # Date-only fallback if marked with DATE_ONLY_FALLBACK_MICROSECOND
        # or midnight timestamp without non-zero time components
        is_fallback = air_date.microsecond == DATE_ONLY_FALLBACK_MICROSECOND or (
            air_date.hour == 0
            and air_date.minute == 0
            and air_date.second == 0
            and air_date.microsecond == 0
        )

    if is_fallback:
        base_date = air_date.replace(microsecond=0)
        return (base_date + timedelta(hours=24)) <= now_utc

    return air_date <= now_utc


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


SIMKL_SYNC_INTERVAL_DEFAULT = 60
SIMKL_SYNC_INTERVAL_MIN = 5
SIMKL_SYNC_INTERVAL_MAX = 1440


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


def normalize_sync_interval_minutes(value: object) -> int:
    """Normalize a periodic-sync interval: 0 = disabled, else clamped to 5..1440 (default 60)."""
    if value is None or isinstance(value, bool):
        return SIMKL_SYNC_INTERVAL_DEFAULT
    try:
        n = int(value)  # type: ignore[call-overload]
    except (ValueError, TypeError):
        return SIMKL_SYNC_INTERVAL_DEFAULT
    if n <= 0:
        return 0
    return max(SIMKL_SYNC_INTERVAL_MIN, min(SIMKL_SYNC_INTERVAL_MAX, n))


def evaluate_periodic_simkl_sync(
    providers: Sequence[Provider], now: datetime
) -> SimklPeriodicSyncStatus:
    """Evaluate whether periodic Simkl sync is due across eligible providers (UTC only)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    eligible = []
    for provider in providers:
        if getattr(provider, "type", None) != "simkl":
            continue
        if not getattr(provider, "is_active", True):
            continue
        cfg = provider.simkl_config
        if not (provider.client_id or cfg.get("client_id")) or not (
            provider.access_token or cfg.get("access_token") or cfg.get("refresh_token")
        ):
            continue
        eligible.append(provider)

    if not eligible:
        return SimklPeriodicSyncStatus("no_provider", 0, None, 0)

    best: SimklPeriodicSyncStatus | None = None
    for provider in eligible:
        cfg = provider.simkl_config
        interval = normalize_sync_interval_minutes(cfg.get("sync_interval_minutes"))
        if interval == 0:
            continue
        last = _parse_iso_utc(cfg.get("last_synced_at"))
        if last is None:
            return SimklPeriodicSyncStatus("due", interval, None, 0)
        elapsed = int((now - last).total_seconds() // 60)
        if elapsed >= interval:
            return SimklPeriodicSyncStatus("due", interval, elapsed, 0)
        status = SimklPeriodicSyncStatus(
            "not_due", interval, elapsed, interval - elapsed
        )
        if best is None or status.remaining_minutes < best.remaining_minutes:
            best = status

    return best or SimklPeriodicSyncStatus("disabled", 0, None, 0)


def is_provider_periodic_sync_due(
    provider: Provider,
    now: datetime | None = None,
) -> bool:
    """Return True if a connected watchlist provider has periodic sync enabled and is due."""
    if not getattr(provider, "is_active", True):
        return False
    cfg = provider.watchlist_config
    if provider.type == "simkl":
        # Legacy Simkl tokens (no refresh token) still count as configured.
        configured = bool(provider.client_id or cfg.get("client_id")) and bool(
            provider.access_token or cfg.get("access_token") or cfg.get("refresh_token")
        )
    else:
        configured = provider.watchlist_connection_status == "connected"
    if not configured:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    interval = normalize_sync_interval_minutes(cfg.get("sync_interval_minutes"))
    if interval == 0:
        return False
    last = _parse_iso_utc(cfg.get("last_synced_at"))
    if last is None:
        return True
    return (now - last) >= timedelta(minutes=interval)


def is_simkl_periodic_sync_due(
    provider: Provider,
    now: datetime | None = None,
) -> bool:
    """Return True if a Simkl provider has periodic sync enabled and its interval has elapsed."""
    return is_provider_periodic_sync_due(provider, now)


async def should_trigger_dashboard_simkl_sync(
    session: AsyncSession,
    now: datetime | None = None,
    cooldown_minutes: int = 15,
) -> bool:
    """Check if any active Simkl provider is due for an automatic page-load sync (>15m cooldown)."""
    if _sync_lock.locked():
        return False
    stmt = select(Provider).where(Provider.type == "simkl")
    providers = (await session.execute(stmt)).scalars().all()
    return any(
        is_simkl_sync_due_on_load(p, now=now, cooldown_minutes=cooldown_minutes)
        for p in providers
    )


async def run_periodic_watchlist_sync_if_due(now: datetime | None = None) -> bool:
    """Run sync_all_providers if any active watchlist provider's periodic interval is due."""
    if _sync_lock.locked():
        return False
    now = now or datetime.now(timezone.utc)
    async with async_session_factory() as session:
        stmt = select(Provider).where(Provider.category == "watchlist")
        providers = (await session.execute(stmt)).scalars().all()
        due = any(is_provider_periodic_sync_due(p, now) for p in providers)
    if due:
        await sync_all_providers()
        return True
    return False


run_periodic_simkl_sync_if_due = run_periodic_watchlist_sync_if_due


async def run_orchestrator_tick(now: datetime | None = None) -> None:
    """Execute 15-minute background orchestrator tasks: episode air date rollover, periodic Simkl sync,
    and (at xx:00) the air-date push and upgrade pass."""
    start = time.monotonic()
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    logger.info(
        "[ORCHESTRATOR] Tick start at %s",
        format_local_datetime(now, "%Y-%m-%d %H:%M:%S"),
    )

    # 1. Roll over any FUTURE episodes and seasons that have reached their air date,
    #    then run the paced upstream air date enrichment (m49 pacing policy)
    async with async_session_factory() as session:
        try:
            rolled = await rollover_due_future_episodes_and_seasons(session, now=now)
            logger.info(
                "[ORCHESTRATOR] [1/3] Air date rollover: %d due episodes/seasons rolled over to searching",
                rolled["episodes_rolled_over"] + rolled["seasons_rolled_over"],
            )
        except Exception as e:  # noqa: BLE001
            logger.error("Error during scheduled air date rollover: %s", e)
            logger.info(
                "[ORCHESTRATOR] [1/3] Air date rollover: failed (see error above)"
            )
        refresh_stats = AirDateRefreshStats()
        try:
            await enrich_active_shows_episode_air_dates(
                session,
                now=now,
                log_prefix="[ORCHESTRATOR] [2/3]",
                stats=refresh_stats,
            )
            logger.info(
                "[ORCHESTRATOR] [2/3] Broadcast air date refresh: %d refreshed (%d frozen, %d paced)",
                refresh_stats.episodes_updated,
                refresh_stats.skipped_frozen,
                refresh_stats.skipped_paced,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("Error during scheduled air date enrichment: %s", e)
            logger.info(
                "[ORCHESTRATOR] [2/3] Broadcast air date refresh: failed (see error above)"
            )

    # 1b. Purge stale Push History Ledger rows (m51, ADR-125) on every tick
    try:
        from app.core.transfer_poller import autoclear_history_ledger

        async with async_session_factory() as session:
            result = await autoclear_history_ledger(session, now=now)
        if result["total"] > 0:
            logger.info(
                "🧹 [HISTORY] Autoclear purged %d entr%s (pruned=%d, aged=%d)",
                result["total"],
                "y" if result["total"] == 1 else "ies",
                result["pruned_count"],
                result["aged_count"],
            )
    except Exception as e:  # noqa: BLE001
        logger.error("Error during history autoclear: %s", e)

    # 2. Check and run periodic Simkl sync if due
    try:
        async with async_session_factory() as session:
            providers = (
                (
                    await session.execute(
                        select(Provider).where(Provider.category == "watchlist")
                    )
                )
                .scalars()
                .all()
            )
            status = evaluate_periodic_simkl_sync(providers, now)
            other_due = any(
                is_provider_periodic_sync_due(p, now)
                for p in providers
                if p.type != "simkl"
            )
        tag = "[ORCHESTRATOR] [3/3] Periodic Simkl sync:"
        if status.state != "due" and other_due:
            tag = "[ORCHESTRATOR] [3/3] Periodic watchlist sync:"
            status = SimklPeriodicSyncStatus("due", 0, None, 0)
        if status.state == "due":
            if _sync_lock.locked():
                logger.info("%s due but a sync is already running; skipping", tag)
            else:
                logger.info("%s due -> triggering provider sync", tag)
                await sync_all_providers()
        elif status.state == "not_due":
            logger.info(
                "%s not due (%dm elapsed, interval %dm; %dm remaining)",
                tag,
                status.elapsed_minutes,
                status.interval_minutes,
                status.remaining_minutes,
            )
        elif status.state == "disabled":
            logger.info("%s disabled (interval 0)", tag)
        else:
            logger.info("%s no active Simkl provider with credentials", tag)
    except Exception as e:  # noqa: BLE001
        logger.error("Error during periodic Simkl sync: %s", e)
        logger.info(
            "[ORCHESTRATOR] [3/3] Periodic Simkl sync: failed (see error above)"
        )

    # 3. At every full hour, run the air-date push and upgrade pass after the sync
    if now.minute == 0:
        try:
            from app.core.air_push_engine import run_air_date_push_and_upgrade_tick

            await run_air_date_push_and_upgrade_tick(now)
        except Exception as e:  # noqa: BLE001
            logger.error("Error during air-date push and upgrade tick: %s", e)

    logger.info(
        "[ORCHESTRATOR] Tick complete (took %dms)",
        int((time.monotonic() - start) * 1000),
    )


async def deduplicate_media_items(session: AsyncSession) -> int:
    """Detect and merge duplicate MediaItem rows sharing the same Simkl ID or external IDs.

    Preserves the canonical item (preferring non-ignored, active/completed status,
    history/seasons, and lowest ID), re-homes any DownloadHistory and FailureLog rows,
    and deletes the redundant duplicate MediaItem row(s).
    """
    from sqlalchemy import update

    from app.db.models import DownloadHistory, FailureLog

    stmt = (
        select(MediaItem)
        .order_by(MediaItem.id.asc())
        .options(
            selectinload(MediaItem.seasons),
            selectinload(MediaItem.download_history),
        )
    )
    all_items = list((await session.execute(stmt)).scalars().unique().all())
    if len(all_items) <= 1:
        return 0

    def _canonical_rank(item: MediaItem) -> tuple[int, int, int, int, int, int]:
        not_ignored = 1 if item.status != MediaStatus.IGNORED else 0
        active_or_done = (
            1
            if item.status
            in (
                MediaStatus.DOWNLOADING,
                MediaStatus.COMPLETED,
                MediaStatus.DOWNLOADED,
            )
            else 0
        )
        hist_count = len(item.download_history or [])
        season_count = len(item.seasons or [])
        has_sticky = (
            1
            if (item.preset_id is not None or bool(item.custom_search_config_json))
            else 0
        )
        return (
            not_ignored,
            active_or_done,
            hist_count,
            season_count,
            has_sticky,
            -int(item.id or 0),
        )

    removed_ids: set[int] = set()
    removed_count = 0

    async def _merge_and_delete(canonical: MediaItem, dup: MediaItem) -> None:
        nonlocal removed_count
        if (
            dup.id in removed_ids
            or canonical.id in removed_ids
            or canonical.id == dup.id
        ):
            return

        logger.info(
            "    🧹 Deduplicating MediaItem '%s' (removing duplicate ID %d, keeping canonical ID %d).",
            dup.title,
            dup.id,
            canonical.id,
        )
        for attr in (
            "simkl_id",
            "tmdb_id",
            "imdb_id",
            "tvdb_id",
            "mal_id",
            "anilist_id",
            "alt_title",
            "poster_url",
            "overview",
            "release_date",
            "runtime_minutes",
            "preset_id",
            "custom_search_config_json",
        ):
            if not getattr(canonical, attr, None) and getattr(dup, attr, None):
                setattr(canonical, attr, getattr(dup, attr))

        for sid in dup.associated_franchise_ids:
            canonical.add_associated_franchise_id(sid)
        if dup.simkl_id and canonical.seasons:
            for s in canonical.seasons:
                if not s.simkl_id and (
                    (dup.anilist_id and s.anilist_id == dup.anilist_id)
                    or (dup.mal_id and s.mal_id == dup.mal_id)
                ):
                    s.simkl_id = dup.simkl_id
                    break

        canonical_provider_ids = {
            src.provider_id for src in canonical.watchlist_sources or []
        }
        for src in list(dup.watchlist_sources or []):
            if src.provider_id in canonical_provider_ids:
                await session.delete(src)
            else:
                await session.execute(
                    update(MediaItemWatchlistSource)
                    .where(MediaItemWatchlistSource.id == src.id)
                    .values(media_item_id=canonical.id)
                )
                canonical_provider_ids.add(src.provider_id)
        await session.flush()
        session.expire(dup, ["watchlist_sources"])
        session.expire(canonical, ["watchlist_sources"])
        await session.execute(
            update(DownloadHistory)
            .where(DownloadHistory.media_item_id == dup.id)
            .values(media_item_id=canonical.id)
        )
        await session.execute(
            update(FailureLog)
            .where(FailureLog.media_item_id == dup.id)
            .values(media_item_id=canonical.id)
        )
        removed_ids.add(dup.id)
        await session.delete(dup)
        removed_count += 1

    # Group by key extractors in priority order:
    # 1) simkl_id (> 0)
    # 2) (media_type, tmdb_id) for MOVIE and SHOW
    # 3) (media_type, imdb_id) for MOVIE and SHOW
    # 4) (media_type, anilist_id) for ANIME
    for key_fn in (
        lambda i: (
            ("simkl", int(i.simkl_id)) if (i.simkl_id and int(i.simkl_id) > 0) else None
        ),
        lambda i: (
            ("tmdb", i.media_type, int(i.tmdb_id))
            if (i.tmdb_id and i.media_type in (MediaType.MOVIE, MediaType.SHOW))
            else None
        ),
        lambda i: (
            ("imdb", i.media_type, str(i.imdb_id).strip().lower())
            if (i.imdb_id and i.media_type in (MediaType.MOVIE, MediaType.SHOW))
            else None
        ),
        lambda i: (
            ("anilist", i.media_type, int(i.anilist_id))
            if (i.anilist_id and i.media_type == MediaType.ANIME)
            else None
        ),
    ):
        groups: dict[tuple[Any, ...], list[MediaItem]] = {}
        for item in all_items:
            if item.id in removed_ids:
                continue
            key = key_fn(item)
            if key is not None:
                groups.setdefault(key, []).append(item)

        for group_items in groups.values():
            if len(group_items) <= 1:
                continue
            sorted_group = sorted(group_items, key=_canonical_rank, reverse=True)
            canonical = sorted_group[0]
            for dup in sorted_group[1:]:
                await _merge_and_delete(canonical, dup)

    if removed_count > 0:
        await session.flush()
    return removed_count


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
    """Return True if the MediaItem or any child Season/Episode has an active transfer (DOWNLOADING or RATE_LIMITED)."""
    if item.status in (MediaStatus.DOWNLOADING, MediaStatus.RATE_LIMITED):
        return True
    for s in item.seasons or []:
        if s.status in (SeasonStatus.DOWNLOADING, SeasonStatus.RATE_LIMITED):
            return True
        for ep in s.episodes or []:
            if ep.status in (EpisodeStatus.DOWNLOADING, EpisodeStatus.RATE_LIMITED):
                return True
    return False


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
    from app.db.models import MediaItemWatchlistSource

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


def _record_provider_error(provider: Provider, error: object) -> str:
    """Store a redacted ``last_error`` on the provider config and return the message."""
    import json

    from app.core.log_redaction import redact_text

    message = redact_text(str(error)) or type(error).__name__
    cfg = provider.watchlist_config
    cfg["last_error"] = message
    provider.config_json = json.dumps(cfg)
    return message


async def _sync_adapter_provider(
    session: AsyncSession, provider: Provider, result: dict[str, Any]
) -> None:
    """Fetch, ingest, complete-absent and prune one non-Simkl watchlist provider."""
    import json

    from app.services.watchlist_ingest import (
        ingest_entries,
        mark_absent_sources_completed,
    )
    from app.services.watchlist_providers import get_adapter

    adapter = get_adapter(provider.type)
    # Release any read transaction snapshot before outbound HTTP calls
    await session.commit()
    entries = await adapter.fetch_entries(provider)
    logger.info("[SYNC] [1/4] %s: fetched %d entries", provider.name, len(entries))

    ingest = await ingest_entries(session, provider, entries)
    logger.info(
        "[SYNC] [2/4] %s: ingested %d (%d added, %d linked, %d status changes)",
        provider.name,
        ingest.ingested,
        ingest.added,
        ingest.linked,
        ingest.transitioned,
    )

    completed = await mark_absent_sources_completed(
        session, provider, {e.remote_id for e in entries}
    )
    logger.info(
        "[SYNC] [3/4] %s: %d departed entries marked completed",
        provider.name,
        completed,
    )

    pruned = await prune_inactive_media_items(session)
    logger.info("[SYNC] [4/4] %s: %d inactive titles pruned", provider.name, pruned)

    result.update(ingested=ingest.ingested, added=ingest.added, pruned=pruned)
    cfg = provider.watchlist_config
    cfg["last_synced_at"] = datetime.now(timezone.utc).isoformat()
    cfg.pop("last_error", None)
    provider.config_json = json.dumps(cfg)
    await session.commit()


async def _run_provider_sync(
    only_provider_id: int | None = None,
) -> list[dict[str, Any]]:
    """Sync active watchlist providers sequentially; caller must hold ``_sync_lock``."""
    import json

    results: list[dict[str, Any]] = []
    log_process_start(logger, "Provider Sync Engine")
    from app.db.models import Provider

    async with async_session_factory() as session:
        try:
            stmt = (
                select(Provider)
                .where(Provider.category == "watchlist", Provider.is_active.is_(True))
                .order_by(Provider.priority.asc(), Provider.id.asc())
            )
            if only_provider_id is not None:
                stmt = stmt.where(Provider.id == only_provider_id)
            providers_res = await session.execute(stmt)
            provider_ids = [p.id for p in providers_res.scalars().unique().all()]

            any_synced = False
            for prov_id in provider_ids:
                provider = await session.get(Provider, prov_id)
                if provider is None or not getattr(provider, "is_active", True):
                    continue

                result: dict[str, Any] = {
                    "status": "ok",
                    "ingested": 0,
                    "added": 0,
                    "pruned": 0,
                    "error": None,
                }

                if provider.type != "simkl":
                    if provider.watchlist_connection_status != "connected":
                        logger.warning(
                            "Provider %s is not connected, skipping.", provider.name
                        )
                        continue
                    results.append(result)
                    try:
                        await _sync_adapter_provider(session, provider, result)
                        any_synced = True
                    except Exception as prov_err:  # noqa: BLE001
                        await session.rollback()
                        prov_after_rb = await session.get(Provider, prov_id)
                        target = prov_after_rb or provider
                        message = _record_provider_error(target, prov_err)
                        logger.error(
                            "[SYNC] Provider sync failed for %s: %s",
                            provider.name,
                            message,
                        )
                        result.update(status="error", error=message)
                        await session.commit()
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

                results.append(result)
                try:
                    client_id, access_token = await simkl.ensure_valid_simkl_token(
                        session, provider
                    )
                    cfg = provider.simkl_config
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
                        ) = await _fetch_simkl_category_with_retry(
                            session,
                            provider,
                            category,
                            client_id,
                            access_token,
                        )
                        active_simkl_ids.update(
                            _extract_active_simkl_ids(fetched[category])
                        )
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
                        await _sync_items(
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
                        sequels_consolidated = (
                            await consolidate_standalone_anime_sequels(session)
                        )
                        anime_seasons_updated = await refresh_due_future_anime_seasons(
                            session
                        )
                    logger.info(
                        "[SYNC] [3/7] Reconciled anime hierarchies (%d sequels consolidated, %d TBA seasons updated)",
                        sequels_consolidated,
                        anime_seasons_updated,
                    )

                    air_stats = AirDateRefreshStats()
                    await enrich_active_shows_episode_air_dates(
                        session, stats=air_stats
                    )
                    logger.info(
                        "[SYNC] [4/7] Air date refresh: %d episode dates updated across %d titles",
                        air_stats.episodes_updated,
                        air_stats.titles_updated,
                    )

                    rolled = await rollover_due_future_episodes_and_seasons(session)
                    logger.info(
                        "[SYNC] [5/7] Rollover: %d items transitioned to searching",
                        rolled["episodes_rolled_over"] + rolled["seasons_rolled_over"],
                    )

                    tba_stats = TbaDiscoveryStats()
                    await refresh_due_future_tv_seasons(session, stats=tba_stats)
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
                    duplicates_merged = await deduplicate_media_items(session)
                    pruned = await prune_inactive_media_items(session)
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
                    cfg = provider.simkl_config
                    cfg["last_synced_at"] = datetime.now(timezone.utc).isoformat()
                    cfg.pop("last_error", None)
                    provider.config_json = json.dumps(cfg)
                    await session.commit()
                    any_synced = True
                except Exception as prov_err:  # noqa: BLE001
                    await session.rollback()
                    prov_after_rb = await session.get(Provider, prov_id)
                    target = prov_after_rb or provider
                    message = _record_provider_error(target, prov_err)
                    logger.error(
                        "Provider sync failed for %s: %s", provider.name, message
                    )
                    result.update(status="error", error=message)
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
    return results


async def sync_all_providers() -> int:
    """Sync every active watchlist provider sequentially; returns the provider error count."""
    async with _sync_lock:
        results = await _run_provider_sync()
    return sum(1 for r in results if r["status"] == "error")


async def sync_single_provider(provider_id: int) -> dict[str, Any]:
    """Sync one watchlist provider; returns ``status`` ok/error/busy plus counters."""
    if _sync_lock.locked():
        return {"status": "busy", "ingested": 0, "added": 0, "pruned": 0, "error": None}
    async with _sync_lock:
        results = await _run_provider_sync(provider_id)
    if not results:
        return {
            "status": "error",
            "ingested": 0,
            "added": 0,
            "pruned": 0,
            "error": "Provider not found, inactive or not connected.",
        }
    return results[0]


async def enrich_movie_show_metadata(
    session: AsyncSession,
    item: MediaItem,
    tmdb_id: int | None,
    media_type: MediaType,
) -> MediaType:
    """Fetch TMDB metadata (IMDB fallback) for a movie/show item; returns the resolved media type."""
    # Commit any pending dirty state before slow TMDB HTTP calls to release the write lock.
    await session.commit()
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
            translations = details.get("translations", {}).get("translations", [])
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

        release_date = await tmdb.get_digital_release_date(tmdb_id) if tmdb_id else None
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

            translations = details.get("translations", {}).get("translations", [])
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
                        (await session.execute(existing_season_stmt)).scalars().first()
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
            _backfill_item_release_date_from_seasons(item, seasons_iter=synced_seasons)
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

    return media_type


async def _upsert_simkl_source_for_item(
    session: AsyncSession,
    item: MediaItem,
    provider_id: int,
    simkl_id: int,
    item_data: dict[str, Any],
) -> None:
    """Record the Simkl list membership of *item* as a watchlist source row."""
    from app.db.models import Provider
    from app.services.watchlist_ingest import upsert_simkl_source

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
    provider_name: str = "Simkl",
    *,
    stats: IngestStats | None = None,
) -> None:
    from sqlalchemy import and_, or_

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
                    evaluate_simkl_watch_progress_and_advance,
                )

                parent_item.watchlist_synced_at = datetime.now(timezone.utc)
                await _upsert_simkl_source_for_item(
                    session, parent_item, provider_id, simkl_id, item_data
                )
                await evaluate_simkl_watch_progress_and_advance(
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
        from app.core.push_engine import evaluate_simkl_watch_progress_and_advance

        await evaluate_simkl_watch_progress_and_advance(
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
        air_date = parse_broadcast_air_date(air_date_str)

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

    now = datetime.now(timezone.utc)
    original_anilist_id = item.anilist_id
    original_simkl_id = item.simkl_id
    original_mal_id = item.mal_id
    item.add_associated_franchise_id(original_simkl_id)
    if not original_anilist_id:
        item.last_metadata_refreshed_at = now
        return
    hierarchy_data = await anilist.get_anime_root_and_hierarchy(original_anilist_id)

    if not hierarchy_data:
        item.last_metadata_refreshed_at = now
        return

    root_node = hierarchy_data["root"]
    hierarchy_list = hierarchy_data["hierarchy"]

    root_id = root_node.get("id")
    if root_id:
        other_root_stmt = (
            select(MediaItem)
            .where(
                MediaItem.anilist_id == root_id,
                MediaItem.id != item.id,
            )
            .options(selectinload(MediaItem.seasons))
        )
        other_root = (await session.execute(other_root_stmt)).scalars().first()
        if other_root:
            logger.info(
                "    🗑️ Auto-consolidating duplicate franchise MediaItem '%s' (ID %d) into existing root ID %d and reconciling seasons.",
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
            for sid in item.associated_franchise_ids:
                other_root.add_associated_franchise_id(sid)
            if other_root.status == MediaStatus.IGNORED:
                other_root.status = (
                    MediaStatus.FUTURE
                    if is_future_or_tba(other_root.release_date, other_root.year, now)
                    else MediaStatus.SEARCHING
                )
            await session.delete(item)
            await session.flush()
            item = other_root

    was_out_of_order = bool(
        root_node.get("id") and root_node.get("id") != original_anilist_id
    )
    if was_out_of_order and item.anilist_id != root_node.get("id"):
        logger.info(
            "    🔄 Out-of-order sequel detected. Updating MediaItem '%s' to root franchise metadata.",
            item.title,
        )
        item.anilist_id = root_node.get("id")
        item.mal_id = root_node.get("idMal") or item.mal_id
        titles = root_node.get("title", {})
        item.title = titles.get("english") or titles.get("romaji") or item.title

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
        anilist_node: dict[str, Any],
        entry_type: str,
        watch_order: int,
        type_number: int,
    ) -> None:
        ep_count = anilist_node.get("episodes")
        start_date = anilist_node.get("startDate")
        s_air_date = _parse_anilist_start_date(start_date, now)
        start_year = start_date.get("year") if isinstance(start_date, dict) else None

        if s_air_date is not None or start_year is not None or watch_order > 1:
            is_season_future = is_future_or_tba(s_air_date, start_year, now)
        else:
            is_season_future = is_future_or_tba(item.release_date, item.year, now)
        node_id = anilist_node.get("id")
        node_mal_id = anilist_node.get("idMal") or (
            original_mal_id if node_id == original_anilist_id else None
        )
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
                mal_id=int(node_mal_id) if node_mal_id is not None else None,
                simkl_id=original_simkl_id
                if (node_id == original_anilist_id and original_simkl_id)
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
            if node_mal_id is not None and not existing_season.mal_id:
                existing_season.mal_id = int(node_mal_id)
            if resolved_title and not existing_season.title:
                existing_season.title = resolved_title
            if (
                node_id == original_anilist_id
                and original_simkl_id
                and not existing_season.simkl_id
            ):
                existing_season.simkl_id = original_simkl_id
            elif (
                was_out_of_order
                and node_id != original_anilist_id
                and existing_season.simkl_id == original_simkl_id
            ):
                existing_season.simkl_id = None
            existing_season.watch_order = watch_order
            existing_season.type_number = type_number
            existing_season.entry_type = entry_type
            if existing_season.status == SeasonStatus.PENDING and not is_season_future:
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
    item.last_metadata_refreshed_at = now
    await consolidate_standalone_anime_sequels(session)


async def refresh_due_future_anime_seasons(session: AsyncSession) -> int:
    """Refresh FUTURE/TBA/0-episode anime seasons whose last_metadata_refreshed_at is >24h old
    using a single batched AniList Page(media(id_in: ...)) query without graph traversal.
    """
    from datetime import timedelta

    from app.db.models import Episode, EpisodeStatus
    from app.services import anilist

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    stmt = (
        select(MediaItem)
        .where(
            MediaItem.media_type == MediaType.ANIME,
            MediaItem.status != MediaStatus.IGNORED,
            MediaItem.last_metadata_refreshed_at.isnot(None),
        )
        .options(selectinload(MediaItem.seasons).selectinload(Season.episodes))
    )
    items = (await session.execute(stmt)).scalars().unique().all()

    due_items: list[MediaItem] = []
    target_anilist_ids: set[int] = set()

    for item in items:
        refreshed_at = item.last_metadata_refreshed_at
        if refreshed_at is None:
            continue
        if refreshed_at.tzinfo is None:
            refreshed_at = refreshed_at.replace(tzinfo=timezone.utc)
        if refreshed_at > cutoff:
            continue

        item_due_seasons = [
            s
            for s in (item.seasons or [])
            if s.status == SeasonStatus.FUTURE
            or s.air_date is None
            or not s.episode_count
        ]
        if (
            item.status == MediaStatus.FUTURE
            or item.release_date is None
            or item_due_seasons
        ):
            due_items.append(item)
            if item.anilist_id:
                target_anilist_ids.add(int(item.anilist_id))
            for s in item_due_seasons:
                if s.anilist_id:
                    target_anilist_ids.add(int(s.anilist_id))

    if not due_items or not target_anilist_ids:
        return 0

    await session.commit()
    batch_details = await anilist.get_anime_details_batch_by_ids(
        sorted(target_anilist_ids)
    )

    refreshed_count = 0
    for item in due_items:
        item.last_metadata_refreshed_at = now
        refreshed_count += 1
        for s in item.seasons or []:
            if not (
                s.status == SeasonStatus.FUTURE
                or s.air_date is None
                or not s.episode_count
            ):
                continue
            aid = s.anilist_id or item.anilist_id
            if not aid or int(aid) not in batch_details:
                continue
            node = batch_details[int(aid)]
            if node.get("idMal") is not None and not s.mal_id:
                s.mal_id = int(node["idMal"])
            start_date = node.get("startDate")
            s_air_date = _parse_anilist_start_date(start_date, now)
            start_year = (
                start_date.get("year") if isinstance(start_date, dict) else None
            )
            if s_air_date is not None:
                s.air_date = s_air_date
            ep_count = node.get("episodes")
            if ep_count:
                s.episode_count = int(ep_count)
            node_title = node.get("title") or {}
            resolved_title = (
                node_title.get("english")
                or node_title.get("romaji")
                or node_title.get("native")
            )
            if resolved_title and not s.title:
                s.title = resolved_title

            is_season_future = is_future_or_tba(s.air_date, start_year, now)
            if s.status in (
                SeasonStatus.PENDING,
                SeasonStatus.FUTURE,
                SeasonStatus.SEARCHING,
            ):
                s.status = (
                    SeasonStatus.FUTURE if is_season_future else SeasonStatus.SEARCHING
                )

            if ep_count:
                existing_eps = {ep.episode_number: ep for ep in (s.episodes or [])}
                for ep_num in range(1, int(ep_count) + 1):
                    if ep_num in existing_eps:
                        ep_obj = existing_eps[ep_num]
                        if s_air_date and ep_num == 1 and not ep_obj.air_date:
                            ep_obj.air_date = s_air_date
                        if ep_obj.status in (
                            EpisodeStatus.PENDING,
                            EpisodeStatus.FUTURE,
                            EpisodeStatus.SEARCHING,
                        ):
                            ep_obj.status = (
                                EpisodeStatus.FUTURE
                                if is_season_future
                                else EpisodeStatus.SEARCHING
                            )
                    else:
                        session.add(
                            Episode(
                                season_id=s.id,
                                episode_number=ep_num,
                                monitored=True,
                                air_date=s_air_date if ep_num == 1 else None,
                                status=EpisodeStatus.FUTURE
                                if is_season_future
                                else EpisodeStatus.SEARCHING,
                            )
                        )

        _backfill_item_release_date_from_seasons(item, now)
        if item.status in (
            MediaStatus.PENDING,
            MediaStatus.FUTURE,
            MediaStatus.SEARCHING,
        ):
            has_released_season = any(
                s.season_number > 0 and s.status == SeasonStatus.SEARCHING
                for s in (item.seasons or [])
            )
            if has_released_season or not is_future_or_tba(
                item.release_date, item.year, now
            ):
                item.status = MediaStatus.SEARCHING
            else:
                item.status = MediaStatus.FUTURE

    await session.commit()
    return refreshed_count


async def consolidate_standalone_anime_sequels(session: AsyncSession) -> int:
    """Detects standalone MediaItems that are tracked as child Seasons on parent series, and marks them IGNORED."""
    import re

    consolidated = 0

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
                elif season.mal_id and cand.mal_id and season.mal_id == cand.mal_id:
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
                    consolidated += 1
                    for s in cand.seasons:
                        s.status = SeasonStatus.IGNORED

                    for sid in cand.associated_franchise_ids:
                        parent.add_associated_franchise_id(sid)
                    if not season.simkl_id and cand.simkl_id:
                        season.simkl_id = cand.simkl_id
                    if not season.anilist_id and cand.anilist_id:
                        season.anilist_id = cand.anilist_id
                    if not season.mal_id and cand.mal_id:
                        season.mal_id = cand.mal_id
                    if (not season.title or is_generic_season_title) and cand.title:
                        season.title = cand.title

    await session.commit()
    return consolidated


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
        if item.mal_id:
            item.anilist_id = await anilist.get_anime_id_by_mal_id(item.mal_id)
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
            other_root_stmt = (
                select(MediaItem)
                .where(
                    MediaItem.anilist_id == root_id,
                    MediaItem.id != item.id,
                )
                .options(selectinload(MediaItem.seasons).selectinload(Season.episodes))
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
                for sid in item.associated_franchise_ids:
                    other_root.add_associated_franchise_id(sid)
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

        node_mal_id = node.get("idMal")
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
                mal_id=int(node_mal_id) if node_mal_id is not None else None,
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
            if node_mal_id is not None:
                season_obj.mal_id = int(node_mal_id)
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


async def _refresh_future_movie_release_dates(
    session: AsyncSession, movies: list[MediaItem], now: datetime
) -> None:
    """Refresh release_date/status of due FUTURE movies via TMDB or AniList and stamp them."""
    from app.services import anilist

    for movie in movies:
        movie.last_air_dates_refreshed_at = now
        release_date: datetime | None = None
        try:
            if movie.tmdb_id:
                release_date = await tmdb.get_digital_release_date(
                    movie.tmdb_id, session=session
                )
                if not release_date:
                    details = await tmdb.get_movie_details(
                        movie.tmdb_id, session=session
                    )
                    raw = details.get("release_date") if details else None
                    if raw:
                        try:
                            release_date = datetime.strptime(raw, "%Y-%m-%d").replace(
                                tzinfo=timezone.utc
                            )
                        except ValueError:
                            pass
            elif movie.is_anime_movie and movie.anilist_id:
                batch = await anilist.get_anime_details_batch_by_ids(
                    [int(movie.anilist_id)]
                )
                node = batch.get(int(movie.anilist_id)) or {}
                release_date = _parse_anilist_start_date(node.get("startDate"), now)
            else:
                continue
        except Exception as e:
            logger.warning(
                "Error refreshing release date for movie %s: %s", movie.id, e
            )
            continue

        if release_date is None:
            continue
        movie.release_date = release_date
        if not is_future_or_tba(release_date, movie.year, now):
            movie.status = MediaStatus.SEARCHING


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
        Provider,
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

    async def _resolve_simkl_creds(prov: Provider | None) -> tuple[str, str] | None:
        if prov is None or not getattr(prov, "is_active", True) or prov.type != "simkl":
            return None
        cfg = prov.simkl_config
        has_cid = bool(prov.client_id or cfg.get("client_id"))
        has_tok = bool(
            prov.access_token or cfg.get("access_token") or cfg.get("refresh_token")
        )
        if not has_cid or not has_tok:
            return None
        try:
            return await simkl.ensure_valid_simkl_token(session, prov)
        except Exception as e:
            logger.warning(
                "Failed to validate Simkl token for provider %s: %s", prov.id, e
            )
            return None

    total_items = len(items)
    for item_idx, item in enumerate(items, start=1):
        if not item.simkl_id:
            continue

        prov_id = item.provider_id
        if prov_id not in provider_creds:
            provider = item.provider
            if provider is None and prov_id is not None:
                provider = await session.get(Provider, prov_id)
            provider_creds[prov_id] = await _resolve_simkl_creds(provider)

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
                    c = await _resolve_simkl_creds(sp)
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


async def rollover_due_future_episodes_and_seasons(
    session: AsyncSession,
    now: datetime | None = None,
) -> dict[str, int]:
    """Transition past-due FUTURE episodes and seasons to SEARCHING and roll up statuses."""
    from app.core.transfer_poller import (
        _recalculate_parent_status,
        _sync_season_status_from_episodes,
    )
    from app.db.models import Episode

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # 1. Episodes past due
    ep_stmt = (
        select(Episode)
        .where(
            Episode.status == EpisodeStatus.FUTURE,
            Episode.air_date.is_not(None),
        )
        .options(
            selectinload(Episode.season).selectinload(Season.media_item),
        )
    )
    all_future_episodes = list((await session.execute(ep_stmt)).scalars().all())
    due_episodes = [
        ep
        for ep in all_future_episodes
        if ep.air_date is not None and is_episode_aired(ep.air_date, now)
    ]
    for ep in due_episodes:
        ep.status = EpisodeStatus.SEARCHING

    # 2. Seasons past due
    season_stmt = (
        select(Season)
        .where(
            Season.status == SeasonStatus.FUTURE,
            Season.air_date.is_not(None),
            Season.air_date <= now,
        )
        .options(
            selectinload(Season.episodes),
            selectinload(Season.media_item),
            selectinload(Season.download_history),
        )
    )
    due_seasons = list((await session.execute(season_stmt)).scalars().all())

    # 3. Collect unique affected seasons
    affected_seasons_dict: dict[int, Season] = {}
    for s in due_seasons:
        if s.id is not None:
            affected_seasons_dict[s.id] = s

    for ep in due_episodes:
        if ep.season and ep.season.id is not None:
            if ep.season.id not in affected_seasons_dict:
                affected_seasons_dict[ep.season.id] = ep.season

    # 4. Sync each affected season
    for season in affected_seasons_dict.values():
        _sync_season_status_from_episodes(season)
        if (
            season.status == SeasonStatus.FUTURE
            and season.air_date is not None
            and _as_utc(season.air_date) <= now
            and all(ep.status != EpisodeStatus.FUTURE for ep in (season.episodes or []))
        ):
            season.status = SeasonStatus.SEARCHING

    # 5. Collect unique parent MediaItems
    affected_items_dict: dict[int, MediaItem] = {}
    for s in affected_seasons_dict.values():
        if s.media_item and s.media_item.id is not None:
            affected_items_dict[s.media_item.id] = s.media_item

    # 6. Recalculate each affected MediaItem
    for item in affected_items_dict.values():
        await _recalculate_parent_status(session, item)
        if item.status == MediaStatus.FUTURE and not is_future_or_tba(
            item.effective_release_date, item.year, now
        ):
            item.status = MediaStatus.SEARCHING

    episodes_rolled_over = len(due_episodes)
    seasons_rolled_over = sum(
        1 for s in affected_seasons_dict.values() if s.status != SeasonStatus.FUTURE
    )
    items_affected = len(affected_items_dict)

    if episodes_rolled_over > 0 or seasons_rolled_over > 0 or items_affected > 0:
        await session.commit()

    return {
        "episodes_rolled_over": episodes_rolled_over,
        "seasons_rolled_over": seasons_rolled_over,
        "items_affected": items_affected,
    }


async def refresh_due_future_tv_seasons(
    session: AsyncSession,
    now: datetime | None = None,
    *,
    stats: TbaDiscoveryStats | None = None,
) -> int:
    """Discover newly scheduled air dates and newly added episodes for TBA/0-episode FUTURE TV seasons."""
    from app.core.transfer_poller import (
        _recalculate_parent_status,
        _sync_season_status_from_episodes,
    )

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    cutoff = now - timedelta(hours=24)

    stmt = (
        select(Season)
        .join(Season.media_item)
        .where(
            MediaItem.media_type == MediaType.SHOW,
            MediaItem.status != MediaStatus.IGNORED,
            Season.status == SeasonStatus.FUTURE,
            (Season.air_date.is_(None)) | (~Season.episodes.any()),
            (MediaItem.last_metadata_refreshed_at.is_(None))
            | (MediaItem.last_metadata_refreshed_at < cutoff),
        )
        .options(
            selectinload(Season.media_item),
            selectinload(Season.episodes),
            selectinload(Season.download_history),
        )
    )

    seasons = list((await session.execute(stmt)).scalars().all())
    # Count distinct shows
    distinct_shows = {
        s.media_item_id: s.media_item for s in seasons if s.media_item is not None
    }
    logger.debug(
        "[SYNC] [6/7] Evaluating %d TV show(s) with TBA/FUTURE seasons for TMDB updates...",
        len(distinct_shows),
    )
    if stats is not None:
        stats.shows_evaluated = len(distinct_shows)
    if not seasons:
        return 0

    refreshed_count = 0
    affected_items: dict[int, MediaItem] = {}
    show_details_cache: dict[int, dict[str, Any] | None] = {}
    total_shows = len(distinct_shows)
    show_idx_map: dict[int, int] = {
        show_id: idx for idx, show_id in enumerate(distinct_shows.keys(), start=1)
    }

    for season in seasons:
        item = season.media_item
        if not item or not item.tmdb_id:
            continue

        item_idx = show_idx_map.get(item.id, 1) if item.id is not None else 1
        logger.debug(
            "[SYNC] [6/7] (%d/%d) Checking TMDB season updates for '%s'...",
            item_idx,
            total_shows,
            item.title,
        )

        # Season boundary check: verify if the show is known to have this season
        if item.tmdb_id not in show_details_cache:
            try:
                show_details_cache[item.tmdb_id] = await tmdb.get_show_details(
                    item.tmdb_id, session=session
                )
            except Exception:
                show_details_cache[item.tmdb_id] = None

        show_details = show_details_cache[item.tmdb_id]
        if show_details:
            known_seasons = {
                s.get("season_number")
                for s in show_details.get("seasons", [])
                if s.get("season_number") is not None
            }
            num_seasons = show_details.get("number_of_seasons")
            if known_seasons and season.season_number not in known_seasons:
                logger.info(
                    "TMDB show %d does not have season %d yet (known seasons up to %s); skipping.",
                    item.tmdb_id,
                    season.season_number,
                    num_seasons,
                )
                item.last_metadata_refreshed_at = now
                if item.id is not None:
                    affected_items[item.id] = item
                continue

        season_details = await tmdb.get_season_details(
            item.tmdb_id, season.season_number, session=session
        )
        if not season_details:
            item.last_metadata_refreshed_at = now
            if item.id is not None:
                affected_items[item.id] = item
            continue

        await _sync_season_episodes(
            session, season, tmdb_id=item.tmdb_id, force_refresh=True
        )
        await session.flush()
        await session.refresh(season, ["episodes"])

        air_date_str = season_details.get("air_date")
        if air_date_str:
            try:
                season.air_date = datetime.strptime(air_date_str, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                pass

        _sync_season_status_from_episodes(season)

        item.last_metadata_refreshed_at = now
        if item.id is not None:
            affected_items[item.id] = item
        refreshed_count += 1

    for item in affected_items.values():
        await _recalculate_parent_status(session, item)

    if refreshed_count > 0 or affected_items:
        await session.commit()

    if stats is not None:
        stats.seasons_discovered = refreshed_count
    return refreshed_count
