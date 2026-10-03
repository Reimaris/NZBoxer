"""
Auto-Wake / Auto-Sleep Transfer Poller & Split Failure Recovery (v3.0.0)
========================================================================
Monitors active TorBox transfers on a 20s cadence ONLY when at least one entity
is in `DOWNLOADING` status, returning to a zero-API sleep state once all active
transfers complete or fail.

Responsibilities:
1. Auto-Wake / Auto-Sleep lifecycle (`transfer_poller.wake()` / `transfer_poller.sleep()`).
2. Live progress tracking (`progress_pct`, `download_speed_bytes`, `eta_seconds`, `status_detail`)
   on active `DownloadHistory` rows.
3. Layer 3 Playable Video Verification (`is_torbox_filelist_fake`) with immediate
   Tick-1 rejection for archive-only (`RAR`/`PAR2`) or executable payloads,
   transitioning verified transfers to `COMPLETED` and dispatching `Ready on TorBox` notifications.
4. Split Failure Recovery:
   - `push_mode == "auto"`: Deletes broken transfer from TorBox, blacklists the release,
     and automatically searches & pushes the next-best non-blacklisted candidate.
   - `push_mode == "manual"`: Deletes broken transfer from TorBox, blacklists the release,
     and transitions the entity to `FAILED` (preserving the row in Active Pushes until
     the user clicks `[Re-Search & Pick]` or `[Dismiss]`).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, func, select
from sqlalchemy.orm import selectinload

from app.core.failure_logger import log_failure
from app.core.fake_detector import is_torbox_filelist_fake
from app.core.parser import build_release_feature_pills
from app.db.database import async_session_factory
from app.db.models import (
    BlacklistedRelease,
    DownloadHistory,
    Episode,
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    MediaType,
    Season,
    SeasonStatus,
    SystemSettings,
)
from app.services import discord, torbox

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

NOT_FOUND_GRACE_PERIOD_SECONDS = 900

_poller_lock = asyncio.Lock()


class TransferPollerState:
    """In-memory runtime state for the Auto-Wake / Auto-Sleep Transfer Poller."""

    def __init__(self) -> None:
        self.is_awake: bool = False
        self.last_polled_at: datetime | None = None
        self.active_count: int = 0
        self._scheduler: Any | None = None
        # Maps item_id -> human-readable item title currently being searched/dispatched in background
        self.in_flight_pushes: dict[int, str] = {}

    @property
    def last_tick_at(self) -> datetime | None:
        return self.last_polled_at

    @last_tick_at.setter
    def last_tick_at(self, val: datetime | None) -> None:
        self.last_polled_at = val

    def set_scheduler(self, scheduler: Any) -> None:
        self._scheduler = scheduler

    def register_in_flight_push(self, item_id: int, title: str) -> None:
        self.in_flight_pushes[item_id] = title
        self.wake()

    def unregister_in_flight_push(self, item_id: int) -> None:
        self.in_flight_pushes.pop(item_id, None)

    @property
    def has_in_flight_pushes(self) -> bool:
        return len(self.in_flight_pushes) > 0

    @property
    def in_flight_titles(self) -> list[str]:
        return list(self.in_flight_pushes.values())

    def wake(self) -> None:
        was_awake = self.is_awake
        self.is_awake = True
        if self._scheduler is not None:
            try:
                job = self._scheduler.get_job("transfer_poller_job")
                if job is not None and job.next_run_time is None:
                    self._scheduler.resume_job("transfer_poller_job")
                    logger.info("⚡ Transfer Poller resumed in APScheduler.")
            except Exception as exc:
                logger.warning("Could not resume transfer_poller_job: %s", exc)
        if not was_awake:
            logger.info(
                "⚡ Transfer Poller woken up — active downloads or in-flight pushes detected."
            )

    def sleep(self, active_count: int = 0) -> None:
        if self.has_in_flight_pushes or active_count > 0:
            # Never sleep while a background Auto-Push is still querying indexers / uploading NZBs
            self.is_awake = True
            self.active_count = active_count
            return
        if self.is_awake:
            logger.info(
                "💤 Transfer Poller going to sleep — 0 active downloads remaining."
            )
        self.is_awake = False
        self.active_count = active_count
        if self._scheduler is not None:
            try:
                job = self._scheduler.get_job("transfer_poller_job")
                if job is not None and job.next_run_time is not None:
                    self._scheduler.pause_job("transfer_poller_job")
                    logger.info(
                        "💤 Transfer Poller paused in APScheduler (zero idle CPU/DB usage)."
                    )
            except Exception as exc:
                logger.warning("Could not pause transfer_poller_job: %s", exc)


transfer_poller = TransferPollerState()


def wake_transfer_poller() -> None:
    """Wake the transfer poller when a new push is dispatched to TorBox."""
    transfer_poller.wake()


async def count_downloading_entities(session: AsyncSession) -> int:
    """Count all active downloading push targets across Movies, Season Packs, and Episodes.

    Season rows in DOWNLOADING state are only counted when they have zero child Episode
    rows in DOWNLOADING state (true season-pack downloads), ensuring a single downloading
    episode reports DOWNLOADING: 1 rather than 2.
    """
    movies_c = (
        await session.execute(
            select(func.count(MediaItem.id)).where(
                MediaItem.status == MediaStatus.DOWNLOADING,
                MediaItem.media_type == MediaType.MOVIE,
            )
        )
    ).scalar() or 0

    seasons_c = (
        await session.execute(
            select(func.count(Season.id)).where(
                Season.status == SeasonStatus.DOWNLOADING,
                ~exists(
                    select(Episode.id).where(
                        Episode.season_id == Season.id,
                        Episode.status == EpisodeStatus.DOWNLOADING,
                    )
                ),
            )
        )
    ).scalar() or 0

    episodes_c = (
        await session.execute(
            select(func.count(Episode.id)).where(
                Episode.status == EpisodeStatus.DOWNLOADING
            )
        )
    ).scalar() or 0

    return int(movies_c + seasons_c + episodes_c)


def _normalize_progress_pct(raw_progress: Any, status_lower: str) -> float:
    """Normalize TorBox progress (either 0.0..1.0 or 0.0..100.0) into 0.0..100.0 percentage."""
    if status_lower in ("completed", "cached"):
        return 100.0
    try:
        val = float(raw_progress or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if val < 0.0:
        return 0.0
    if 0.0 < val <= 1.0:
        return round(val * 100.0, 1)
    return round(min(val, 100.0), 1)


def _is_terminal_failure_state(status_lower: str) -> bool:
    """Check if a TorBox status string represents a permanent terminal failure."""
    if not status_lower:
        return False
    return (
        status_lower.startswith("failed")
        or status_lower.startswith("error")
        or "aborted" in status_lower
        or "repair failed" in status_lower
        or "not-complete" in status_lower
        or status_lower
        in (
            "cannot be re-completed",
            "not enough repair blocks",
            "not_found",
        )
    )


async def _delete_and_purge_torbox_transfer(
    session: AsyncSession,
    torbox_id: str | None,
    tb_item: dict[str, Any] | None = None,
) -> None:
    """Delete a failed/canceled transfer from TorBox."""
    if not torbox_id:
        return
    tb_type = tb_item.get("_type", "usenet") if tb_item else "usenet"
    parsed_id: Any = int(torbox_id) if str(torbox_id).isdigit() else torbox_id
    try:
        if tb_type == "torrent":
            await torbox.delete_torrent_download(parsed_id, session=session)
        else:
            await torbox.delete_usenet_download(parsed_id, session=session)
    except Exception as exc:
        logger.warning(
            "Failed to delete %s %s from TorBox: %s", tb_type, torbox_id, exc
        )


async def _auto_replace_failed_push(
    session: AsyncSession,
    item: MediaItem,
    season: Season | None = None,
    episode: Episode | None = None,
) -> DownloadHistory | None:
    """Search and push the next-best non-blacklisted release candidate for a failed Auto-Push."""
    from sqlalchemy import inspect as sa_inspect

    from app.core.push_engine import (
        _dispatch_candidate_list_to_torbox,
        _load_blacklisted_sets,
        _push_single_season_or_movie_entry,
        _query_movie_across_indexers,
        _query_show_across_indexers,
        _score_and_partition_candidates,
        resolve_effective_search_config,
    )

    effective_cfg = await resolve_effective_search_config(session, item)
    blacklisted_guids, blacklisted_titles = await _load_blacklisted_sets(
        session, item.id
    )

    item_media_type = (
        item.media_type.value
        if hasattr(item.media_type, "value")
        else str(item.media_type or "")
    )

    if episode is not None and season is not None:
        effective_s_num = int(season.type_number or season.season_number)
        raw_results = await _query_show_across_indexers(
            session,
            item,
            season_number=effective_s_num,
            episode_number=episode.episode_number,
        )
        partitioned = _score_and_partition_candidates(
            raw_results=raw_results,
            blacklisted_guids=blacklisted_guids,
            blacklisted_titles=blacklisted_titles,
            expected_title=item.title,
            expected_year=None,
            expected_alt_title=item.alt_title,
            expected_season=effective_s_num,
            expected_episode=episode.episode_number,
            expected_season_title=(
                season.title if season.title and season.title != item.title else None
            ),
            runtime_minutes=item.runtime_minutes,
            effective_cfg=effective_cfg,
            media_type=item_media_type,
            season_episode_count=season.episode_count,
        )
        return await _dispatch_candidate_list_to_torbox(
            session,
            partitioned["all_valid"],
            item=item,
            season=season,
            episode=episode,
            push_mode="auto",
            event_type="push_initiated",
            reset_fail_count=False,
        )

    if season is not None:
        entry_type = getattr(season, "entry_type", "season") or "season"
        if entry_type != "movie":
            if "episodes" in sa_inspect(season).unloaded:
                await session.refresh(season, ["episodes"])
            # Revert episodes that were marked DOWNLOADING solely by the failed Season Pack
            for ep in season.episodes:
                if ep.status == EpisodeStatus.DOWNLOADING:
                    ep.status = EpisodeStatus.SEARCHING
            await session.commit()

        hists = await _push_single_season_or_movie_entry(
            session=session,
            item=item,
            season=season,
            effective_cfg=effective_cfg,
            blacklisted_guids=blacklisted_guids,
            blacklisted_titles=blacklisted_titles,
            is_auto_advance=False,
            reset_fail_count=False,
        )
        return hists[0] if hists else None

    # Standalone Movie
    raw_results = await _query_movie_across_indexers(session, item)
    partitioned = _score_and_partition_candidates(
        raw_results=raw_results,
        blacklisted_guids=blacklisted_guids,
        blacklisted_titles=blacklisted_titles,
        expected_title=item.title,
        expected_year=item.year,
        expected_alt_title=item.alt_title,
        expected_season=None,
        expected_episode=None,
        expected_season_title=None,
        runtime_minutes=item.runtime_minutes,
        effective_cfg=effective_cfg,
        media_type="movie",
    )
    return await _dispatch_candidate_list_to_torbox(
        session,
        partitioned["all_valid"],
        item=item,
        season=None,
        episode=None,
        push_mode="auto",
        event_type="push_initiated",
        reset_fail_count=False,
    )


def _match_torbox_transfer(
    history: DownloadHistory, tb_map: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any] | None]:
    """Match a DownloadHistory row against TorBox's transfer dictionary by ID, hash, or title."""
    tb_item: dict[str, Any] | None = None
    if history.torbox_id:
        tb_item = tb_map.get(str(history.torbox_id))

    if tb_item is None and history.torbox_hash:
        for cand in tb_map.values():
            if str(cand.get("hash") or "").lower() == history.torbox_hash.lower():
                history.torbox_id = str(cand.get("id"))
                tb_item = cand
                break

    if tb_item is None and history.nzb_title:
        for cand in tb_map.values():
            tb_name = str(cand.get("name") or cand.get("title") or "")
            if tb_name and (
                history.nzb_title.lower() in tb_name.lower()
                or tb_name.lower() in history.nzb_title.lower()
            ):
                history.torbox_id = str(cand.get("id"))
                tb_item = cand
                break

    if tb_item is None:
        if not history.torbox_id:
            return "downloading", None
        now_utc = datetime.now(timezone.utc)
        sent_at = history.torbox_sent_at
        if sent_at and sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        if (
            sent_at
            and (now_utc - sent_at).total_seconds() < NOT_FOUND_GRACE_PERIOD_SECONDS
        ):
            return "downloading", None
        return "not_found", None

    return str(tb_item.get("download_state") or "downloading"), tb_item


def _is_history_terminal(status_detail: str | None) -> bool:
    detail = (status_detail or "").strip().lower()
    return detail in (
        "completed",
        "deleted",
        "expired",
        "replaced",
        "canceled",
    ) or detail.startswith("failed")


def _sync_season_status_from_episodes(season: Season | None) -> None:
    """Roll up Episode statuses to Season.status across any non-future season with episodes."""
    if season is None or season.status in (SeasonStatus.FUTURE, SeasonStatus.IGNORED):
        return
    from sqlalchemy import inspect as sa_inspect

    hist_list = (
        list(season.download_history or [])
        if "download_history" not in sa_inspect(season).unloaded
        else []
    )
    has_active_pack = any(
        not h.is_dismissed
        and bool(h.torbox_id or h.torbox_hash)
        and h.episode_id is None
        and not _is_history_terminal(h.status_detail)
        for h in hist_list
    )
    if has_active_pack:
        season.status = SeasonStatus.DOWNLOADING
        return

    eps = (
        list(season.episodes or [])
        if "episodes" not in sa_inspect(season).unloaded
        else []
    )
    if not eps:
        return

    if any(ep.status == EpisodeStatus.DOWNLOADING for ep in eps):
        season.status = SeasonStatus.DOWNLOADING
        return

    if any(ep.status == EpisodeStatus.FAILED for ep in eps):
        season.status = SeasonStatus.FAILED
        return

    non_future_eps = [ep for ep in eps if ep.status != EpisodeStatus.FUTURE]
    if non_future_eps and all(
        ep.status in (EpisodeStatus.COMPLETED, EpisodeStatus.DOWNLOADED)
        for ep in non_future_eps
    ):
        season.status = SeasonStatus.COMPLETED
    elif season.status in (
        SeasonStatus.DOWNLOADING,
        SeasonStatus.COMPLETED,
        SeasonStatus.DOWNLOADED,
        SeasonStatus.PENDING,
    ):
        season.status = SeasonStatus.SEARCHING


async def _count_active_transfers_for_item(
    session: AsyncSession, media_item_id: int
) -> int:
    """Count active non-terminal transfers for a specific MediaItem."""
    stmt = (
        select(DownloadHistory)
        .where(
            DownloadHistory.media_item_id == media_item_id,
            DownloadHistory.is_dismissed.is_(False),
        )
        .options(
            selectinload(DownloadHistory.media_item),
            selectinload(DownloadHistory.season),
            selectinload(DownloadHistory.episode),
        )
    )
    rows = (await session.execute(stmt)).scalars().all()
    active = 0
    for h in rows:
        if _is_history_terminal(h.status_detail):
            continue
        if h.episode is not None:
            if h.episode.status == EpisodeStatus.DOWNLOADING:
                active += 1
        elif h.season is not None:
            if h.season.status == SeasonStatus.DOWNLOADING:
                active += 1
        elif h.media_item is not None:
            if h.media_item.status == MediaStatus.DOWNLOADING:
                active += 1
    return active


def _build_failure_reason_for_history(
    history: DownloadHistory, sh_max_retries: int
) -> str:
    detail = (history.status_detail or "").strip()
    raw_reason = (
        detail[len("failed: ") :].strip()
        if detail.lower().startswith("failed:")
        else (detail or "TorBox transfer failed")
    )
    push_mode = (history.push_mode or "auto").strip().lower()
    if push_mode == "manual":
        return f"Manual pick failed ({raw_reason}). Click Re-Search & Pick in Active Pushes."

    target: Any = history.episode or history.season or history.media_item
    fail_count = int(getattr(target, "fail_count", 0) or 0) if target is not None else 0
    if fail_count > sh_max_retries:
        return (
            f"Auto-push failed ({raw_reason}) and max replacement attempts "
            f"({sh_max_retries}) were exhausted."
        )
    return f"Push failed ({raw_reason}) and no replacement candidates were found."


async def _dispatch_settled_show_notifications(
    session: AsyncSession,
    affected_items: dict[int, MediaItem],
    sh_max_retries: int,
) -> None:
    """For each affected MediaItem that now has 0 active downloading transfers,
    dispatch at most 1 consolidated Ready on TorBox and/or 1 consolidated Failure notification.
    """
    for item_id, item in affected_items.items():
        remaining_for_item = await _count_active_transfers_for_item(session, item_id)
        if remaining_for_item > 0:
            continue

        stmt = (
            select(DownloadHistory)
            .where(
                DownloadHistory.media_item_id == item_id,
                DownloadHistory.is_dismissed.is_(False),
                DownloadHistory.notification_sent.is_(False),
            )
            .options(
                selectinload(DownloadHistory.media_item),
                selectinload(DownloadHistory.season),
                selectinload(DownloadHistory.episode).selectinload(Episode.season),
            )
            .order_by(DownloadHistory.id.asc())
        )
        unnotified_rows = list((await session.execute(stmt)).scalars().all())
        if not unnotified_rows:
            continue

        completed_rows = [
            h
            for h in unnotified_rows
            if (h.status_detail or "").strip().lower() == "completed"
        ]
        failed_rows = [
            h
            for h in unnotified_rows
            if (h.status_detail or "").strip().lower().startswith("failed")
        ]
        if not completed_rows and not failed_rows:
            continue

        # Commit notification_sent = True BEFORE outbound webhook HTTP calls
        for h in completed_rows + failed_rows:
            h.notification_sent = True
        await session.commit()

        if completed_rows:
            await discord.dispatch_batch_notification_event(
                session=session,
                event_type="completed",
                histories=completed_rows,
                item=item,
                reason="Ready on TorBox (Verified Playable Video)",
            )

        if failed_rows:
            if len(failed_rows) == 1:
                fail_reason = _build_failure_reason_for_history(
                    failed_rows[0], sh_max_retries=sh_max_retries
                )
            else:
                first_reason = _build_failure_reason_for_history(
                    failed_rows[0], sh_max_retries=sh_max_retries
                )
                fail_reason = f"{len(failed_rows)} transfers failed ({first_reason})"
            await discord.dispatch_batch_notification_event(
                session=session,
                event_type="failure",
                histories=failed_rows,
                item=item,
                reason=fail_reason,
            )


async def run_transfer_poller_tick(
    session: AsyncSession | None = None,
    pre_fetched_downloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute a single tick of the Auto-Wake / Auto-Sleep Transfer Poller.

    Guarded by `_poller_lock` so concurrent invocations from the 8s HTMX active-pushes
    endpoint and the 20s APScheduler job never overlap.
    """
    async with _poller_lock:
        if session is None:
            async with async_session_factory() as owned_session:
                return await _run_transfer_poller_tick_with_session(
                    owned_session, pre_fetched_downloads=pre_fetched_downloads
                )
        return await _run_transfer_poller_tick_with_session(
            session, pre_fetched_downloads=pre_fetched_downloads
        )


ACTIVE_POLLABLE_STATUS_DETAILS = (
    "downloading",
    "queued",
    "processing",
    "unpacking",
    "verifying",
    "pending",
    "",
)


async def _run_transfer_poller_tick_with_session(
    session: AsyncSession,
    pre_fetched_downloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    active_count = await count_downloading_entities(session)
    if active_count == 0:
        transfer_poller.sleep(0)
        return {
            "polled": False,
            "active_count": 0,
            "remaining_active": 0,
            "sleeping": not transfer_poller.is_awake,
        }

    transfer_poller.wake()
    transfer_poller.last_polled_at = datetime.now(timezone.utc)

    if pre_fetched_downloads is None:
        raw_downloads = await torbox.get_usenet_downloads(
            bypass_cache=True, session=session
        )
    else:
        raw_downloads = pre_fetched_downloads

    tb_map: dict[str, dict[str, Any]] = {}
    for d in raw_downloads:
        d.setdefault("_type", "usenet")
        if d.get("id") is not None:
            tb_map[str(d["id"])] = d

    if tb_map:
        await reconcile_completed_history_with_torbox(session, set(tb_map.keys()))

    sys_settings = (
        (await session.execute(select(SystemSettings).where(SystemSettings.id == 1)))
        .scalars()
        .first()
    )
    download_timeout_hours = sys_settings.download_timeout_hours if sys_settings else 24
    sh_max_retries = (
        sys_settings.sh_max_retries
        if sys_settings and sys_settings.sh_max_retries is not None
        else 3
    )

    # Load active downloading Movies, Seasons, and Episodes
    movies = (
        (
            await session.execute(
                select(MediaItem)
                .where(
                    MediaItem.status == MediaStatus.DOWNLOADING,
                    MediaItem.media_type == MediaType.MOVIE,
                )
                .options(
                    selectinload(MediaItem.download_history),
                    selectinload(MediaItem.provider),
                )
            )
        )
        .scalars()
        .all()
    )

    seasons = (
        (
            await session.execute(
                select(Season)
                .where(Season.status == SeasonStatus.DOWNLOADING)
                .options(
                    selectinload(Season.download_history),
                    selectinload(Season.episodes),
                    selectinload(Season.media_item).selectinload(MediaItem.provider),
                    selectinload(Season.media_item).selectinload(MediaItem.seasons),
                )
            )
        )
        .scalars()
        .all()
    )

    episodes = (
        (
            await session.execute(
                select(Episode)
                .where(Episode.status == EpisodeStatus.DOWNLOADING)
                .options(
                    selectinload(Episode.download_history),
                    selectinload(Episode.season).selectinload(Season.episodes),
                    selectinload(Episode.season).selectinload(Season.download_history),
                    selectinload(Episode.season)
                    .selectinload(Season.media_item)
                    .selectinload(MediaItem.provider),
                    selectinload(Episode.season)
                    .selectinload(Season.media_item)
                    .selectinload(MediaItem.seasons),
                )
            )
        )
        .scalars()
        .all()
    )

    # Collect unique (target, history, item, season, episode) tuples to evaluate
    targets_to_evaluate: list[
        tuple[
            MediaItem | Season | Episode,
            DownloadHistory,
            MediaItem,
            Season | None,
            Episode | None,
        ]
    ] = []

    def _sent_at_utc(h: DownloadHistory) -> datetime:
        dt = h.torbox_sent_at
        if dt is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    def _latest_history(
        histories: list[DownloadHistory],
        require_episode_none: bool = False,
    ) -> DownloadHistory | None:
        candidates = [
            h
            for h in histories
            if not h.is_dismissed
            and bool(h.torbox_id or h.torbox_hash)
            and (h.status_detail or "").strip().lower()
            in ACTIVE_POLLABLE_STATUS_DETAILS
            and (not require_episode_none or h.episode_id is None)
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda h: (
                _sent_at_utc(h),
                h.id or 0,
            ),
        )

    affected_items: dict[int, MediaItem] = {}

    for m in movies:
        hist = _latest_history(m.download_history)
        if hist is not None:
            targets_to_evaluate.append((m, hist, m, None, None))
        elif not transfer_poller.has_in_flight_pushes and m.id is not None:
            affected_items[m.id] = m

    for s in seasons:
        hist = _latest_history(s.download_history, require_episode_none=True)
        if hist is not None:
            targets_to_evaluate.append((s, hist, s.media_item, s, None))
        elif (
            not transfer_poller.has_in_flight_pushes
            and s.media_item is not None
            and s.media_item.id is not None
        ):
            affected_items[s.media_item.id] = s.media_item

    for e in episodes:
        hist = _latest_history(e.download_history)
        if hist is not None:
            targets_to_evaluate.append((e, hist, e.season.media_item, e.season, e))
        elif (
            not transfer_poller.has_in_flight_pushes
            and e.season is not None
            and e.season.media_item is not None
            and e.season.media_item.id is not None
        ):
            affected_items[e.season.media_item.id] = e.season.media_item

    now_utc = datetime.now(timezone.utc)

    for target, history, item, season, episode in targets_to_evaluate:
        if item.id is not None:
            affected_items[item.id] = item

        raw_status, tb_item = _match_torbox_transfer(history, tb_map)
        status_lower = raw_status.lower()

        sent_at = history.torbox_sent_at
        if sent_at and sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        elapsed_seconds = (now_utc - sent_at).total_seconds() if sent_at else 0.0

        # Update live progress metrics from TorBox snapshot if available
        if tb_item is not None:
            history.progress_pct = _normalize_progress_pct(
                tb_item.get("progress"), status_lower
            )
            history.download_speed_bytes = int(
                tb_item.get("download_speed") or tb_item.get("speed") or 0
            )
            raw_eta = tb_item.get("eta")
            if raw_eta is not None:
                try:
                    history.eta_seconds = max(0, int(raw_eta))
                except (TypeError, ValueError):
                    history.eta_seconds = None
            else:
                history.eta_seconds = None

        # 1. Check Completed / Cached -> Immediate Layer 3 Playable Video Verification
        is_completed = status_lower in ("completed", "cached", "paused") or bool(
            tb_item and tb_item.get("download_finished") is True
        )
        if is_completed and tb_item is not None:
            raw_files = tb_item.get("files")
            tb_files: list[Any] = (
                raw_files
                if isinstance(raw_files, list)
                else [{"name": tb_item.get("name") or tb_item.get("title") or ""}]
            )
            is_movie = episode is None and (
                season is None or getattr(season, "entry_type", "season") == "movie"
            )
            media_type_str = "movie" if is_movie else "episode"
            is_fake, fake_reason = is_torbox_filelist_fake(
                tb_files, media_type=media_type_str
            )

            if is_fake:
                reason = (
                    "Layer 3 Fake Detection: Archive/RAR-only without playable video"
                )
                await _handle_transfer_failure(
                    session=session,
                    target=target,
                    history=history,
                    item=item,
                    season=season,
                    episode=episode,
                    tb_item=tb_item,
                    reason=reason,
                    error_type="unextracted_archive",
                    log_msg=f"{reason} ({fake_reason})",
                    sh_max_retries=sh_max_retries,
                )
                continue

            # Verified Playable Video! Transition immediately to COMPLETED
            history.progress_pct = 100.0
            history.download_speed_bytes = 0
            history.eta_seconds = 0
            history.status_detail = "completed"

            if isinstance(target, Episode):
                target.status = EpisodeStatus.COMPLETED
                target.fail_count = 0
                target.last_error = None
                _sync_season_status_from_episodes(season)
            elif isinstance(target, Season):
                target.status = SeasonStatus.COMPLETED
                target.fail_count = 0
                target.last_error = None
                for ep in target.episodes:
                    if ep.status != EpisodeStatus.FUTURE:
                        ep.status = EpisodeStatus.COMPLETED
                        ep.fail_count = 0
                        ep.last_error = None
            else:
                target.status = MediaStatus.COMPLETED
                target.fail_count = 0
                target.last_error = None
            continue

        # 1b. Check Expired (Previously completed transfer whose cloud files aged out on TorBox)
        if status_lower == "expired" or "expired" in status_lower:
            history.progress_pct = 100.0
            history.download_speed_bytes = 0
            history.eta_seconds = 0
            history.status_detail = "deleted"
            history.notification_sent = True

            if isinstance(target, Episode):
                target.status = EpisodeStatus.COMPLETED
                target.fail_count = 0
                target.last_error = None
                _sync_season_status_from_episodes(season)
            elif isinstance(target, Season):
                target.status = SeasonStatus.COMPLETED
                target.fail_count = 0
                target.last_error = None
                for ep in target.episodes:
                    if ep.status != EpisodeStatus.FUTURE:
                        ep.status = EpisodeStatus.COMPLETED
                        ep.fail_count = 0
                        ep.last_error = None
            else:
                target.status = MediaStatus.COMPLETED
                target.fail_count = 0
                target.last_error = None
            continue

        # 2. Check Stalled Timeout or Terminal Failure States
        is_stalled = (
            status_lower in ("downloading", "processing", "queued", "unknown")
            and sent_at is not None
            and elapsed_seconds > download_timeout_hours * 3600
        )
        if is_stalled or _is_terminal_failure_state(status_lower):
            if is_stalled:
                reason = (
                    f"Stalled download exceeded timeout ({download_timeout_hours}h)"
                )
                log_msg = f"TorBox download stalled past {download_timeout_hours}h limit ({int(elapsed_seconds // 3600)}h elapsed)"
            elif status_lower == "not_found":
                reason = "TorBox download missing (not_found)"
                log_msg = "TorBox download missing past 15m grace window (not_found)"
            else:
                reason = f"TorBox reported status: {raw_status}"
                log_msg = f"TorBox download failed: {raw_status}"

            await _handle_transfer_failure(
                session=session,
                target=target,
                history=history,
                item=item,
                season=season,
                episode=episode,
                tb_item=tb_item,
                reason=reason,
                error_type="torbox_error",
                log_msg=log_msg,
                sh_max_retries=sh_max_retries,
            )
            continue

        # 3. Still actively downloading / processing
        history.status_detail = status_lower or "downloading"

    for aff_item in affected_items.values():
        await _recalculate_parent_status(session, aff_item)

    await session.commit()

    # Dispatch show-level batch notifications for any show/movie whose active transfers have all settled
    await _dispatch_settled_show_notifications(
        session=session,
        affected_items=affected_items,
        sh_max_retries=sh_max_retries,
    )

    remaining_active = await count_downloading_entities(session)
    if remaining_active == 0:
        transfer_poller.sleep(0)

    return {
        "polled": True,
        "active_count": active_count,
        "remaining_active": remaining_active,
        "sleeping": not transfer_poller.is_awake,
    }


async def _handle_transfer_failure(
    session: AsyncSession,
    target: MediaItem | Season | Episode,
    history: DownloadHistory,
    item: MediaItem,
    season: Season | None,
    episode: Episode | None,
    tb_item: dict[str, Any] | None,
    reason: str,
    error_type: str,
    log_msg: str,
    sh_max_retries: int = 3,
) -> None:
    """Execute Split Failure Recovery (`auto` vs `manual`) with bounded retry budget (`sh_max_retries`)."""
    log_failure(session, target, error_type, log_msg)

    bl = BlacklistedRelease(
        media_item_id=item.id,
        nzb_guid=history.nzb_guid,
        nzb_title=history.nzb_title,
        reason=reason,
    )
    session.add(bl)
    # Commit blacklist and failure log before outbound TorBox delete HTTP call
    await session.commit()

    await _delete_and_purge_torbox_transfer(session, history.torbox_id, tb_item)

    push_mode = (history.push_mode or "auto").strip().lower()

    if push_mode == "manual":
        # Manual Pick Failure: do NOT auto-replace; mark entity FAILED and keep row in Active Pushes
        history.download_speed_bytes = 0
        history.eta_seconds = None
        history.status_detail = f"failed: {reason}"
        history.is_dismissed = False

        if isinstance(target, Episode):
            target.status = EpisodeStatus.FAILED
            target.last_error = reason
            _sync_season_status_from_episodes(season)
        elif isinstance(target, Season):
            target.status = SeasonStatus.FAILED
            target.last_error = reason
        else:
            target.status = MediaStatus.FAILED
            target.last_error = reason

        await session.commit()
        return

    # Auto-Push Failure (`push_mode == "auto"`): increment target.fail_count and check retry budget
    prev_auto_replaced = int(getattr(history, "auto_replaced_count", 0) or 0)
    target.fail_count = int(target.fail_count or 0) + 1
    await session.commit()

    replacement_hist: DownloadHistory | None = None
    if target.fail_count <= sh_max_retries:
        replacement_hist = await _auto_replace_failed_push(
            session, item=item, season=season, episode=episode
        )

    if replacement_hist is not None:
        replacement_hist.auto_replaced_count = prev_auto_replaced + 1
        history.status_detail = "replaced"
        history.is_dismissed = True
        history.notification_sent = True
        history.download_speed_bytes = 0
        history.eta_seconds = None
        await session.commit()
        # Intermediate auto_replaced notifications are intentionally suppressed;
        # the final settled notification will summarize any auto-replacements.
        return

    # Budget exhausted (`target.fail_count > sh_max_retries`) or zero replacement candidates found:
    # Stop auto-replacing, transition target to FAILED, keep history row in Active Pushes (`is_dismissed = False`).
    history.download_speed_bytes = 0
    history.eta_seconds = None
    history.status_detail = f"failed: {reason}"
    history.is_dismissed = False

    if isinstance(target, Episode):
        target.status = EpisodeStatus.FAILED
        target.last_error = reason
        _sync_season_status_from_episodes(season)
    elif isinstance(target, Season):
        target.status = SeasonStatus.FAILED
        target.last_error = reason
        for ep in target.episodes:
            if ep.status == EpisodeStatus.DOWNLOADING:
                ep.status = EpisodeStatus.FAILED
                ep.last_error = reason
    else:
        target.status = MediaStatus.FAILED
        target.last_error = reason

    await session.commit()


def _format_speed_and_eta(speed_bytes: int, eta_seconds: int | None) -> str:
    if speed_bytes <= 0 and not eta_seconds:
        return "—"
    speed_mb = speed_bytes / (1024 * 1024)
    speed_str = f"{speed_mb:.1f} MB/s" if speed_bytes > 0 else "0.0 MB/s"
    if eta_seconds is None or eta_seconds <= 0:
        return speed_str
    mins, secs = divmod(int(eta_seconds), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        eta_str = f"{hours}h {mins}m"
    elif mins > 0:
        eta_str = f"{mins}m {secs}s"
    else:
        eta_str = f"{secs}s"
    return f"{speed_str} • {eta_str}"


async def get_active_pushes(session: AsyncSession) -> list[dict[str, Any]]:
    """Return flat list of active transfers (`DOWNLOADING`) and unacknowledged failed pushes (`FAILED` & `is_dismissed == False`)."""
    stmt = (
        select(DownloadHistory)
        .where(DownloadHistory.is_dismissed.is_(False))
        .options(
            selectinload(DownloadHistory.media_item),
            selectinload(DownloadHistory.season),
            selectinload(DownloadHistory.episode),
        )
        .order_by(DownloadHistory.torbox_sent_at.desc(), DownloadHistory.id.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()

    active_rows: list[dict[str, Any]] = []
    seen_targets: set[tuple[int, int | None, int | None]] = set()

    for h in rows:
        item = h.media_item
        if item is None:
            continue
        season = h.season
        episode = h.episode

        # Determine current entity status
        if episode is not None:
            entity_status = episode.status.value
        elif season is not None:
            entity_status = season.status.value
        else:
            entity_status = item.status.value

        detail_lower = (h.status_detail or "").lower()
        is_failed = entity_status == "failed" or detail_lower.startswith("failed")
        is_active_downloading = (
            entity_status == "downloading"
            and detail_lower
            not in ("completed", "replaced", "canceled", "deleted", "expired")
            and not detail_lower.startswith("failed")
        )

        if not (is_active_downloading or is_failed):
            continue

        target_key = (item.id, h.season_id, h.episode_id)
        if target_key in seen_targets:
            continue
        seen_targets.add(target_key)

        target_label = discord.format_target_label(
            item=item, season=season, episode=episode
        )
        title_and_target = (
            f"{item.title} ({item.year})"
            if target_label == "Movie" and item.year
            else (
                item.title
                if target_label == "Movie"
                else f"{item.title} — {target_label}"
            )
        )

        if is_failed:
            status_badge = (
                "Failed (Manual Pick)"
                if h.push_mode == "manual"
                else "Failed (Auto-Push)"
            )
        elif detail_lower == "unpacking":
            status_badge = "Unpacking"
        elif detail_lower == "verifying":
            status_badge = "Verifying"
        else:
            status_badge = "Downloading"

        item_media_type = (
            item.media_type.value
            if hasattr(item.media_type, "value")
            else str(item.media_type or "")
        )
        feature_pills = build_release_feature_pills(
            release_title=h.nzb_title,
            score=h.score,
            size_bytes=h.size_bytes,
            is_fallback=bool(getattr(h, "is_fallback", False)),
            matched_language=h.grabbed_language,
            bitrate_mbps=h.bitrate_mbps,
            runtime_minutes=item.runtime_minutes,
            media_type=item_media_type,
            season_episode_count=season.episode_count if season else None,
        )

        active_rows.append(
            {
                "history_id": h.id,
                "media_item_id": item.id,
                "season_id": h.season_id,
                "episode_id": h.episode_id,
                "media_title": item.title,
                "target_label": target_label,
                "title_and_target": title_and_target,
                "release_name": h.nzb_title,
                "resolution": h.resolution,
                "source": h.source,
                "language": h.grabbed_language,
                "feature_pills": feature_pills,
                "progress_pct": round(float(h.progress_pct or 0.0), 1),
                "download_speed_bytes": int(h.download_speed_bytes or 0),
                "eta_seconds": h.eta_seconds,
                "speed_and_eta": _format_speed_and_eta(
                    int(h.download_speed_bytes or 0), h.eta_seconds
                ),
                "status": status_badge,
                "status_detail": h.status_detail,
                "push_mode": h.push_mode,
                "is_failed": is_failed,
            }
        )

    return active_rows


async def dismiss_failed_push(session: AsyncSession, history_id: int) -> bool:
    """Dismiss a failed manual-pick row from Active Pushes and revert its entity status to SEARCHING."""
    stmt = (
        select(DownloadHistory)
        .where(DownloadHistory.id == history_id)
        .options(
            selectinload(DownloadHistory.media_item),
            selectinload(DownloadHistory.season),
            selectinload(DownloadHistory.episode),
        )
    )
    history = (await session.execute(stmt)).scalars().first()
    if history is None:
        return False

    history.is_dismissed = True
    if history.episode is not None and history.episode.status == EpisodeStatus.FAILED:
        history.episode.status = EpisodeStatus.SEARCHING
    elif history.season is not None and history.season.status == SeasonStatus.FAILED:
        history.season.status = SeasonStatus.SEARCHING
    elif (
        history.media_item is not None
        and history.media_item.status == MediaStatus.FAILED
    ):
        history.media_item.status = MediaStatus.SEARCHING

    await session.commit()
    return True


async def cancel_active_push(session: AsyncSession, history_id: int) -> bool:
    """Cancel and delete an active transfer on TorBox, mark its history row 'canceled', and revert entity to SEARCHING."""
    stmt = (
        select(DownloadHistory)
        .where(DownloadHistory.id == history_id)
        .options(
            selectinload(DownloadHistory.media_item)
            .selectinload(MediaItem.seasons)
            .selectinload(Season.episodes),
            selectinload(DownloadHistory.season).selectinload(Season.episodes),
            selectinload(DownloadHistory.episode),
        )
    )
    history = (await session.execute(stmt)).scalars().first()
    if history is None:
        return False

    old_torbox_id = history.torbox_id

    if history.episode is not None:
        history.episode.status = EpisodeStatus.SEARCHING
    elif history.season is not None:
        history.season.status = SeasonStatus.SEARCHING
        for ep in history.season.episodes:
            if ep.status == EpisodeStatus.DOWNLOADING:
                ep.status = EpisodeStatus.SEARCHING
    elif history.media_item is not None:
        history.media_item.status = MediaStatus.SEARCHING

    history.status_detail = "canceled"
    history.is_dismissed = True
    history.notification_sent = True
    history.progress_pct = 0.0
    history.download_speed_bytes = 0
    history.eta_seconds = None

    if history.media_item is not None and (
        history.season is not None or history.episode is not None
    ):
        await _recalculate_parent_status(session, history.media_item)

    await session.commit()

    if old_torbox_id:
        await _delete_and_purge_torbox_transfer(session, old_torbox_id)

    if history.media_item is not None and history.media_item.id is not None:
        sys_settings = (
            (
                await session.execute(
                    select(SystemSettings).where(SystemSettings.id == 1)
                )
            )
            .scalars()
            .first()
        )
        sh_max_retries = (
            sys_settings.sh_max_retries
            if sys_settings and sys_settings.sh_max_retries is not None
            else 3
        )
        await _dispatch_settled_show_notifications(
            session=session,
            affected_items={history.media_item.id: history.media_item},
            sh_max_retries=sh_max_retries,
        )

    remaining = await count_downloading_entities(session)
    if remaining == 0:
        transfer_poller.sleep()
    return True


async def reconcile_completed_history_with_torbox(
    session: AsyncSession,
    live_torbox_ids: set[str],
) -> int:
    """Passively reconcile completed DownloadHistory rows against live TorBox transfer IDs.

    Marks missing rows as status_detail='deleted' without altering MediaItem, Season, or Episode status.
    """
    stmt = select(DownloadHistory).where(
        DownloadHistory.status_detail == "completed",
        DownloadHistory.torbox_id.is_not(None),
    )
    rows = (await session.execute(stmt)).scalars().all()
    updated = 0
    for row in rows:
        if row.torbox_id and str(row.torbox_id) not in live_torbox_ids:
            row.status_detail = "deleted"
            updated += 1
    if updated > 0:
        await session.commit()
    return updated


async def _recalculate_parent_status(
    session: AsyncSession,
    item: MediaItem | None,
) -> None:
    """Recalculate parent MediaItem, Season, and Episode statuses strictly from DownloadHistory truth."""
    if item is None or item.id is None:
        return

    seasons = (
        (
            await session.execute(
                select(Season)
                .where(Season.media_item_id == item.id)
                .options(
                    selectinload(Season.episodes),
                    selectinload(Season.download_history),
                )
            )
        )
        .scalars()
        .all()
    )

    season_ids = [s.id for s in seasons if s.id is not None]
    ep_ids = [ep.id for s in seasons for ep in (s.episodes or []) if ep.id is not None]

    from sqlalchemy import or_

    hist_conds = [DownloadHistory.media_item_id == item.id]
    if season_ids:
        hist_conds.append(DownloadHistory.season_id.in_(season_ids))
    if ep_ids:
        hist_conds.append(DownloadHistory.episode_id.in_(ep_ids))

    item_histories = (
        (await session.execute(select(DownloadHistory).where(or_(*hist_conds))))
        .scalars()
        .all()
    )

    def _is_hist_active(h: DownloadHistory) -> bool:
        if h.is_dismissed or not (h.torbox_id or h.torbox_hash):
            return False
        d = (h.status_detail or "").strip().lower()
        return d in (
            "downloading",
            "queued",
            "processing",
            "unpacking",
            "verifying",
            "pending",
        )

    def _is_hist_completed(h: DownloadHistory) -> bool:
        d = (h.status_detail or "").strip().lower()
        if d in ("deleted", "expired"):
            return not h.is_dismissed
        return d in ("completed", "")

    def _is_hist_failed(h: DownloadHistory) -> bool:
        if h.is_dismissed:
            return False
        d = (h.status_detail or "").strip().lower()
        return d.startswith("failed")

    if not seasons:
        has_active_movie = any(_is_hist_active(h) for h in item_histories)
        has_completed_movie = any(_is_hist_completed(h) for h in item_histories)
        has_failed_movie = any(_is_hist_failed(h) for h in item_histories)

        if has_active_movie:
            item.status = MediaStatus.DOWNLOADING
        elif has_completed_movie:
            item.status = MediaStatus.COMPLETED
        elif has_failed_movie and item.status == MediaStatus.FAILED:
            item.status = MediaStatus.FAILED
        else:
            if item.status != MediaStatus.FUTURE:
                item.status = MediaStatus.SEARCHING
                item.fail_count = 0
                item.last_error = None
                if hasattr(item, "completed_at"):
                    setattr(item, "completed_at", None)
        return

    for s in seasons:
        if s.season_number > 0 and s.status != SeasonStatus.IGNORED:
            s.monitored = True
        has_completed_pack = any(
            h.season_id == s.id and h.episode_id is None and _is_hist_completed(h)
            for h in item_histories
        )
        has_active_pack = any(
            h.season_id == s.id and h.episode_id is None and _is_hist_active(h)
            for h in item_histories
        )

        for ep in s.episodes or []:
            if ep.status != EpisodeStatus.IGNORED:
                ep.monitored = True
            has_completed_ep = has_completed_pack or any(
                h.episode_id == ep.id and _is_hist_completed(h) for h in item_histories
            )
            has_active_ep = has_active_pack or any(
                h.episode_id == ep.id and _is_hist_active(h) for h in item_histories
            )
            if (
                ep.status in (EpisodeStatus.COMPLETED, EpisodeStatus.DOWNLOADED)
                and not has_completed_ep
            ):
                ep.status = EpisodeStatus.SEARCHING
                ep.fail_count = 0
                ep.last_error = None
            elif ep.status == EpisodeStatus.DOWNLOADING and not has_active_ep:
                ep.status = (
                    EpisodeStatus.COMPLETED
                    if has_completed_ep
                    else EpisodeStatus.SEARCHING
                )
            elif ep.status == EpisodeStatus.PENDING:
                ep.status = EpisodeStatus.SEARCHING

        _sync_season_status_from_episodes(s)

        if not s.episodes:
            if (
                s.status in (SeasonStatus.COMPLETED, SeasonStatus.DOWNLOADED)
                and not has_completed_pack
            ):
                s.status = SeasonStatus.SEARCHING
                s.fail_count = 0
                s.last_error = None
            elif s.status == SeasonStatus.DOWNLOADING and not has_active_pack:
                s.status = (
                    SeasonStatus.COMPLETED
                    if has_completed_pack
                    else SeasonStatus.SEARCHING
                )
            elif s.status == SeasonStatus.PENDING and s.season_number > 0:
                s.status = SeasonStatus.SEARCHING

    any_downloading = any(
        s.status == SeasonStatus.DOWNLOADING
        or any(ep.status == EpisodeStatus.DOWNLOADING for ep in (s.episodes or []))
        for s in seasons
    )
    any_failed = any(
        s.status == SeasonStatus.FAILED
        or any(ep.status == EpisodeStatus.FAILED for ep in (s.episodes or []))
        for s in seasons
    )
    released_seasons = [
        s for s in seasons if s.season_number > 0 and s.status != SeasonStatus.FUTURE
    ]

    if any_downloading:
        item.status = MediaStatus.DOWNLOADING
    elif any_failed:
        item.status = MediaStatus.FAILED
    elif released_seasons and all(
        s.status in (SeasonStatus.COMPLETED, SeasonStatus.DOWNLOADED)
        for s in released_seasons
    ):
        item.status = MediaStatus.COMPLETED
    elif not released_seasons and any(
        s.status == SeasonStatus.FUTURE for s in seasons if s.season_number > 0
    ):
        item.status = MediaStatus.FUTURE
    else:
        item.status = MediaStatus.SEARCHING
        item.fail_count = 0
        item.last_error = None
        if hasattr(item, "completed_at"):
            setattr(item, "completed_at", None)


async def reconcile_all_items_transfer_truth(session: AsyncSession) -> int:
    """Reconcile all non-ignored MediaItems, Seasons, and Episodes against active/completed DownloadHistory truth."""
    stmt = (
        select(MediaItem)
        .where(MediaItem.status != MediaStatus.IGNORED)
        .options(
            selectinload(MediaItem.seasons).selectinload(Season.episodes),
        )
    )
    items = (await session.execute(stmt)).scalars().unique().all()
    reconciled = 0
    for item in items:
        needs_check = item.status in (
            MediaStatus.COMPLETED,
            MediaStatus.DOWNLOADED,
            MediaStatus.DOWNLOADING,
            MediaStatus.PENDING,
        ) or any(
            s.status
            in (
                SeasonStatus.COMPLETED,
                SeasonStatus.DOWNLOADED,
                SeasonStatus.DOWNLOADING,
                SeasonStatus.PENDING,
            )
            or not s.monitored
            or any(
                ep.status
                in (
                    EpisodeStatus.COMPLETED,
                    EpisodeStatus.DOWNLOADED,
                    EpisodeStatus.DOWNLOADING,
                    EpisodeStatus.PENDING,
                )
                or not ep.monitored
                for ep in (s.episodes or [])
            )
            for s in (item.seasons or [])
        )
        if needs_check:
            await _recalculate_parent_status(session, item)
            reconciled += 1
    return reconciled


async def manual_delete_from_torbox(
    session: AsyncSession,
    *,
    history_id: int | None = None,
    item_id: int | None = None,
    season_id: int | None = None,
    episode_id: int | None = None,
) -> dict[str, Any]:
    """Delete transfer(s) from TorBox, mark DownloadHistory 'deleted', reset target entities to SEARCHING, and recalculate parent MediaItem status."""
    from sqlalchemy import or_

    history_row: DownloadHistory | None = None
    if history_id is not None:
        history_row = (
            (
                await session.execute(
                    select(DownloadHistory)
                    .where(DownloadHistory.id == history_id)
                    .options(
                        selectinload(DownloadHistory.media_item),
                        selectinload(DownloadHistory.season).selectinload(
                            Season.episodes
                        ),
                        selectinload(DownloadHistory.episode),
                    )
                )
            )
            .scalars()
            .first()
        )
        if history_row is None:
            return {"status": "not_found", "deleted_torbox_ids": []}
        if item_id is None:
            item_id = history_row.media_item_id
        if season_id is None:
            season_id = history_row.season_id
        if episode_id is None:
            episode_id = history_row.episode_id

    item: MediaItem | None = None
    if item_id is not None:
        item = (
            (
                await session.execute(
                    select(MediaItem)
                    .where(MediaItem.id == item_id)
                    .options(
                        selectinload(MediaItem.seasons).selectinload(Season.episodes),
                    )
                )
            )
            .scalars()
            .first()
        )

    season: Season | None = None
    if season_id is not None:
        season = (
            (
                await session.execute(
                    select(Season)
                    .where(Season.id == season_id)
                    .options(
                        selectinload(Season.episodes),
                        selectinload(Season.media_item),
                    )
                )
            )
            .scalars()
            .first()
        )
        if item is None and season is not None:
            item = season.media_item

    episode: Episode | None = None
    if episode_id is not None:
        episode = (
            (
                await session.execute(
                    select(Episode)
                    .where(Episode.id == episode_id)
                    .options(
                        selectinload(Episode.season).selectinload(Season.episodes),
                        selectinload(Episode.season).selectinload(Season.media_item),
                    )
                )
            )
            .scalars()
            .first()
        )
        if season is None and episode is not None:
            season = episode.season
        if item is None and season is not None:
            item = season.media_item

    # Collect matching DownloadHistory rows to mark 'deleted' and extract torbox_ids
    matching_histories: list[DownloadHistory] = []
    if history_row is not None:
        matching_histories.append(history_row)

    if episode_id is not None:
        ep_hists = (
            (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.episode_id == episode_id
                    )
                )
            )
            .scalars()
            .all()
        )
        for h in ep_hists:
            if all(existing.id != h.id for existing in matching_histories):
                matching_histories.append(h)
    elif season_id is not None:
        ep_ids = [
            ep.id for ep in (season.episodes if season else []) if ep.id is not None
        ]
        if ep_ids:
            s_stmt = select(DownloadHistory).where(
                or_(
                    DownloadHistory.season_id == season_id,
                    DownloadHistory.episode_id.in_(ep_ids),
                )
            )
        else:
            s_stmt = select(DownloadHistory).where(
                DownloadHistory.season_id == season_id
            )
        s_hists = (await session.execute(s_stmt)).scalars().all()
        for h in s_hists:
            if all(existing.id != h.id for existing in matching_histories):
                matching_histories.append(h)
    elif item_id is not None:
        i_stmt = select(DownloadHistory).where(
            DownloadHistory.media_item_id == item_id,
            DownloadHistory.season_id.is_(None),
            DownloadHistory.episode_id.is_(None),
        )
        i_hists = (await session.execute(i_stmt)).scalars().all()
        for h in i_hists:
            if all(existing.id != h.id for existing in matching_histories):
                matching_histories.append(h)

    torbox_ids_to_delete: list[str] = []
    for h in matching_histories:
        detail_low = (h.status_detail or "").strip().lower()
        if h.id == history_id or detail_low not in ("replaced", "canceled"):
            if h.id == history_id or detail_low != "deleted":
                if h.torbox_id and str(h.torbox_id) not in torbox_ids_to_delete:
                    torbox_ids_to_delete.append(str(h.torbox_id))
            h.status_detail = "deleted"
            h.is_dismissed = True
            h.notification_sent = True
            h.download_speed_bytes = 0
            h.eta_seconds = None

    # Reset target entities to SEARCHING (Ready to Push)
    if episode is not None:
        episode.status = EpisodeStatus.SEARCHING
        episode.fail_count = 0
        episode.last_error = None
        if hasattr(episode, "torbox_id"):
            setattr(episode, "torbox_id", None)
        if season is not None and season.status in (
            SeasonStatus.COMPLETED,
            SeasonStatus.DOWNLOADED,
        ):
            season.status = SeasonStatus.SEARCHING
        await _recalculate_parent_status(session, item)
    elif season is not None:
        season.status = SeasonStatus.SEARCHING
        season.fail_count = 0
        season.last_error = None
        if hasattr(season, "torbox_id"):
            setattr(season, "torbox_id", None)
        for ep in season.episodes or []:
            if ep.status != EpisodeStatus.FUTURE:
                ep.status = EpisodeStatus.SEARCHING
                ep.fail_count = 0
                ep.last_error = None
                if hasattr(ep, "torbox_id"):
                    setattr(ep, "torbox_id", None)
        await _recalculate_parent_status(session, item)
    elif item is not None:
        item.status = MediaStatus.SEARCHING
        item.fail_count = 0
        item.last_error = None
        if hasattr(item, "torbox_id"):
            setattr(item, "torbox_id", None)
        if hasattr(item, "completed_at"):
            setattr(item, "completed_at", None)
        await _recalculate_parent_status(session, item)

    await session.commit()

    for tb_id in torbox_ids_to_delete:
        await _delete_and_purge_torbox_transfer(session, tb_id)

    remaining = await count_downloading_entities(session)
    if remaining == 0:
        transfer_poller.sleep()

    return {
        "status": "ok",
        "deleted_torbox_ids": torbox_ids_to_delete,
        "item_id": item.id if item else item_id,
    }


async def get_push_history_ledger(session: AsyncSession) -> list[dict[str, Any]]:
    """Query DownloadHistory ordered by COALESCE(torbox_sent_at, created_at) DESC, id DESC for Card 4 Push History Ledger."""
    stmt = (
        select(DownloadHistory)
        .options(
            selectinload(DownloadHistory.media_item),
            selectinload(DownloadHistory.season),
            selectinload(DownloadHistory.episode),
        )
        .order_by(
            func.coalesce(
                DownloadHistory.torbox_sent_at, DownloadHistory.created_at
            ).desc(),
            DownloadHistory.id.desc(),
        )
    )
    rows = (await session.execute(stmt)).scalars().all()
    ledger: list[dict[str, Any]] = []

    for h in rows:
        item = h.media_item
        season = h.season
        episode = h.episode

        # Title & Target
        if item is not None:
            media_title = item.title
            media_year = item.year
            alt_title = item.alt_title
            poster_url = item.poster_url or h.poster_url
            simkl_id = (
                (season.simkl_id if season and season.simkl_id else None)
                or item.simkl_id
                or h.simkl_id
            )
            tmdb_id = item.tmdb_id or h.tmdb_id
            imdb_id = item.imdb_id or h.imdb_id
            anilist_id = (
                (season.anilist_id if season and season.anilist_id else None)
                or item.anilist_id
                or h.anilist_id
            )
            target_label = discord.format_target_label(
                item=item, season=season, episode=episode
            )
            if item.media_type == MediaType.ANIME or getattr(
                item, "is_anime_movie", False
            ):
                type_label = "ANIME"
                category_key = "anime"
            elif item.media_type == MediaType.SHOW:
                type_label = "SERIES"
                category_key = "show"
            else:
                type_label = "MOVIE"
                category_key = "movie"
            is_detached = False
        else:
            media_title = h.media_title or h.nzb_title
            media_year = h.media_year
            alt_title = None
            poster_url = h.poster_url
            simkl_id = h.simkl_id
            tmdb_id = h.tmdb_id
            imdb_id = h.imdb_id
            anilist_id = h.anilist_id
            target_label = h.target_label or "Movie"
            raw_type = (h.media_type_label or "MOVIE").upper()
            if "ANIME" in raw_type:
                type_label = "ANIME"
                category_key = "anime"
            elif "SERIES" in raw_type or "SHOW" in raw_type:
                type_label = "SERIES"
                category_key = "show"
            else:
                type_label = "MOVIE"
                category_key = "movie"
            is_detached = True

        # Status badge classification (active downloading transfers stay in Card 5 Active Pushes)
        detail_raw = (h.status_detail or "").strip()
        detail_low = detail_raw.lower()
        entity_is_downloading = (
            (episode is not None and episode.status == EpisodeStatus.DOWNLOADING)
            or (
                episode is None
                and season is not None
                and season.status == SeasonStatus.DOWNLOADING
            )
            or (
                episode is None
                and season is None
                and item is not None
                and item.status == MediaStatus.DOWNLOADING
            )
        )
        if detail_low in (
            "downloading",
            "queued",
            "processing",
            "unpacking",
            "verifying",
        ) or (not detail_low and entity_is_downloading):
            continue

        if detail_low in ("deleted", "expired"):
            torbox_status = "DELETED"
            status_key = "deleted"
            failure_reason = None
        elif detail_low == "replaced":
            torbox_status = "REPLACED"
            status_key = "replaced"
            failure_reason = None
        elif detail_low == "canceled":
            torbox_status = "CANCELED"
            status_key = "canceled"
            failure_reason = None
        elif detail_low.startswith("failed"):
            torbox_status = "FAILED"
            status_key = "failed"
            failure_reason = (
                detail_raw[len("failed:") :].strip()
                if ":" in detail_raw
                else detail_raw
            )
        else:
            torbox_status = "READY ON TORBOX"
            status_key = "completed"
            failure_reason = None

        pushed_dt = h.torbox_sent_at or h.created_at
        pushed_ts = pushed_dt.timestamp() if pushed_dt else 0.0
        pushed_str = pushed_dt.strftime("%Y-%m-%d %H:%M") if pushed_dt else "—"

        size_gb = (
            f"{h.size_bytes / (1024**3):.1f} GB"
            if h.size_bytes and h.size_bytes > 0
            else None
        )

        feature_pills = build_release_feature_pills(
            release_title=h.nzb_title,
            score=h.score,
            size_bytes=h.size_bytes,
            is_fallback=bool(getattr(h, "is_fallback", False)),
            matched_language=h.grabbed_language,
            bitrate_mbps=h.bitrate_mbps,
            runtime_minutes=item.runtime_minutes if item else None,
            media_type=category_key,
            season_episode_count=season.episode_count if season else None,
        )

        ledger.append(
            {
                "id": h.id,
                "media_item_id": h.media_item_id,
                "season_id": h.season_id,
                "episode_id": h.episode_id,
                "media_title": media_title,
                "media_year": media_year,
                "alt_title": alt_title,
                "poster_url": poster_url,
                "target_label": target_label,
                "type_label": type_label,
                "category_key": category_key,
                "is_detached": is_detached,
                "simkl_id": simkl_id,
                "tmdb_id": tmdb_id,
                "imdb_id": imdb_id,
                "anilist_id": anilist_id,
                "nzb_title": h.nzb_title,
                "resolution": h.resolution,
                "source": h.source,
                "video_codec": h.video_codec,
                "audio_codec": h.audio_codec,
                "grabbed_language": h.grabbed_language,
                "size_gb": size_gb,
                "score": int(h.score) if h.score is not None else None,
                "feature_pills": feature_pills,
                "pushed_at_str": pushed_str,
                "pushed_at_ts": pushed_ts,
                "torbox_id": h.torbox_id,
                "status_detail": h.status_detail,
                "torbox_status": torbox_status,
                "status_key": status_key,
                "failure_reason": failure_reason,
                "can_delete_torbox": status_key == "completed" and bool(h.torbox_id),
                "can_repush": h.media_item_id is not None,
            }
        )

    return ledger
