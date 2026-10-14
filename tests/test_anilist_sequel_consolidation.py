from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import MediaItem, MediaType, Provider, Season
from tests.conftest import ingest_simkl_payloads


@pytest.mark.asyncio
async def test_anilist_fallback_creates_seasons_and_episodes(db_session):
    # Setup provider
    provider = Provider(name="Test Provider", type="torbox")
    db_session.add(provider)
    await db_session.commit()

    simkl_items = [
        {
            "anime": {
                "title": "Anime Without TMDB Seasons",
                "year": 2023,
                "ids": {
                    "simkl": 1234,
                    "anilist": 10001,
                },
            }
        }
    ]

    mock_tmdb = AsyncMock()
    # Mock TMDB returning details but with NO seasons
    mock_tmdb.get_show_details.return_value = {
        "name": "Anime Without TMDB Seasons",
        "seasons": [],
        "first_air_date": "2023-01-01",
    }

    mock_hierarchy = {
        "root": {
            "id": 10001,
            "title": {"romaji": "Anime Without TMDB Seasons"},
            "episodes": 12,
            "startDate": {"year": 2023, "month": 1, "day": 1},
        },
        "hierarchy": [
            {
                "id": 10001,
                "title": {"romaji": "Anime Without TMDB Seasons"},
                "episodes": 12,
                "startDate": {"year": 2023, "month": 1, "day": 1},
            },
            {
                "id": 10002,
                "title": {"romaji": "Anime Without TMDB Seasons Season 2"},
                "episodes": 12,
                "startDate": {"year": 2024, "month": 1, "day": 1},
            },
        ],
    }

    with (
        patch("app.core.automation.tmdb", mock_tmdb),
        patch(
            "app.services.anilist.get_anime_root_and_hierarchy", new_callable=AsyncMock
        ) as mock_hierarchy_call,
    ):
        mock_hierarchy_call.return_value = mock_hierarchy

        await ingest_simkl_payloads(db_session, provider, simkl_items, MediaType.ANIME)
        await db_session.commit()

    # Verify the media item
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    item = (
        await db_session.execute(
            select(MediaItem)
            .where(MediaItem.anilist_id == 10001)
            .options(selectinload(MediaItem.seasons).selectinload(Season.episodes))
        )
    ).scalar_one_or_none()

    assert item is not None
    assert item.anilist_id == 10001
    assert len(item.seasons) == 2

    s1 = next(s for s in item.seasons if s.season_number == 1)
    s2 = next(s for s in item.seasons if s.season_number == 2)

    assert s1.anilist_id == 10001
    assert s1.episode_count == 12
    assert len(s1.episodes) == 12
    assert s1.monitored is True

    assert s2.anilist_id == 10002
    assert s2.episode_count == 12
    assert len(s2.episodes) == 12
    assert s2.monitored is True

    # Second sync: A standalone item matching the sequel Anilist ID should be skipped
    simkl_items_standalone = [
        {
            "anime": {
                "title": "Anime Without TMDB Seasons Season 2",
                "year": 2024,
                "ids": {
                    "simkl": 5678,  # Different simkl
                    "anilist": 10002,  # Matches season 2!
                },
            }
        }
    ]

    with (
        patch("app.core.automation.tmdb", mock_tmdb),
        patch(
            "app.services.anilist.get_anime_root_and_hierarchy", new_callable=AsyncMock
        ) as mock_hierarchy_call,
    ):
        await ingest_simkl_payloads(
            db_session, provider, simkl_items_standalone, MediaType.ANIME
        )

    # Verify NO new standalone media item was created for anilist=10002
    duplicate = (
        await db_session.execute(select(MediaItem).where(MediaItem.anilist_id == 10002))
    ).scalar_one_or_none()

    assert duplicate is None
