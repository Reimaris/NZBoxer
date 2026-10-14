"""
Tests for Out-of-Order Ingestion, Prequel Backfill & Sequel Deduplication
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.db.models import MediaItem, MediaType, Provider, Season, SeasonStatus
from tests.conftest import ingest_simkl_payloads


@pytest.mark.asyncio
async def test_out_of_order_sequel_ingestion(db_session):
    """Verify that syncing a sequel out-of-order creates the root and correctly sets monitored status."""
    provider = Provider(name="Test Provider", type="simkl")
    db_session.add(provider)
    await db_session.commit()

    # We are syncing Season 2 (anilist_id=200, simkl_id=222)
    simkl_items = [
        {
            "anime": {
                "title": "Season 2",
                "ids": {
                    "simkl": 222,
                    "anilist": 200,
                },
            }
        }
    ]

    hierarchy_data = {
        "root": {
            "id": 100,
            "title": {"romaji": "Root Season"},
            "idMal": 10,
            "startDate": {"year": 2020, "month": 1, "day": 10},
        },
        "hierarchy": [
            {
                "id": 100,
                "title": {"romaji": "Root Season"},
                "idMal": 10,
                "episodes": 12,
                "startDate": {"year": 2020, "month": 1, "day": 10},
            },
            {
                "id": 200,
                "title": {"romaji": "Season 2"},
                "idMal": 20,
                "episodes": 12,
                "startDate": {"year": 2021, "month": 1, "day": 10},
            },
            {
                "id": 300,
                "title": {"romaji": "Season 3"},
                "idMal": 30,
                "episodes": 12,
                "startDate": {"year": 2022, "month": 1, "day": 10},
            },
        ],
    }

    with patch("app.services.tmdb.get_show_details", return_value=None):
        with patch(
            "app.services.anilist.get_anime_root_and_hierarchy",
            return_value=hierarchy_data,
        ):
            await ingest_simkl_payloads(
                db_session, provider, simkl_items, MediaType.ANIME
            )

    await db_session.commit()

    # Check if root MediaItem was created
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    stmt = select(MediaItem).options(selectinload(MediaItem.seasons))
    items = (await db_session.execute(stmt)).scalars().all()

    assert len(items) == 1
    item = items[0]

    assert item.anilist_id == 100  # Updated to root
    assert item.title == "Root Season"

    seasons = sorted(item.seasons, key=lambda s: s.season_number)
    assert len(seasons) == 3

    # Per ADR-086: All released seasons are monitored and SEARCHING by default
    assert seasons[0].season_number == 1
    assert seasons[0].monitored is True
    assert seasons[0].status == SeasonStatus.SEARCHING

    # S2 carries the out-of-order sequel's anilist_id
    assert seasons[1].season_number == 2
    assert seasons[1].monitored is True
    assert seasons[1].status == SeasonStatus.SEARCHING
    assert seasons[1].anilist_id == 200

    # S3 is also released and monitored by default
    assert seasons[2].season_number == 3
    assert seasons[2].monitored is True
    assert seasons[2].status == SeasonStatus.SEARCHING


@pytest.mark.asyncio
async def test_sequel_deduplication(db_session):
    """Verify that syncing a sequel that already exists as a season just activates it once, and subsequent routine syncs do not spam INFO logs."""
    import logging

    from app.db.models import MediaStatus

    provider = Provider(name="Test Provider", type="simkl")
    db_session.add(provider)
    await db_session.commit()

    # Setup existing root (initially IGNORED to also verify restoration) and season 2 (unmonitored)
    item = MediaItem(
        title="Root Season",
        media_type=MediaType.ANIME,
        provider_id=provider.id,
        simkl_id=111,
        anilist_id=100,
        year=2020,
        status=MediaStatus.IGNORED,
    )
    db_session.add(item)
    await db_session.flush()

    season2 = Season(
        media_item_id=item.id,
        season_number=2,
        anilist_id=200,
        simkl_id=222,
        monitored=False,
        status=SeasonStatus.PENDING,
    )
    db_session.add(season2)
    await db_session.commit()

    # Sync Season 2 first time -> activates Season 2 and restores parent item from IGNORED
    simkl_items = [
        {
            "anime": {
                "title": "Season 2",
                "ids": {
                    "simkl": 222,
                    "anilist": 200,
                },
            }
        }
    ]

    with patch("app.services.tmdb.get_show_details", return_value=None):
        await ingest_simkl_payloads(db_session, provider, simkl_items, MediaType.ANIME)

    await db_session.commit()
    await db_session.refresh(season2)
    await db_session.refresh(item)

    # Should be activated and parent restored from IGNORED
    assert season2.monitored is True
    assert season2.status == SeasonStatus.SEARCHING
    assert item.status != MediaStatus.IGNORED

    # Subsequent routine sync when already monitored should NOT emit INFO skip logs
    records = []

    class MemoryHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = MemoryHandler()
    auto_logger = logging.getLogger("app.core.automation")
    auto_logger.addHandler(handler)
    try:
        with patch("app.services.tmdb.get_show_details", return_value=None):
            await ingest_simkl_payloads(
                db_session, provider, simkl_items, MediaType.ANIME
            )

        info_messages = [
            rec.getMessage() for rec in records if rec.levelno == logging.INFO
        ]
        assert not any(
            "Skipping standalone creation" in msg
            or "Skipping AniList hierarchy enrichment" in msg
            for msg in info_messages
        )
    finally:
        auto_logger.removeHandler(handler)

    # No new MediaItem should be created
    from sqlalchemy import select

    stmt = select(MediaItem)
    items = (await db_session.execute(stmt)).scalars().all()
    assert len(items) == 1
