from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import MediaItem, MediaType, Provider
from tests.conftest import ingest_simkl_payloads


@pytest.mark.asyncio
async def test_existing_anime_gets_seasons(db_session):
    provider = Provider(name="Test Provider", type="torbox")
    db_session.add(provider)
    await db_session.commit()

    # Pre-insert existing item WITH a release date but NO seasons
    from datetime import datetime, timezone

    item = MediaItem(
        title="Existing Anime",
        media_type=MediaType.ANIME,
        provider_id=provider.id,
        simkl_id=9999,
        anilist_id=20000,
        release_date=datetime(2023, 1, 1, tzinfo=timezone.utc),
        status="downloaded",
    )
    db_session.add(item)
    await db_session.commit()

    simkl_items = [
        {
            "anime": {
                "title": "Existing Anime",
                "year": 2023,
                "ids": {
                    "simkl": 9999,
                    "anilist": 20000,
                },
            }
        }
    ]

    mock_tmdb = AsyncMock()
    mock_tmdb.get_show_details.return_value = {
        "name": "Existing Anime",
        "seasons": [],
        "first_air_date": "2023-01-01",
    }

    mock_hierarchy = {
        "root": {
            "id": 20000,
            "title": {"romaji": "Existing Anime"},
            "episodes": 12,
            "startDate": {"year": 2023, "month": 1, "day": 1},
        },
        "hierarchy": [
            {
                "id": 20000,
                "title": {"romaji": "Existing Anime"},
                "episodes": 12,
                "startDate": {"year": 2023, "month": 1, "day": 1},
            }
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
        db_session.expire_all()
        await db_session.commit()

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    db_item = (
        await db_session.execute(
            select(MediaItem)
            .where(MediaItem.anilist_id == 20000)
            .options(selectinload(MediaItem.seasons))
        )
    ).scalar_one_or_none()

    assert db_item is not None
    assert len(db_item.seasons) == 1
    assert db_item.seasons[0].anilist_id == 20000
