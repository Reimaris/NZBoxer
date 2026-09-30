"""
TorBox Cache Sync & Auto-Adoption Engine
========================================
Provides active downloading entity counting, SeenTorboxDownload cache synchronization,
and automatic adoption of existing TorBox transfers for video media items.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Episode,
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    MediaType,
    Season,
    SeasonStatus,
)

logger = logging.getLogger(__name__)


async def count_downloading_entities(session: AsyncSession) -> int:
    """Counts active downloading entities across all supported media models."""
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


async def sync_torbox_cache(
    session, raw_downloads: list[dict], force_rescan: bool = False
) -> None:
    """Parses new/modified TorBox items into SeenTorboxDownload records.
    Ignores unchanged items to save CPU cycles.
    """
    from sqlalchemy import select

    from app.core.parser import parse_release_name
    from app.db.models import SeenTorboxDownload

    if not raw_downloads:
        return

    tb_ids = [str(d.get("id")) for d in raw_downloads if d.get("id")]
    if not tb_ids:
        return

    existing_stmt = select(SeenTorboxDownload).where(
        SeenTorboxDownload.torbox_id.in_(tb_ids)
    )
    existing_items = {
        item.torbox_id: item
        for item in (await session.execute(existing_stmt)).scalars().all()
    }

    changed = False

    for d in raw_downloads:
        tb_id = str(d.get("id"))
        if not tb_id:
            continue

        name = d.get("name") or d.get("title") or "Unknown"
        state = d.get("download_state") or d.get("status") or "unknown"
        size = d.get("size") or 0
        try:
            progress = float(d.get("progress") or 0.0)
        except ValueError:
            progress = 0.0

        existing = existing_items.get(tb_id)

        if existing:
            # Skip if nothing changed
            if (
                not force_rescan
                and existing.download_state == state
                and existing.progress == progress
            ):
                continue

            existing.download_state = state
            existing.progress = progress
            existing.size_bytes = size
            changed = True
        else:
            # Only parse if new
            parsed = parse_release_name(name)

            media_type = "unknown"
            if parsed.season is not None:
                if parsed.episode is not None:
                    media_type = "series_episode"
                else:
                    media_type = "series_season"
            elif parsed.resolution or parsed.video_codec:
                media_type = "movie"

            new_item = SeenTorboxDownload(
                torbox_id=tb_id,
                raw_title=name,
                parsed_title=parsed.title or name,
                parsed_year=parsed.year,
                season_number=parsed.season,
                episode_number=parsed.episode,
                media_type=media_type,
                download_state=state,
                size_bytes=size,
                progress=progress,
            )
            session.add(new_item)
            changed = True

    if changed:
        await session.commit()


async def adopt_torbox_downloads_for_video(session) -> None:
    """Auto-adopts SEARCHING video items using the local TorBox download cache."""
    from datetime import datetime, timezone

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.config import scoring_config
    from app.core.parser import parse_release_name
    from app.core.scorer import score_release
    from app.db.models import (
        DownloadHistory,
        Episode,
        EpisodeStatus,
        MediaItem,
        MediaStatus,
        MediaType,
        Season,
        SeasonStatus,
        SeenTorboxDownload,
    )

    target_score = scoring_config.get("cutoffs", {}).get("target_score", 8000)

    # 1. Fetch active cached video downloads
    stmt_td = select(SeenTorboxDownload).where(
        SeenTorboxDownload.media_type.in_(["movie", "series_season", "series_episode"]),
        SeenTorboxDownload.download_state.not_in(["failed", "error"]),
    )
    active_downloads = (await session.execute(stmt_td)).scalars().all()

    if not active_downloads:
        return

    import re

    def clean_title(title: str) -> str:
        return re.sub(r"[^a-z0-9]", "", title.lower())

    changed = False

    # 2. Match Movies
    stmt_m = select(MediaItem).where(
        MediaItem.media_type == MediaType.MOVIE,
        MediaItem.status == MediaStatus.SEARCHING,
    )
    movies = (await session.execute(stmt_m)).scalars().all()

    for movie in movies:
        expected = clean_title(movie.title)
        best_item = None
        best_score = -1.0
        best_parsed = None

        for d in active_downloads:
            if d.media_type != "movie":
                continue
            if not d.parsed_title or clean_title(d.parsed_title) != expected:
                continue
            if d.parsed_year and movie.year and d.parsed_year != movie.year:
                continue

            parsed = parse_release_name(d.raw_title)
            score_res = score_release(
                parsed,
                d.size_bytes or 0,
                expected_title=movie.title,
                expected_year=movie.year,
            )
            if not score_res.is_rejected and score_res.score > best_score:
                best_score = score_res.score
                best_item = d
                best_parsed = parsed

        if best_item and best_parsed:
            is_completed = best_score >= target_score
            new_status = (
                MediaStatus.COMPLETED if is_completed else MediaStatus.DOWNLOADED
            )
            if best_item.download_state in ("downloading", "queued", "processing"):
                new_status = MediaStatus.DOWNLOADING

            movie.status = new_status
            dh = DownloadHistory(
                media_item_id=movie.id,
                nzb_title=best_item.raw_title,
                score=best_score,
                size_bytes=best_item.size_bytes,
                resolution=best_parsed.resolution,
                video_codec=best_parsed.video_codec,
                audio_codec=best_parsed.audio_codec,
                source=best_parsed.source,
                torbox_id=best_item.torbox_id,
                torbox_sent_at=datetime.now(timezone.utc),
            )
            session.add(dh)
            changed = True

    # 3. TV Shows (Season Packs)
    stmt_s = (
        select(Season)
        .where(Season.status == SeasonStatus.SEARCHING)
        .options(selectinload(Season.media_item), selectinload(Season.episodes))
    )
    seasons = (await session.execute(stmt_s)).scalars().all()

    for season in seasons:
        expected = clean_title(season.media_item.title)
        best_item = None
        best_score = -1.0
        best_parsed = None

        for d in active_downloads:
            if d.media_type not in ("series_season", "series_episode"):
                continue
            if d.episode_number is not None:
                continue
            if not d.parsed_title or clean_title(d.parsed_title) != expected:
                continue
            if d.season_number != season.season_number:
                continue

            parsed = parse_release_name(d.raw_title)
            score_res = score_release(
                parsed,
                d.size_bytes or 0,
                expected_title=season.media_item.title,
                expected_year=season.media_item.year,
                expected_season=season.season_number,
            )
            if not score_res.is_rejected and score_res.score > best_score:
                best_score = score_res.score
                best_item = d
                best_parsed = parsed

        if best_item and best_parsed:
            is_completed = best_score >= target_score
            new_season_status = (
                SeasonStatus.COMPLETED if is_completed else SeasonStatus.DOWNLOADED
            )
            if best_item.download_state in ("downloading", "queued", "processing"):
                new_season_status = SeasonStatus.DOWNLOADING

            season.status = new_season_status

            # Cascade to episodes
            ep_status = (
                EpisodeStatus.DOWNLOADED
                if new_season_status != SeasonStatus.DOWNLOADING
                else EpisodeStatus.DOWNLOADING
            )
            for ep in season.episodes:
                if ep.status in (EpisodeStatus.PENDING, EpisodeStatus.SEARCHING):
                    ep.status = ep_status

            dh = DownloadHistory(
                media_item_id=season.media_item_id,
                season_id=season.id,
                nzb_title=best_item.raw_title,
                score=best_score,
                size_bytes=best_item.size_bytes,
                resolution=best_parsed.resolution,
                video_codec=best_parsed.video_codec,
                audio_codec=best_parsed.audio_codec,
                source=best_parsed.source,
                torbox_id=best_item.torbox_id,
                torbox_sent_at=datetime.now(timezone.utc),
            )
            session.add(dh)
            changed = True

    # 4. Episodes
    stmt_e = (
        select(Episode)
        .where(Episode.status == EpisodeStatus.SEARCHING)
        .options(selectinload(Episode.season).selectinload(Season.media_item))
    )
    episodes = (await session.execute(stmt_e)).scalars().all()

    for episode in episodes:
        expected = clean_title(episode.season.media_item.title)
        best_item = None
        best_score = -1.0
        best_parsed = None

        for d in active_downloads:
            if d.media_type != "series_episode":
                continue
            if not d.parsed_title or clean_title(d.parsed_title) != expected:
                continue
            if (
                d.season_number != episode.season.season_number
                or d.episode_number != episode.episode_number
            ):
                continue

            parsed = parse_release_name(d.raw_title)
            score_res = score_release(
                parsed,
                d.size_bytes or 0,
                expected_title=episode.season.media_item.title,
                expected_year=episode.season.media_item.year,
                expected_season=episode.season.season_number,
                expected_episode=episode.episode_number,
            )
            if not score_res.is_rejected and score_res.score > best_score:
                best_score = score_res.score
                best_item = d
                best_parsed = parsed

        if best_item and best_parsed:
            is_completed = best_score >= target_score
            new_episode_status = (
                EpisodeStatus.COMPLETED if is_completed else EpisodeStatus.DOWNLOADED
            )
            if best_item.download_state in ("downloading", "queued", "processing"):
                new_episode_status = EpisodeStatus.DOWNLOADING

            episode.status = new_episode_status
            dh = DownloadHistory(
                media_item_id=episode.season.media_item_id,
                season_id=episode.season.id,
                episode_id=episode.id,
                nzb_title=best_item.raw_title,
                score=best_score,
                size_bytes=best_item.size_bytes,
                resolution=best_parsed.resolution,
                video_codec=best_parsed.video_codec,
                audio_codec=best_parsed.audio_codec,
                source=best_parsed.source,
                torbox_id=best_item.torbox_id,
                torbox_sent_at=datetime.now(timezone.utc),
            )
            session.add(dh)
            changed = True

    if changed:
        await session.commit()


async def match_and_adopt_target_from_cache(session, target) -> bool:
    """
    Instantaneous evaluation of a newly added target against the cache.
    Evaluates only the single provided target against the cached downloads.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.config import scoring_config
    from app.core.parser import parse_release_name
    from app.core.scorer import score_release
    from app.db.models import (
        DownloadHistory,
        EpisodeStatus,
        MediaItem,
        MediaStatus,
        MediaType,
        Season,
        SeasonStatus,
        SeenTorboxDownload,
    )

    await session.commit()
    target_score = scoring_config.get("cutoffs", {}).get("target_score", 8000)

    import re

    def clean_title(title: str) -> str:
        return re.sub(r"[^a-z0-9]", "", title.lower())

    changed = False

    if isinstance(target, MediaItem) and target.media_type == MediaType.MOVIE:
        if target.status != MediaStatus.SEARCHING:
            return False

        stmt_td = select(SeenTorboxDownload).where(
            SeenTorboxDownload.media_type == "movie",
            SeenTorboxDownload.download_state.not_in(["failed", "error"]),
        )
        active_downloads = (await session.execute(stmt_td)).scalars().all()

        expected = clean_title(target.title)
        best_item = None
        best_score = -1.0
        best_parsed = None

        for d in active_downloads:
            if not d.parsed_title or clean_title(d.parsed_title) != expected:
                continue
            if d.parsed_year and target.year and d.parsed_year != target.year:
                continue

            parsed = parse_release_name(d.raw_title)
            score_res = score_release(
                parsed,
                d.size_bytes or 0,
                expected_title=target.title,
                expected_year=target.year,
            )
            if not score_res.is_rejected and score_res.score > best_score:
                best_score = score_res.score
                best_item = d
                best_parsed = parsed

        if best_item and best_parsed:
            is_completed = best_score >= target_score
            new_status = (
                MediaStatus.COMPLETED if is_completed else MediaStatus.DOWNLOADED
            )
            if best_item.download_state in ("downloading", "queued", "processing"):
                new_status = MediaStatus.DOWNLOADING

            target.status = new_status
            dh = DownloadHistory(
                media_item_id=target.id,
                nzb_title=best_item.raw_title,
                score=best_score,
                size_bytes=best_item.size_bytes,
                resolution=best_parsed.resolution,
                video_codec=best_parsed.video_codec,
                audio_codec=best_parsed.audio_codec,
                source=best_parsed.source,
                torbox_id=best_item.torbox_id,
                torbox_sent_at=datetime.now(timezone.utc),
            )
            session.add(dh)
            changed = True

    elif isinstance(target, MediaItem) and target.media_type in (
        MediaType.SHOW,
        MediaType.ANIME,
    ):
        # We need seasons and episodes loaded
        if not hasattr(target, "seasons") or not target.seasons:
            stmt = (
                select(MediaItem)
                .where(MediaItem.id == target.id)
                .options(selectinload(MediaItem.seasons).selectinload(Season.episodes))
            )
            target = (await session.execute(stmt)).scalars().first()
            if not target:
                return False

        stmt_td = select(SeenTorboxDownload).where(
            SeenTorboxDownload.media_type.in_(["series_season", "series_episode"]),
            SeenTorboxDownload.download_state.not_in(["failed", "error"]),
        )
        active_downloads = (await session.execute(stmt_td)).scalars().all()
        expected = clean_title(target.title)

        for season in target.seasons:
            if season.status != SeasonStatus.SEARCHING:
                continue

            best_item = None
            best_score = -1.0
            best_parsed = None

            for d in active_downloads:
                if d.media_type not in ("series_season", "series_episode"):
                    continue
                if d.episode_number is not None:
                    continue
                if not d.parsed_title or clean_title(d.parsed_title) != expected:
                    continue
                if d.season_number != season.season_number:
                    continue

                parsed = parse_release_name(d.raw_title)
                score_res = score_release(
                    parsed,
                    d.size_bytes or 0,
                    expected_title=target.title,
                    expected_year=target.year,
                    expected_season=season.season_number,
                )
                if not score_res.is_rejected and score_res.score > best_score:
                    best_score = score_res.score
                    best_item = d
                    best_parsed = parsed

            if best_item and best_parsed:
                is_completed = best_score >= target_score
                new_season_status = (
                    SeasonStatus.COMPLETED if is_completed else SeasonStatus.DOWNLOADED
                )
                if best_item.download_state in ("downloading", "queued", "processing"):
                    new_season_status = SeasonStatus.DOWNLOADING

                season.status = new_season_status

                ep_status = (
                    EpisodeStatus.DOWNLOADED
                    if new_season_status != SeasonStatus.DOWNLOADING
                    else EpisodeStatus.DOWNLOADING
                )
                for ep in season.episodes:
                    if ep.status in (EpisodeStatus.PENDING, EpisodeStatus.SEARCHING):
                        ep.status = ep_status

                dh = DownloadHistory(
                    media_item_id=target.id,
                    season_id=season.id,
                    nzb_title=best_item.raw_title,
                    score=best_score,
                    size_bytes=best_item.size_bytes,
                    resolution=best_parsed.resolution,
                    video_codec=best_parsed.video_codec,
                    audio_codec=best_parsed.audio_codec,
                    source=best_parsed.source,
                    torbox_id=best_item.torbox_id,
                    torbox_sent_at=datetime.now(timezone.utc),
                )
                session.add(dh)
                changed = True

            # Check individual episodes if season pack not found
            if season.status == SeasonStatus.SEARCHING:
                for ep in season.episodes:
                    if ep.status != EpisodeStatus.SEARCHING:
                        continue

                    best_item_ep = None
                    best_score_ep = -1.0
                    best_parsed_ep = None

                    for d in active_downloads:
                        if d.media_type != "series_episode":
                            continue
                        if (
                            not d.parsed_title
                            or clean_title(d.parsed_title) != expected
                        ):
                            continue
                        if (
                            d.season_number != season.season_number
                            or d.episode_number != ep.episode_number
                        ):
                            continue

                        parsed = parse_release_name(d.raw_title)
                        score_res = score_release(
                            parsed,
                            d.size_bytes or 0,
                            expected_title=target.title,
                            expected_year=target.year,
                            expected_season=season.season_number,
                            expected_episode=ep.episode_number,
                        )
                        if (
                            not score_res.is_rejected
                            and score_res.score > best_score_ep
                        ):
                            best_score_ep = score_res.score
                            best_item_ep = d
                            best_parsed_ep = parsed

                    if best_item_ep and best_parsed_ep:
                        is_completed = best_score_ep >= target_score
                        new_episode_status = (
                            EpisodeStatus.COMPLETED
                            if is_completed
                            else EpisodeStatus.DOWNLOADED
                        )
                        if best_item_ep.download_state in (
                            "downloading",
                            "queued",
                            "processing",
                        ):
                            new_episode_status = EpisodeStatus.DOWNLOADING

                        ep.status = new_episode_status
                        dh = DownloadHistory(
                            media_item_id=target.id,
                            season_id=season.id,
                            episode_id=ep.id,
                            nzb_title=best_item_ep.raw_title,
                            score=best_score_ep,
                            size_bytes=best_item_ep.size_bytes,
                            resolution=best_parsed_ep.resolution,
                            video_codec=best_parsed_ep.video_codec,
                            audio_codec=best_parsed_ep.audio_codec,
                            source=best_parsed_ep.source,
                            torbox_id=best_item_ep.torbox_id,
                            torbox_sent_at=datetime.now(timezone.utc),
                        )
                        session.add(dh)
                        changed = True

    if changed:
        await session.commit()
        return True

    return False
