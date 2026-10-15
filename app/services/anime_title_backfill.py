"""Startup backfill of AniList title variants for existing anime rows (m65, ADR-144)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.anime_titles import apply_title_variants
from app.db.models import MediaItem, MediaType, Season
from app.services import anilist

logger = logging.getLogger(__name__)

BACKFILL_CHUNK_SIZE = 50
BACKFILL_CHUNK_DELAY_SECONDS = 2.0


@dataclass
class BackfillStats:
    items_updated: int = 0
    seasons_updated: int = 0
    ids_missing: int = 0  # AniList returned no node
    chunks_failed: int = 0


async def _load_candidates(
    session: AsyncSession,
) -> tuple[list[MediaItem], list[Season]]:
    no_variants_item = and_(
        MediaItem.title_english.is_(None),
        MediaItem.title_romaji.is_(None),
        MediaItem.title_native.is_(None),
    )
    items = (
        (
            await session.execute(
                select(MediaItem).where(
                    MediaItem.anilist_id.is_not(None),
                    or_(
                        MediaItem.media_type == MediaType.ANIME,
                        MediaItem.is_anime_movie.is_(True),
                    ),
                    no_variants_item,
                )
            )
        )
        .scalars()
        .all()
    )
    seasons = (
        (
            await session.execute(
                select(Season).where(
                    Season.anilist_id.is_not(None),
                    Season.title_english.is_(None),
                    Season.title_romaji.is_(None),
                    Season.title_native.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return list(items), list(seasons)


async def run_anime_title_backfill(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    fetch: Callable[
        [list[int]], Awaitable[dict[int, dict[str, Any]]]
    ] = anilist.get_anime_details_batch_by_ids,
    chunk_size: int = BACKFILL_CHUNK_SIZE,
    chunk_delay: float = BACKFILL_CHUNK_DELAY_SECONDS,
) -> BackfillStats:
    """Fill missing title variants via AniList ``id_in`` batches; failures are isolated."""
    stats = BackfillStats()
    async with session_factory() as session:
        items, seasons = await _load_candidates(session)
        rows: list[MediaItem | Season] = [*items, *seasons]
        ids = sorted({int(r.anilist_id) for r in rows if r.anilist_id is not None})
        if not ids:
            return stats

        chunks = [ids[i : i + chunk_size] for i in range(0, len(ids), chunk_size)]
        for index, chunk in enumerate(chunks):
            if index:
                await asyncio.sleep(chunk_delay)
            try:
                nodes = await fetch(chunk)
            except Exception as exc:
                stats.chunks_failed += 1
                logger.warning(
                    "[BACKFILL] anime titles chunk failed (%d ids): %s",
                    len(chunk),
                    anilist._sanitize_error_message(exc),
                )
                continue
            chunk_ids = set(chunk)
            stats.ids_missing += sum(1 for aid in chunk if aid not in nodes)
            for row in items:
                node = nodes.get(int(row.anilist_id or 0))
                if row.anilist_id in chunk_ids and node:
                    apply_title_variants(row, node)
                    stats.items_updated += 1
            for season in seasons:
                node = nodes.get(int(season.anilist_id or 0))
                if season.anilist_id in chunk_ids and node:
                    apply_title_variants(season, node)
                    stats.seasons_updated += 1
            await session.commit()

    logger.info(
        "[BACKFILL] anime titles: items=%d seasons=%d missing=%d failed_chunks=%d",
        stats.items_updated,
        stats.seasons_updated,
        stats.ids_missing,
        stats.chunks_failed,
    )
    return stats
