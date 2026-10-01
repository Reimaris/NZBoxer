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
3. Layer 3 Playable Video Verification (`is_torbox_filelist_fake`) with a 15-minute
   unpack grace window (`UNPACK_GRACE_PERIOD_SECONDS = 900`) for archive-only payloads,
   transitioning verified transfers to `COMPLETED` and dispatching `Ready on TorBox` notifications.
4. Split Failure Recovery:
   - `push_mode == "auto"`: Deletes broken transfer from TorBox, blacklists the release,
     and automatically searches & pushes the next-best non-blacklisted candidate.
   - `push_mode == "manual"`: Deletes broken transfer from TorBox, blacklists the release,
     and transitions the entity to `FAILED` (preserving the row in Active Pushes until
     the user clicks `[Re-Search & Pick]` or `[Dismiss]`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from app.core.failure_logger import log_failure
from app.core.fake_detector import is_torbox_filelist_fake
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
    SeenTorboxDownload,
    SystemSettings,
)
from app.services import discord, torbox

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

UNPACK_GRACE_PERIOD_SECONDS = 900
NOT_FOUND_GRACE_PERIOD_SECONDS = 900


class TransferPollerState:
    """Tracks whether the event-driven TorBox transfer poller is awake or sleeping."""

    def __init__(self) -> None:
        self.is_awake: bool = False
        self.last_polled_at: datetime | None = None

    def wake(self) -> None:
        if not self.is_awake:
            logger.info("⚡ Auto-Wake Transfer Poller awakened.")
        self.is_awake = True

    def sleep(self) -> None:
        if self.is_awake:
            logger.info("💤 Active transfers finished. Transfer Poller entering sleep.")
        self.is_awake = False


transfer_poller = TransferPollerState()


def wake_transfer_poller() -> None:
    """Wake the transfer poller when a new push is dispatched to TorBox."""
    transfer_poller.wake()


async def count_downloading_entities(session: AsyncSession) -> int:
    """Count all entities currently in DOWNLOADING status across Movies, Seasons, and Episodes."""
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
                Season.status == SeasonStatus.DOWNLOADING
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
    """Delete a failed/canceled transfer from TorBox and remove its SeenTorboxDownload cache row."""
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

    await session.execute(
        delete(SeenTorboxDownload).where(SeenTorboxDownload.torbox_id == str(torbox_id))
    )


async def _auto_replace_failed_push(
    session: AsyncSession,
    item: MediaItem,
    season: Season | None = None,
    episode: Episode | None = None,
) -> DownloadHistory | None:
    """Search and push the next-best non-blacklisted release candidate for a failed Auto-Push."""
    from app.core.push_engine import (
        _dispatch_candidate_list_to_torbox,
        _load_blacklisted_sets,
        _query_movie_across_indexers,
        _query_show_across_indexers,
        _score_and_partition_candidates,
        resolve_effective_search_config,
    )

    effective_cfg = await resolve_effective_search_config(session, item)
    blacklisted_guids, blacklisted_titles = await _load_blacklisted_sets(
        session, item.id
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
            runtime_minutes=None,
            effective_cfg=effective_cfg,
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
        if entry_type == "movie":
            raw_results = await _query_movie_across_indexers(
                session,
                item,
                title_override=season.title or item.title,
                use_external_ids=False,
            )
            partitioned = _score_and_partition_candidates(
                raw_results=raw_results,
                blacklisted_guids=blacklisted_guids,
                blacklisted_titles=blacklisted_titles,
                expected_title=season.title or item.title,
                expected_year=season.air_date.year if season.air_date else None,
                expected_alt_title=item.title,
                expected_season=None,
                expected_episode=None,
                expected_season_title=season.title,
                runtime_minutes=item.runtime_minutes,
                effective_cfg=effective_cfg,
            )
            return await _dispatch_candidate_list_to_torbox(
                session,
                partitioned["all_valid"],
                item=item,
                season=season,
                episode=None,
                push_mode="auto",
                event_type="push_initiated",
                reset_fail_count=False,
            )

        effective_s_num = int(season.type_number or season.season_number)
        raw_results = await _query_show_across_indexers(
            session, item, season_number=effective_s_num, episode_number=None
        )
        partitioned = _score_and_partition_candidates(
            raw_results=raw_results,
            blacklisted_guids=blacklisted_guids,
            blacklisted_titles=blacklisted_titles,
            expected_title=item.title,
            expected_year=None,
            expected_alt_title=item.alt_title,
            expected_season=effective_s_num,
            expected_episode=None,
            expected_season_title=(
                season.title if season.title and season.title != item.title else None
            ),
            runtime_minutes=None,
            effective_cfg=effective_cfg,
        )
        return await _dispatch_candidate_list_to_torbox(
            session,
            partitioned["all_valid"],
            item=item,
            season=season,
            episode=None,
            push_mode="auto",
            event_type="push_initiated",
            reset_fail_count=False,
        )

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


async def run_transfer_poller_tick(
    session: AsyncSession | None = None,
    pre_fetched_downloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute a single tick of the Auto-Wake / Auto-Sleep Transfer Poller.

    If zero entities are currently in `DOWNLOADING` status, makes zero TorBox API calls
    and immediately puts the poller to sleep.
    """
    if session is None:
        async with async_session_factory() as owned_session:
            return await _run_transfer_poller_tick_with_session(
                owned_session, pre_fetched_downloads=pre_fetched_downloads
            )
    return await _run_transfer_poller_tick_with_session(
        session, pre_fetched_downloads=pre_fetched_downloads
    )


async def _run_transfer_poller_tick_with_session(
    session: AsyncSession,
    pre_fetched_downloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    active_count = await count_downloading_entities(session)
    if active_count == 0:
        transfer_poller.sleep()
        return {
            "polled": False,
            "active_count": 0,
            "remaining_active": 0,
            "sleeping": True,
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

    def _latest_history(
        histories: list[DownloadHistory],
        require_episode_none: bool = False,
    ) -> DownloadHistory | None:
        candidates = [
            h
            for h in histories
            if not h.is_dismissed and (not require_episode_none or h.episode_id is None)
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda h: (
                h.torbox_sent_at or datetime.min.replace(tzinfo=timezone.utc),
                h.id or 0,
            ),
        )

    for m in movies:
        hist = _latest_history(m.download_history)
        if hist is not None:
            targets_to_evaluate.append((m, hist, m, None, None))

    for s in seasons:
        hist = _latest_history(s.download_history, require_episode_none=True)
        if hist is not None:
            targets_to_evaluate.append((s, hist, s.media_item, s, None))

    for e in episodes:
        hist = _latest_history(e.download_history)
        if hist is not None:
            targets_to_evaluate.append((e, hist, e.season.media_item, e.season, e))

    now_utc = datetime.now(timezone.utc)

    for target, history, item, season, episode in targets_to_evaluate:
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

        # 1. Check Completed / Cached -> Layer 3 Playable Video Verification
        if status_lower in ("completed", "cached", "paused") and tb_item is not None:
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
                if elapsed_seconds < UNPACK_GRACE_PERIOD_SECONDS:
                    # Still within the 15-minute unpack grace window
                    history.progress_pct = 100.0
                    history.download_speed_bytes = 0
                    history.eta_seconds = 0
                    history.status_detail = "unpacking"
                    continue
                else:
                    # Exceeded 15m unpack grace window -> treat as unextractable archive failure
                    await _handle_transfer_failure(
                        session=session,
                        target=target,
                        history=history,
                        item=item,
                        season=season,
                        episode=episode,
                        tb_item=tb_item,
                        reason="TorBox failed to extract archive (only RARs/PAR2)",
                        error_type="unextracted_archive",
                        log_msg=f"TorBox failed to extract archive (only RARs/PAR2): {fake_reason}",
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
                if season is not None:
                    non_future_eps = [
                        ep
                        for ep in season.episodes
                        if ep.status != EpisodeStatus.FUTURE
                    ]
                    if non_future_eps and all(
                        ep.status in (EpisodeStatus.COMPLETED, EpisodeStatus.DOWNLOADED)
                        for ep in non_future_eps
                    ):
                        season.status = SeasonStatus.COMPLETED
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

            target_lbl = discord.format_target_label(
                item=item, season=season, episode=episode
            )
            await discord.dispatch_notification_event(
                session=session,
                event_type="completed",
                media_title=item.title,
                media_year=item.year,
                target_label=target_lbl,
                release_name=history.nzb_title,
                resolution=history.resolution,
                source=history.source,
                video_codec=history.video_codec,
                audio_codec=history.audio_codec,
                language=history.grabbed_language,
                status_reason="Ready on TorBox (Verified Playable Video)",
                poster_url=item.poster_url,
            )
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

    await session.commit()

    remaining_active = await count_downloading_entities(session)
    if remaining_active == 0:
        transfer_poller.sleep()

    return {
        "polled": True,
        "active_count": active_count,
        "remaining_active": remaining_active,
        "sleeping": remaining_active == 0,
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
    await session.flush()

    await _delete_and_purge_torbox_transfer(session, history.torbox_id, tb_item)

    target_lbl = discord.format_target_label(item=item, season=season, episode=episode)
    push_mode = (history.push_mode or "auto").strip().lower()
    failed_release_title = history.nzb_title

    if push_mode == "manual":
        # Manual Pick Failure: do NOT auto-replace; mark entity FAILED and keep row in Active Pushes
        history.download_speed_bytes = 0
        history.eta_seconds = None
        history.status_detail = f"failed: {reason}"
        history.is_dismissed = False

        if isinstance(target, Episode):
            target.status = EpisodeStatus.FAILED
            target.last_error = reason
        elif isinstance(target, Season):
            target.status = SeasonStatus.FAILED
            target.last_error = reason
        else:
            target.status = MediaStatus.FAILED
            target.last_error = reason

        await session.flush()
        await discord.dispatch_notification_event(
            session=session,
            event_type="failure",
            media_title=item.title,
            media_year=item.year,
            target_label=target_lbl,
            release_name=failed_release_title,
            resolution=history.resolution,
            source=history.source,
            video_codec=history.video_codec,
            audio_codec=history.audio_codec,
            language=history.grabbed_language,
            status_reason=f"Manual pick failed ({reason}). Click Re-Search & Pick in Active Pushes.",
            poster_url=item.poster_url,
        )
        return

    # Auto-Push Failure (`push_mode == "auto"`): increment target.fail_count and check retry budget
    target.fail_count = int(target.fail_count or 0) + 1
    await session.flush()

    replacement_hist: DownloadHistory | None = None
    if target.fail_count <= sh_max_retries:
        replacement_hist = await _auto_replace_failed_push(
            session, item=item, season=season, episode=episode
        )

    if replacement_hist is not None:
        await session.delete(history)
        await session.flush()
        await discord.dispatch_notification_event(
            session=session,
            event_type="auto_replaced",
            media_title=item.title,
            media_year=item.year,
            target_label=target_lbl,
            release_name=replacement_hist.nzb_title,
            resolution=replacement_hist.resolution,
            source=replacement_hist.source,
            video_codec=replacement_hist.video_codec,
            audio_codec=replacement_hist.audio_codec,
            language=replacement_hist.grabbed_language,
            status_reason=(
                f"Auto-replaced failed release ({failed_release_title}) with next-best candidate "
                f"(attempt {target.fail_count}/{sh_max_retries})."
            ),
            poster_url=item.poster_url,
        )
        return

    # Budget exhausted (`target.fail_count > sh_max_retries`) or zero replacement candidates found:
    # Stop auto-replacing, transition target to FAILED, keep history row in Active Pushes (`is_dismissed = False`),
    # and dispatch a failure notification.
    history.download_speed_bytes = 0
    history.eta_seconds = None
    history.status_detail = f"failed: {reason}"
    history.is_dismissed = False

    if isinstance(target, Episode):
        target.status = EpisodeStatus.FAILED
        target.last_error = reason
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

    await session.flush()

    failure_reason_msg = (
        f"Auto-push failed ({reason}) and max replacement attempts ({sh_max_retries}) were exhausted."
        if target.fail_count > sh_max_retries
        else f"Push failed ({reason}) and no replacement candidates were found."
    )
    await discord.dispatch_notification_event(
        session=session,
        event_type="failure",
        media_title=item.title,
        media_year=item.year,
        target_label=target_lbl,
        release_name=failed_release_title,
        resolution=history.resolution,
        source=history.source,
        video_codec=history.video_codec,
        audio_codec=history.audio_codec,
        language=history.grabbed_language,
        status_reason=failure_reason_msg,
        poster_url=item.poster_url,
    )


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
            and detail_lower != "completed"
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
    """Cancel and delete an active transfer on TorBox, dismiss its history row, and revert entity to SEARCHING."""
    stmt = (
        select(DownloadHistory)
        .where(DownloadHistory.id == history_id)
        .options(
            selectinload(DownloadHistory.media_item),
            selectinload(DownloadHistory.season).selectinload(Season.episodes),
            selectinload(DownloadHistory.episode),
        )
    )
    history = (await session.execute(stmt)).scalars().first()
    if history is None:
        return False

    if history.torbox_id:
        await _delete_and_purge_torbox_transfer(session, history.torbox_id)

    if history.episode is not None:
        history.episode.status = EpisodeStatus.SEARCHING
    elif history.season is not None:
        history.season.status = SeasonStatus.SEARCHING
        for ep in history.season.episodes:
            if ep.status == EpisodeStatus.DOWNLOADING:
                ep.status = EpisodeStatus.SEARCHING
    elif history.media_item is not None:
        history.media_item.status = MediaStatus.SEARCHING

    await session.delete(history)
    await session.commit()

    remaining = await count_downloading_entities(session)
    if remaining == 0:
        transfer_poller.sleep()
    return True
