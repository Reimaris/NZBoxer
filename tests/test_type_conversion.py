import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    DownloadHistory,
    MediaItem,
    MediaStatus,
    MediaType,
    Season,
    SeasonStatus,
)
from app.main import app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def setup_data(db_session: AsyncSession):
    # Create test data
    from app.db.models import Provider

    provider = Provider(name="Test", type="simkl", category="watchlist")
    db_session.add(provider)
    await db_session.flush()

    movie = MediaItem(
        provider_id=provider.id,
        simkl_id=1,
        title="Test Movie",
        media_type=MediaType.MOVIE,
        is_anime_movie=False,
        status=MediaStatus.PENDING,
    )
    db_session.add(movie)

    series = MediaItem(
        provider_id=provider.id,
        simkl_id=2,
        title="Test Series",
        media_type=MediaType.SHOW,
        status=MediaStatus.PENDING,
    )
    db_session.add(series)
    await db_session.flush()

    season = Season(
        media_item_id=series.id,
        season_number=1,
        monitored=True,
        status=SeasonStatus.PENDING,
    )
    db_session.add(season)
    await db_session.flush()

    dh1 = DownloadHistory(media_item_id=movie.id, nzb_title="test1", nzb_guid="test1")
    dh2 = DownloadHistory(
        media_item_id=series.id,
        season_id=season.id,
        nzb_title="test2",
        nzb_guid="test2",
    )
    db_session.add_all([dh1, dh2])

    await db_session.commit()

    return {"movie": movie.id, "series": series.id, "provider": provider.id}


async def test_convert_movie_to_series(setup_data, db_session: AsyncSession):
    movie_id = setup_data["movie"]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/items/{movie_id}/change-type", data={"target_type": "series"}
        )
        assert response.status_code in (200, 303)

    await db_session.refresh(await db_session.get(MediaItem, movie_id))
    item = await db_session.get(MediaItem, movie_id)

    assert item.media_type == MediaType.SHOW
    assert item.is_anime_movie == False

    # Season 1 should be created
    stmt = select(Season).where(Season.media_item_id == movie_id)
    seasons = (await db_session.execute(stmt)).scalars().all()
    assert len(seasons) == 1
    assert seasons[0].season_number == 1

    # DownloadHistory should be linked to season 1
    stmt = select(DownloadHistory).where(DownloadHistory.media_item_id == movie_id)
    dh = (await db_session.execute(stmt)).scalars().first()
    assert dh.season_id == seasons[0].id


async def test_convert_series_to_movie(setup_data, db_session: AsyncSession):
    series_id = setup_data["series"]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/items/{series_id}/change-type", data={"target_type": "movie"}
        )
        assert response.status_code in (200, 303)

    await db_session.refresh(await db_session.get(MediaItem, series_id))
    item = await db_session.get(MediaItem, series_id)
    assert item.media_type == MediaType.MOVIE
    assert item.is_anime_movie == False

    # Seasons should be deleted
    stmt = select(Season).where(Season.media_item_id == series_id)
    seasons = (await db_session.execute(stmt)).scalars().all()
    assert len(seasons) == 0

    # DownloadHistory should have season_id = None
    stmt = select(DownloadHistory).where(DownloadHistory.media_item_id == series_id)
    dh = (await db_session.execute(stmt)).scalars().first()
    assert dh.season_id is None


async def test_convert_movie_to_anime_movie(setup_data, db_session: AsyncSession):
    movie_id = setup_data["movie"]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/items/{movie_id}/change-type", data={"target_type": "anime-movie"}
        )
        assert response.status_code in (200, 303)

    await db_session.refresh(await db_session.get(MediaItem, movie_id))
    item = await db_session.get(MediaItem, movie_id)
    assert item.media_type == MediaType.MOVIE
    assert item.is_anime_movie == True


async def test_convert_series_to_anime(setup_data, db_session: AsyncSession):
    series_id = setup_data["series"]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/items/{series_id}/change-type", data={"target_type": "anime"}
        )
        assert response.status_code in (200, 303)

    await db_session.refresh(await db_session.get(MediaItem, series_id))
    item = await db_session.get(MediaItem, series_id)
    assert item.media_type == MediaType.ANIME
    assert item.is_anime_movie == False

    # Seasons should be preserved
    stmt = select(Season).where(Season.media_item_id == series_id)
    seasons = (await db_session.execute(stmt)).scalars().all()
    assert len(seasons) == 1
