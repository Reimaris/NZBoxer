import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MediaItem, MediaStatus, MediaType, Provider
from tests.conftest import ingest_simkl_payloads

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def setup_data(db_session: AsyncSession):
    provider = Provider(name="Test", type="simkl", category="watchlist")
    db_session.add(provider)
    await db_session.flush()

    movie = MediaItem(
        provider_id=provider.id,
        simkl_id=10,
        anilist_id=500,
        title="Existing Movie",
        media_type=MediaType.MOVIE,
        is_anime_movie=True,
        status=MediaStatus.COMPLETED,
    )
    db_session.add(movie)

    series = MediaItem(
        provider_id=provider.id,
        simkl_id=20,
        title="Existing Series",
        media_type=MediaType.SHOW,
        status=MediaStatus.COMPLETED,
    )
    db_session.add(series)

    anime = MediaItem(
        provider_id=provider.id,
        simkl_id=30,
        title="Existing Anime",
        media_type=MediaType.ANIME,
        status=MediaStatus.COMPLETED,
    )
    db_session.add(anime)

    await db_session.commit()
    return {
        "movie": movie.id,
        "series": series.id,
        "anime": anime.id,
        "provider": provider.id,
    }


async def test_watchlist_sync_immunity(setup_data, db_session: AsyncSession):
    # Mock simkl returning an anime (which in our DB is an anime-movie)
    simkl_list = [
        {
            "anime": {
                "title": "Existing Movie",
                "year": 2020,
                "ids": {"simkl": 10, "anilist": 500},
            }
        }
    ]

    provider = await db_session.get(Provider, setup_data["provider"])
    await ingest_simkl_payloads(
        db_session, provider, simkl_list, MediaType.ANIME, maintain=False
    )

    # Refresh DB state
    item = await db_session.get(MediaItem, setup_data["movie"])
    # Should STILL be MOVIE and is_anime_movie=True, not overwritten by the sync function
    assert item.media_type == MediaType.MOVIE
    assert item.is_anime_movie is True
