from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.db.models import MediaItem, MediaType
from app.services import anilist
from tests.conftest import ingest_simkl_payloads, make_watchlist_provider


@pytest.mark.asyncio
async def test_search_anime_id_by_title_success():
    mock_response_data = {
        "data": {
            "Page": {
                "media": [
                    {
                        "id": 16498,
                        "title": {
                            "romaji": "Shingeki no Kyojin",
                            "english": "Attack on Titan",
                            "native": "進撃の巨人",
                        },
                        "startDate": {"year": 2013},
                        "format": "TV",
                    },
                    {
                        "id": 20958,
                        "title": {
                            "romaji": "Shingeki no Kyojin Season 2",
                            "english": "Attack on Titan Season 2",
                            "native": "進撃の巨人 Season 2",
                        },
                        "startDate": {"year": 2017},
                        "format": "TV",
                    },
                ]
            }
        }
    }

    with patch("app.services.anilist._post_with_retry") as mock_post:
        from unittest.mock import MagicMock

        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_response_data
        mock_post.return_value = mock_resp

        # Without year -> returns first match

        result_id = await anilist.search_anime_id_by_title("Attack on Titan")
        assert result_id == 16498

        # With year 2017 -> returns season 2 ID
        result_id_2017 = await anilist.search_anime_id_by_title(
            "Attack on Titan", year=2017
        )
        assert result_id_2017 == 20958


@pytest.mark.asyncio
async def test_anime_ingestion_bypasses_tmdb_and_uses_anilist(db_session):
    provider = await make_watchlist_provider(db_session, "simkl")
    simkl_items = [
        {
            "anime": {
                "title": "Frieren: Beyond Journey's End",
                "year": 2023,
                "ids": {"simkl": 12345, "anilist": 154587, "tmdb": 98765},
            }
        }
    ]

    with (
        patch("app.core.automation.tmdb.get_show_details") as mock_tmdb_show,
        patch("app.core.automation.tmdb.get_movie_details") as mock_tmdb_movie,
        patch("app.core.automation.tmdb.find_by_external_id") as mock_tmdb_find,
        patch(
            "app.core.automation.enrich_anime_metadata",
            new=AsyncMock(side_effect=lambda _session, item: item),
        ) as mock_enrich,
    ):
        await ingest_simkl_payloads(
            db_session, provider, simkl_items, MediaType.ANIME, maintain=False
        )

        # TMDB methods must NEVER be called for Anime
        mock_tmdb_show.assert_not_called()
        mock_tmdb_movie.assert_not_called()
        mock_tmdb_find.assert_not_called()
        mock_enrich.assert_awaited_once()

    item = (await db_session.execute(select(MediaItem))).scalars().one()
    assert item.anilist_id == 154587
    assert item.media_type == MediaType.ANIME
