from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.models import (
    EpisodeStatus,
    MediaItem,
    MediaType,
    Provider,
    Season,
)
from tests.conftest import ingest_simkl_payloads


@pytest.mark.asyncio
async def test_anime_franchise_consolidation_and_monitoring_default(db_session):
    provider = Provider(name="Simkl", type="simkl", category="watchlist")
    db_session.add(provider)
    await db_session.flush()

    # 4-Season franchise hierarchy for To LOVE-Ru
    mock_hierarchy = {
        "root": {
            "id": 3457,
            "idMal": 3457,
            "title": {"english": "To LOVE-Ru", "romaji": "To LOVE-Ru"},
            "episodes": 26,
            "startDate": {"year": 2008, "month": 4, "day": 4},
        },
        "hierarchy": [
            {
                "id": 3457,
                "idMal": 3457,
                "title": {"english": "To LOVE-Ru", "romaji": "To LOVE-Ru"},
                "episodes": 26,
                "startDate": {"year": 2008, "month": 4, "day": 4},
            },
            {
                "id": 9181,
                "idMal": 9181,
                "title": {"english": "Motto To LOVE-Ru", "romaji": "Motto To LOVE-Ru"},
                "episodes": 12,
                "startDate": {"year": 2010, "month": 10, "day": 6},
            },
            {
                "id": 13663,
                "idMal": 13663,
                "title": {
                    "english": "To LOVE-Ru Darkness",
                    "romaji": "To LOVE-Ru Darkness",
                },
                "episodes": 12,
                "startDate": {"year": 2012, "month": 10, "day": 6},
            },
            {
                "id": 20999,
                "idMal": 20999,
                "title": {
                    "english": "To LOVE-Ru Darkness 2nd",
                    "romaji": "To LOVE-Ru Darkness 2nd",
                },
                "episodes": 14,
                "startDate": {"year": 2015, "month": 7, "day": 7},
            },
        ],
    }

    simkl_items = [
        {
            "anime": {
                "title": "To LOVE-Ru",
                "year": 2008,
                "ids": {"simkl": 1001, "anilist": "3457"},
            }
        }
    ]

    with patch(
        "app.services.anilist.get_anime_root_and_hierarchy",
        new_callable=AsyncMock,
        return_value=mock_hierarchy,
    ):
        await ingest_simkl_payloads(db_session, provider, simkl_items, MediaType.ANIME)
        await db_session.commit()

    # 1. Verify parent MediaItem created with 4 sequential seasons
    stmt = select(MediaItem).options(
        selectinload(MediaItem.seasons).selectinload(Season.episodes)
    )
    items = (await db_session.execute(stmt)).scalars().all()
    assert len(items) == 1
    parent = items[0]
    assert parent.title == "To LOVE-Ru"
    assert parent.anilist_id == 3457
    assert len(parent.seasons) == 4

    # 2. Verify v3 ADR-086 rule: All released seasons are monitored and SEARCHING by default
    s1 = next(s for s in parent.seasons if s.season_number == 1)
    s2 = next(s for s in parent.seasons if s.season_number == 2)
    s3 = next(s for s in parent.seasons if s.season_number == 3)
    s4 = next(s for s in parent.seasons if s.season_number == 4)

    assert s1.monitored is True
    assert s1.episode_count == 26
    assert all(e.status == EpisodeStatus.SEARCHING for e in s1.episodes)

    assert s2.monitored is True
    assert s2.episode_count == 12
    assert all(e.status == EpisodeStatus.SEARCHING for e in s2.episodes)

    assert s3.monitored is True
    assert s3.episode_count == 12

    assert s4.monitored is True
    assert s4.episode_count == 14

    # 3. Simulate subsequent watchlist sync with Season 2 entry (Motto To LOVE-Ru)
    sequel_simkl_items = [
        {
            "anime": {
                "title": "Motto To LOVE-Ru",
                "year": 2010,
                "ids": {"simkl": 1002, "anilist": "9181"},
            }
        }
    ]

    with patch(
        "app.services.anilist.get_anime_root_and_hierarchy",
        new_callable=AsyncMock,
        return_value=mock_hierarchy,
    ):
        await ingest_simkl_payloads(
            db_session, provider, sequel_simkl_items, MediaType.ANIME
        )
        await db_session.commit()

    # 4. Verify no new MediaItem created, and Season 2 is activated/monitored
    db_session.expire_all()
    all_items = (await db_session.execute(stmt)).scalars().all()
    assert len(all_items) == 1

    reloaded_parent = all_items[0]
    reloaded_s2 = next(s for s in reloaded_parent.seasons if s.season_number == 2)
    assert reloaded_s2.anilist_id == 9181
    assert reloaded_s2.monitored is True


@pytest.mark.asyncio
async def test_metadata_reset_preserves_multi_season_franchise(db_session):
    from app.core.automation import reset_anime_metadata

    provider = Provider(name="Simkl", type="simkl", category="watchlist")
    db_session.add(provider)
    await db_session.flush()

    mock_hierarchy = {
        "root": {
            "id": 3457,
            "idMal": 3457,
            "title": {"english": "To LOVE-Ru", "romaji": "To LOVE-Ru"},
            "episodes": 26,
            "startDate": {"year": 2008, "month": 4, "day": 4},
        },
        "hierarchy": [
            {
                "id": 3457,
                "idMal": 3457,
                "title": {"english": "To LOVE-Ru", "romaji": "To LOVE-Ru"},
                "episodes": 26,
                "startDate": {"year": 2008, "month": 4, "day": 4},
            },
            {
                "id": 9181,
                "idMal": 9181,
                "title": {"english": "Motto To LOVE-Ru", "romaji": "Motto To LOVE-Ru"},
                "episodes": 12,
                "startDate": {"year": 2010, "month": 10, "day": 6},
            },
            {
                "id": 13663,
                "idMal": 13663,
                "title": {
                    "english": "To LOVE-Ru Darkness",
                    "romaji": "To LOVE-Ru Darkness",
                },
                "episodes": 12,
                "startDate": {"year": 2012, "month": 10, "day": 6},
            },
            {
                "id": 20999,
                "idMal": 20999,
                "title": {
                    "english": "To LOVE-Ru Darkness 2nd",
                    "romaji": "To LOVE-Ru Darkness 2nd",
                },
                "episodes": 14,
                "startDate": {"year": 2015, "month": 7, "day": 7},
            },
        ],
    }

    # Ingest To LOVE-Ru
    simkl_items = [
        {
            "anime": {
                "title": "To LOVE-Ru",
                "year": 2008,
                "ids": {"simkl": 1001, "anilist": "3457"},
            }
        }
    ]

    with patch(
        "app.services.anilist.get_anime_root_and_hierarchy",
        new_callable=AsyncMock,
        return_value=mock_hierarchy,
    ):
        await ingest_simkl_payloads(db_session, provider, simkl_items, MediaType.ANIME)
        await db_session.commit()

    stmt = select(MediaItem).options(
        selectinload(MediaItem.seasons).selectinload(Season.episodes)
    )
    parent = (await db_session.execute(stmt)).scalars().first()
    assert parent is not None

    # Simulate downloading S01E01 and S02E01
    s1 = next(s for s in parent.seasons if s.season_number == 1)
    s2 = next(s for s in parent.seasons if s.season_number == 2)
    s1.episodes[0].status = EpisodeStatus.DOWNLOADED
    s2.monitored = True
    s2.episodes[0].status = EpisodeStatus.DOWNLOADED
    await db_session.commit()

    # Trigger Metadata Reset
    with patch(
        "app.services.anilist.get_anime_root_and_hierarchy",
        new_callable=AsyncMock,
        return_value=mock_hierarchy,
    ):
        res = await reset_anime_metadata(db_session, parent.id)
        await db_session.commit()

    assert res is not None
    db_session.expire_all()

    reloaded = (await db_session.execute(stmt)).scalars().first()
    assert reloaded is not None
    assert len(reloaded.seasons) == 4
    rel_s1 = next(s for s in reloaded.seasons if s.season_number == 1)
    rel_s2 = next(s for s in reloaded.seasons if s.season_number == 2)
    rel_s3 = next(s for s in reloaded.seasons if s.season_number == 3)
    rel_s4 = next(s for s in reloaded.seasons if s.season_number == 4)

    assert rel_s1.episode_count == 26
    assert rel_s1.episodes[0].status == EpisodeStatus.DOWNLOADED

    assert rel_s2.episode_count == 12
    assert rel_s2.monitored is True
    assert rel_s3.episode_count == 12
    assert rel_s4.episode_count == 14


@pytest.mark.asyncio
async def test_anime_duplicate_root_consolidation_and_cleanup(db_session):
    """Verify that when 2 items map to the same root anime franchise, they merge into 1 card."""
    provider = Provider(name="Simkl", type="simkl", category="watchlist")
    db_session.add(provider)
    await db_session.flush()

    mock_hierarchy = {
        "root": {
            "id": 356,
            "idMal": 356,
            "title": {"english": "Fate/stay night", "romaji": "Fate/stay night"},
            "episodes": 24,
            "startDate": {"year": 2006, "month": 1, "day": 7},
        },
        "hierarchy": [
            {
                "id": 356,
                "idMal": 356,
                "title": {"english": "Fate/stay night", "romaji": "Fate/stay night"},
                "episodes": 24,
                "startDate": {"year": 2006, "month": 1, "day": 7},
            },
            {
                "id": 10087,
                "idMal": 10087,
                "title": {"english": "Fate/Zero", "romaji": "Fate/Zero"},
                "episodes": 13,
                "startDate": {"year": 2011, "month": 10, "day": 2},
            },
        ],
    }

    # Simulate two incoming Simkl items for Fate (e.g. 2023 release and 2024 release)
    simkl_items = [
        {
            "anime": {
                "title": "Fate/stay night",
                "year": 2023,
                "ids": {"simkl": 1999306, "anilist": "356"},
            }
        },
        {
            "anime": {
                "title": "Fate/stay night",
                "year": 2024,
                "ids": {
                    "simkl": 2200822,
                    "anilist": "356",
                    "tmdb": "229858",
                    "imdb": "tt32864316",
                },
            }
        },
    ]

    with patch(
        "app.services.anilist.get_anime_root_and_hierarchy",
        new_callable=AsyncMock,
        return_value=mock_hierarchy,
    ):
        await ingest_simkl_payloads(db_session, provider, simkl_items, MediaType.ANIME)
        await db_session.commit()

    stmt = select(MediaItem).options(selectinload(MediaItem.seasons))
    items = (await db_session.execute(stmt)).scalars().all()

    # Exactly 1 MediaItem must exist on the dashboard
    assert len(items) == 1
    fate_item = items[0]
    assert fate_item.title == "Fate/stay night"
    assert fate_item.anilist_id == 356
    # IDs from both entries should be merged
    assert fate_item.tmdb_id == 229858
    assert fate_item.imdb_id == "tt32864316"
    assert len(fate_item.seasons) == 2
