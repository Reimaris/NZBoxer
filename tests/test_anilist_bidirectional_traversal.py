"""
Tests for AniList Bidirectional Traversal (Prequel Root & Sequel Hierarchy)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import anilist


@pytest.mark.asyncio
async def test_get_anime_root_and_hierarchy_success():
    """Verify bidirectional traversal starts at season 2, finds root, and builds hierarchy."""
    # We start at S2. S2 has a PREQUEL S1. S1 has a SEQUEL S2. S2 has a SEQUEL S3.
    # Movie is ignored.

    # Mocking responses
    def mock_post_with_retry(client, json_data):
        query_id = json_data["variables"]["id"]

        # Season 1 (Root)
        if query_id == 100:
            return AsyncMock(
                status_code=200,
                json=lambda: {
                    "data": {
                        "Media": {
                            "id": 100,
                            "type": "ANIME",
                            "format": "TV",
                            "title": {"romaji": "Root Season"},
                            "relations": {
                                "edges": [
                                    {
                                        "relationType": "SEQUEL",
                                        "node": {
                                            "id": 200,
                                            "type": "ANIME",
                                            "format": "TV",
                                            "title": {"romaji": "Season 2"},
                                        },
                                    },
                                    {
                                        "relationType": "SEQUEL",
                                        "node": {
                                            "id": 150,
                                            "type": "ANIME",
                                            "format": "MOVIE",
                                            "title": {"romaji": "Movie"},
                                        },
                                    },
                                ]
                            },
                        }
                    }
                },
            )
        # Season 2 (Starting Point)
        elif query_id == 200:
            return AsyncMock(
                status_code=200,
                json=lambda: {
                    "data": {
                        "Media": {
                            "id": 200,
                            "type": "ANIME",
                            "format": "TV",
                            "title": {"romaji": "Season 2"},
                            "relations": {
                                "edges": [
                                    {
                                        "relationType": "PREQUEL",
                                        "node": {
                                            "id": 100,
                                            "type": "ANIME",
                                            "format": "TV",
                                        },
                                    },
                                    {
                                        "relationType": "SEQUEL",
                                        "node": {
                                            "id": 300,
                                            "type": "ANIME",
                                            "format": "ONA",
                                        },
                                    },
                                ]
                            },
                        }
                    }
                },
            )
        # Season 3
        elif query_id == 300:
            return AsyncMock(
                status_code=200,
                json=lambda: {
                    "data": {
                        "Media": {
                            "id": 300,
                            "type": "ANIME",
                            "format": "ONA",
                            "title": {"romaji": "Season 3"},
                            "relations": {
                                "edges": [
                                    {
                                        "relationType": "PREQUEL",
                                        "node": {
                                            "id": 200,
                                            "type": "ANIME",
                                            "format": "TV",
                                        },
                                    }
                                ]
                            },
                        }
                    }
                },
            )

        return AsyncMock(json=lambda: {})

    with patch(
        "app.services.anilist._post_with_retry", side_effect=mock_post_with_retry
    ):
        result = await anilist.get_anime_root_and_hierarchy(200)

        assert result is not None
        assert result["root"]["id"] == 100

        hierarchy = result["hierarchy"]
        assert len(hierarchy) == 4
        assert hierarchy[0]["id"] == 100
        assert hierarchy[1]["id"] == 200
        assert hierarchy[2]["id"] == 150
        assert hierarchy[3]["id"] == 300
