from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.automation import reset_anime_metadata
from app.db.models import (
    DownloadHistory,
    Episode,
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    MediaType,
    Season,
    SeasonStatus,
)


@pytest.mark.asyncio
async def test_reset_anime_metadata_purges_phantoms_and_preserves_downloaded(
    db_session,
):
    # Create Provider
    from app.db.models import Provider

    provider = Provider(name="Simkl", type="simkl", category="watchlist")
    db_session.add(provider)
    await db_session.flush()

    item = MediaItem(
        provider_id=provider.id,
        simkl_id=123456,
        title="Sousou no Frieren",
        media_type=MediaType.ANIME,
        anilist_id=154587,
        status=MediaStatus.SEARCHING,
        last_searched_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    await db_session.flush()

    season1 = Season(
        media_item_id=item.id,
        season_number=1,
        monitored=True,
        episode_count=15,  # Incorrect
        status=SeasonStatus.SEARCHING,
        last_searched_at=datetime.now(timezone.utc),
    )
    db_session.add(season1)
    await db_session.flush()

    # Ep 1 (Downloaded)
    ep1 = Episode(
        season_id=season1.id,
        episode_number=1,
        monitored=True,
        status=EpisodeStatus.DOWNLOADED,
    )
    # Ep 2 (Completed)
    ep2 = Episode(
        season_id=season1.id,
        episode_number=2,
        monitored=True,
        status=EpisodeStatus.COMPLETED,
    )
    db_session.add_all([ep1, ep2])

    # Ep 3 to 15 (with phantom eps 13, 14, 15)
    for ep_num in range(3, 16):
        ep = Episode(
            season_id=season1.id,
            episode_number=ep_num,
            monitored=True,
            status=EpisodeStatus.SEARCHING,
        )
        db_session.add(ep)

    await db_session.flush()

    # Add a download history record for episode 1
    history1 = DownloadHistory(
        media_item_id=item.id,
        season_id=season1.id,
        episode_id=ep1.id,
        nzb_title="Sousou no Frieren - 01.nzb",
        score=8500.0,
    )
    db_session.add(history1)
    await db_session.commit()

    item_id = item.id

    # Mock AniList response returning correct 12 episodes for Season 1
    mock_hierarchy = {
        "root": {
            "id": 154587,
            "idMal": 52991,
            "title": {
                "english": "Frieren: Beyond Journey's End",
                "romaji": "Sousou no Frieren",
            },
            "episodes": 12,
            "startDate": {"year": 2023, "month": 9, "day": 29},
        },
        "hierarchy": [
            {
                "id": 154587,
                "idMal": 52991,
                "title": {
                    "english": "Frieren: Beyond Journey's End",
                    "romaji": "Sousou no Frieren",
                },
                "episodes": 12,
                "startDate": {"year": 2023, "month": 9, "day": 29},
            }
        ],
    }

    with patch(
        "app.services.anilist.get_anime_root_and_hierarchy",
        new_callable=AsyncMock,
        return_value=mock_hierarchy,
    ):
        updated_item = await reset_anime_metadata(db_session, item_id)
        assert updated_item is not None

    # Verify in DB
    stmt = (
        select(MediaItem)
        .where(MediaItem.id == item_id)
        .options(
            selectinload(MediaItem.seasons).selectinload(Season.episodes),
            selectinload(MediaItem.download_history),
        )
    )
    reloaded = (await db_session.execute(stmt)).scalars().first()
    assert reloaded is not None
    assert reloaded.title == "Frieren: Beyond Journey's End"
    assert reloaded.last_searched_at is None
    assert reloaded.last_metadata_refreshed_at is not None

    assert len(reloaded.seasons) == 1
    s1 = reloaded.seasons[0]
    assert s1.episode_count == 12
    assert s1.last_searched_at is None

    # Exactly 12 episodes should exist (eps 13, 14, 15 purged)
    assert len(s1.episodes) == 12
    ep_nums = [e.episode_number for e in s1.episodes]
    assert ep_nums == list(range(1, 13))

    # Ep 1 and Ep 2 must retain downloaded state
    ep1_reloaded = next(e for e in s1.episodes if e.episode_number == 1)
    assert ep1_reloaded.status == EpisodeStatus.DOWNLOADED

    ep2_reloaded = next(e for e in s1.episodes if e.episode_number == 2)
    assert ep2_reloaded.status == EpisodeStatus.COMPLETED

    for ep in s1.episodes:
        if ep.episode_number not in (1, 2):
            assert ep.status == EpisodeStatus.SEARCHING

    # Download history is preserved and still links to ep1
    assert len(reloaded.download_history) == 1
    assert reloaded.download_history[0].episode_id == ep1_reloaded.id
    assert reloaded.download_history[0].score == 8500.0


@pytest.mark.asyncio
async def test_reset_anime_metadata_multi_season_and_phantom_season_purge(db_session):
    from app.db.models import Provider

    provider = Provider(name="Simkl", type="simkl", category="watchlist")
    db_session.add(provider)
    await db_session.flush()

    item = MediaItem(
        provider_id=provider.id,
        simkl_id=222222,
        title="Mushoku Tensei",
        media_type=MediaType.ANIME,
        anilist_id=108465,
        status=MediaStatus.SEARCHING,
    )
    db_session.add(item)
    await db_session.flush()

    # Create Season 1, Season 2, and an invalid phantom Season 3
    s1 = Season(
        media_item_id=item.id,
        season_number=1,
        monitored=True,
        episode_count=11,
        status=SeasonStatus.COMPLETED,
    )
    s2 = Season(
        media_item_id=item.id,
        season_number=2,
        monitored=True,
        episode_count=12,
        status=SeasonStatus.DOWNLOADED,
    )
    s3_phantom = Season(
        media_item_id=item.id,
        season_number=3,
        monitored=True,
        episode_count=24,
        status=SeasonStatus.SEARCHING,
    )
    db_session.add_all([s1, s2, s3_phantom])
    await db_session.flush()

    # Add episodes
    for ep_num in range(1, 12):
        db_session.add(
            Episode(
                season_id=s1.id,
                episode_number=ep_num,
                status=EpisodeStatus.COMPLETED,
            )
        )
    for ep_num in range(1, 13):
        db_session.add(
            Episode(
                season_id=s2.id,
                episode_number=ep_num,
                status=EpisodeStatus.DOWNLOADED,
            )
        )
    for ep_num in range(1, 25):
        db_session.add(
            Episode(
                season_id=s3_phantom.id,
                episode_number=ep_num,
                status=EpisodeStatus.SEARCHING,
            )
        )
    await db_session.commit()
    item_id = item.id

    # AniList hierarchy only has 2 seasons
    mock_hierarchy = {
        "root": {
            "id": 108465,
            "idMal": 39535,
            "title": {"english": "Mushoku Tensei: Jobless Reincarnation"},
            "episodes": 11,
            "startDate": {"year": 2021, "month": 1, "day": 11},
        },
        "hierarchy": [
            {
                "id": 108465,
                "idMal": 39535,
                "title": {"english": "Mushoku Tensei: Jobless Reincarnation"},
                "episodes": 11,
                "startDate": {"year": 2021, "month": 1, "day": 11},
            },
            {
                "id": 127720,
                "idMal": 45576,
                "title": {"english": "Mushoku Tensei: Jobless Reincarnation Part 2"},
                "episodes": 12,
                "startDate": {"year": 2021, "month": 10, "day": 4},
            },
        ],
    }

    with patch(
        "app.services.anilist.get_anime_root_and_hierarchy",
        new_callable=AsyncMock,
        return_value=mock_hierarchy,
    ):
        await reset_anime_metadata(db_session, item_id)

    # Verify
    stmt = (
        select(MediaItem)
        .where(MediaItem.id == item_id)
        .options(selectinload(MediaItem.seasons).selectinload(Season.episodes))
    )
    reloaded = (await db_session.execute(stmt)).scalars().first()
    assert reloaded is not None
    # Season 3 must be completely purged
    assert len(reloaded.seasons) == 2
    assert [s.season_number for s in reloaded.seasons] == [1, 2]

    # Season 1 and Season 2 episodes preserved
    reloaded_s1 = next(s for s in reloaded.seasons if s.season_number == 1)
    assert len(reloaded_s1.episodes) == 11
    assert all(e.status == EpisodeStatus.COMPLETED for e in reloaded_s1.episodes)

    reloaded_s2 = next(s for s in reloaded.seasons if s.season_number == 2)
    assert len(reloaded_s2.episodes) == 12
    assert all(e.status == EpisodeStatus.DOWNLOADED for e in reloaded_s2.episodes)


@pytest.mark.asyncio
async def test_reset_anime_metadata_missing_anilist_id_fallback(db_session):
    from app.db.models import Provider

    provider = Provider(name="Simkl", type="simkl", category="watchlist")
    db_session.add(provider)
    await db_session.flush()

    item = MediaItem(
        provider_id=provider.id,
        simkl_id=333333,
        title="Chainsaw Man",
        media_type=MediaType.ANIME,
        anilist_id=None,  # Missing
        status=MediaStatus.SEARCHING,
    )
    db_session.add(item)
    await db_session.commit()
    item_id = item.id

    mock_hierarchy = {
        "root": {
            "id": 127230,
            "idMal": 44511,
            "title": {"english": "Chainsaw Man", "romaji": "Chainsaw Man"},
            "episodes": 12,
            "startDate": {"year": 2022, "month": 10, "day": 12},
        },
        "hierarchy": [
            {
                "id": 127230,
                "idMal": 44511,
                "title": {"english": "Chainsaw Man", "romaji": "Chainsaw Man"},
                "episodes": 12,
                "startDate": {"year": 2022, "month": 10, "day": 12},
            }
        ],
    }

    with (
        patch(
            "app.services.anilist.search_anime_id_by_title",
            new_callable=AsyncMock,
            return_value=127230,
        ) as mock_search,
        patch(
            "app.services.anilist.get_anime_root_and_hierarchy",
            new_callable=AsyncMock,
            return_value=mock_hierarchy,
        ),
    ):
        await reset_anime_metadata(db_session, item_id)
        mock_search.assert_awaited_once_with("Chainsaw Man", None)

    # Verify ID and episodes were set
    stmt = (
        select(MediaItem)
        .where(MediaItem.id == item_id)
        .options(selectinload(MediaItem.seasons).selectinload(Season.episodes))
    )
    reloaded = (await db_session.execute(stmt)).scalars().first()
    assert reloaded is not None
    assert reloaded.anilist_id == 127230
    assert len(reloaded.seasons) == 1
    assert len(reloaded.seasons[0].episodes) == 12
