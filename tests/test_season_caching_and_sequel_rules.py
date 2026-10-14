from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.automation import _sync_season_episodes
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
from tests.conftest import ingest_simkl_payloads


@pytest.mark.asyncio
async def test_sync_season_episodes_skips_when_episodes_exist(db_session: AsyncSession):
    """Test that _sync_season_episodes skips TMDB API call if season already has episodes."""
    provider = Provider(name="Simkl Test", type="simkl", is_active=True)
    db_session.add(provider)
    await db_session.flush()

    item = MediaItem(
        title="Test Show",
        media_type=MediaType.SHOW,
        status=MediaStatus.SEARCHING,
        provider_id=provider.id,
        simkl_id=10101,
        tmdb_id=12345,
    )
    db_session.add(item)
    await db_session.flush()

    season = Season(
        media_item_id=item.id,
        season_number=1,
        monitored=True,
        episode_count=2,
        status=SeasonStatus.SEARCHING,
    )
    db_session.add(season)
    await db_session.flush()

    ep1 = Episode(
        season_id=season.id,
        episode_number=1,
        status=EpisodeStatus.SEARCHING,
    )
    ep2 = Episode(
        season_id=season.id,
        episode_number=2,
        status=EpisodeStatus.SEARCHING,
    )
    db_session.add_all([ep1, ep2])
    await db_session.commit()

    with patch(
        "app.services.tmdb.get_season_details", new_callable=AsyncMock
    ) as mock_get_season:
        await _sync_season_episodes(db_session, season, tmdb_id=12345)
        mock_get_season.assert_not_called()


@pytest.mark.asyncio
async def test_sync_items_soft_ignores_standalone_sequel(db_session: AsyncSession):
    """Test that ingest + maintenance retire a standalone sequel MediaItem as IGNORED if consolidated."""
    provider = Provider(name="Simkl Test", type="simkl", is_active=True)
    db_session.add(provider)
    await db_session.flush()

    # Parent series
    parent_item = MediaItem(
        title="Parent Anime",
        media_type=MediaType.ANIME,
        status=MediaStatus.SEARCHING,
        provider_id=provider.id,
        simkl_id=10010,
        anilist_id=1001,
    )
    db_session.add(parent_item)
    await db_session.flush()

    # Season 2 on parent
    season2 = Season(
        media_item_id=parent_item.id,
        season_number=2,
        monitored=True,
        anilist_id=2002,
        status=SeasonStatus.SEARCHING,
    )
    db_session.add(season2)

    # Standalone sequel item created earlier
    standalone_item = MediaItem(
        title="Parent Anime Season 2",
        media_type=MediaType.ANIME,
        status=MediaStatus.SEARCHING,
        provider_id=provider.id,
        simkl_id=99999,
        anilist_id=2002,
    )
    db_session.add(standalone_item)
    await db_session.commit()

    incoming_data = [
        {
            "anime": {
                "title": "Parent Anime Season 2",
                "year": 2024,
                "ids": {"anilist": "2002", "simkl": 99999},
            }
        }
    ]

    with patch(
        "app.services.tmdb.get_show_details", new_callable=AsyncMock, return_value=None
    ):
        await ingest_simkl_payloads(
            db_session, provider, incoming_data, MediaType.ANIME
        )

    # Verify standalone item is now IGNORED
    refreshed_standalone = await db_session.get(MediaItem, standalone_item.id)
    assert refreshed_standalone is not None
    assert refreshed_standalone.status == MediaStatus.IGNORED


@pytest.mark.asyncio
async def test_consolidate_standalone_anime_sequels_by_title_and_id(
    db_session: AsyncSession,
):
    """Test that consolidate_standalone_anime_sequels finds existing standalone sequel MediaItems and soft-ignores them."""
    from app.core.automation import consolidate_standalone_anime_sequels

    provider = Provider(name="Simkl Test", type="simkl", is_active=True)
    db_session.add(provider)
    await db_session.flush()

    # Parent series: Goblin Slayer (Season 1 & Season 2)
    parent = MediaItem(
        title="Goblin Slayer",
        media_type=MediaType.ANIME,
        status=MediaStatus.SEARCHING,
        provider_id=provider.id,
        simkl_id=1010,
        anilist_id=10001,
    )
    db_session.add(parent)
    await db_session.flush()

    s1 = Season(
        media_item_id=parent.id,
        season_number=1,
        monitored=True,
        anilist_id=10001,
        status=SeasonStatus.COMPLETED,
    )
    s2 = Season(
        media_item_id=parent.id,
        season_number=2,
        monitored=True,
        anilist_id=20002,
        title="Goblin Slayer II",
        status=SeasonStatus.SEARCHING,
    )
    db_session.add_all([s1, s2])

    # Standalone sequel MediaItem: Goblin Slayer II
    standalone_sequel = MediaItem(
        title="Goblin Slayer II",
        media_type=MediaType.ANIME,
        status=MediaStatus.SEARCHING,
        provider_id=provider.id,
        simkl_id=2020,
    )
    db_session.add(standalone_sequel)
    await db_session.commit()

    # Run auto-consolidation
    await consolidate_standalone_anime_sequels(db_session)

    # Verify standalone item is now IGNORED and not displayed
    refreshed_sequel = await db_session.get(MediaItem, standalone_sequel.id)
    assert refreshed_sequel is not None
    assert refreshed_sequel.status == MediaStatus.IGNORED

    # Verify season 2 received the simkl_id from the standalone item
    refreshed_s2 = await db_session.get(Season, s2.id)
    assert refreshed_s2 is not None
    assert refreshed_s2.simkl_id == 2020
