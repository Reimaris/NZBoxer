"""Simkl ``fetch_entries`` mapping: list status, progress counts and media kind.

Fields read from each Simkl list entry:

| Key                                    | Used for                                          |
|----------------------------------------|---------------------------------------------------|
| status                                 | ``list_status`` (default ``plantowatch``)         |
| seasons[].number (+ episodes)          | ``watched_season`` / ``watched_episodes``         |
| last_watched ("S02E01")                | ``watched_season``                                |
| watched_episodes_count / watched_episodes | ``watched_episodes``                           |
| movie / show / anime -> ids.*          | ``remote_id`` (simkl) and external ids            |
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import (
    Provider,
)

SIMKL_SHOW_WATCHING: dict[str, Any] = {
    "status": "watching",
    "last_watched": "S02E01",
    "watched_episodes_count": 9,
    "seasons": [
        {"number": 1, "episodes": [{"number": i} for i in range(1, 9)]},
        {"number": 2, "episodes": [{"number": 1}]},
    ],
    "show": {
        "title": "The Bear",
        "year": 2022,
        "ids": {"simkl": 73001, "tmdb": "83001"},
    },
}

SIMKL_ANIME_COMPLETED: dict[str, Any] = {
    "status": "completed",
    "anime": {
        "title": "Made in Abyss",
        "year": 2017,
        "ids": {"simkl": 74001, "anilist": "94001"},
    },
}

SIMKL_MOVIE_PLANTOWATCH: dict[str, Any] = {
    "status": "plantowatch",
    "movie": {
        "title": "Dune",
        "year": 2021,
        "ids": {"simkl": 75001, "tmdb": "85001"},
    },
}


def _fetch_patches(by_category: dict[str, list[dict[str, Any]]]):
    async def fake_fetch(session, provider, category, client_id, access_token):
        return by_category.get(category, []), client_id, access_token

    return (
        patch(
            "app.services.watchlists.simkl_adapter.simkl.ensure_valid_simkl_token",
            new=AsyncMock(return_value=("cid", "tok")),
        ),
        patch(
            "app.services.watchlists.simkl_adapter.simkl._fetch_simkl_category_with_retry",
            new=fake_fetch,
        ),
    )


async def _fetch(by_category: dict[str, list[dict[str, Any]]]):
    from app.services.watchlists.simkl_adapter import SimklWatchlistAdapter

    p1, p2 = _fetch_patches(by_category)
    with p1, p2:
        return await SimklWatchlistAdapter().fetch_entries(
            Provider(name="Simkl", type="simkl", category="watchlist")
        )


@pytest.mark.asyncio
async def test_fetch_entries_maps_progress_status_and_kind() -> None:
    entries = await _fetch(
        {
            "shows": [SIMKL_SHOW_WATCHING],
            "anime": [SIMKL_ANIME_COMPLETED],
            "movies": [SIMKL_MOVIE_PLANTOWATCH],
        }
    )
    by_id = {e.remote_id: e for e in entries}
    assert len(entries) == 3
    show = by_id["73001"]
    assert (show.media_kind, show.list_status, show.watched_episodes) == (
        "show",
        "watching",
        9,
    )
    assert show.tmdb_id == 83001
    anime = by_id["74001"]
    assert (anime.media_kind, anime.list_status, anime.anilist_id) == (
        "anime",
        "completed",
        94001,
    )
    movie = by_id["75001"]
    assert (movie.media_kind, movie.list_status, movie.watched_episodes) == (
        "movie",
        "plantowatch",
        0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("hold", "watching"),
        ("dropped", "dropped"),
        ("WATCHING", "watching"),
        (None, "plantowatch"),
        ("bogus", "plantowatch"),
    ],
)
async def test_fetch_entries_status_mapping(raw_status: str | None, expected: str):
    item = {
        "status": raw_status,
        "show": {"title": "X", "ids": {"simkl": 1, "tvdb": "55"}},
    }
    (entry,) = await _fetch({"shows": [item]})
    assert entry.list_status == expected
    assert entry.tvdb_id == 55


@pytest.mark.asyncio
async def test_fetch_entries_counts_progress_from_seasons_array() -> None:
    item = {
        "status": "watching",
        "seasons": [{"number": 1, "episodes": [{"number": 1}, {"number": 2}]}],
        "show": {"title": "X", "ids": {"simkl": 2}},
    }
    (entry,) = await _fetch({"shows": [item]})
    assert entry.watched_episodes == 2


@pytest.mark.asyncio
async def test_fetch_entries_propagates_retry_failure() -> None:
    from app.services.watchlists.simkl_adapter import SimklWatchlistAdapter

    boom = AsyncMock(side_effect=RuntimeError("401 twice"))
    with (
        patch(
            "app.services.watchlists.simkl_adapter.simkl.ensure_valid_simkl_token",
            new=AsyncMock(return_value=("cid", "tok")),
        ),
        patch(
            "app.services.watchlists.simkl_adapter.simkl._fetch_simkl_category_with_retry",
            new=boom,
        ),
        pytest.raises(RuntimeError),
    ):
        await SimklWatchlistAdapter().fetch_entries(
            Provider(name="Simkl", type="simkl", category="watchlist")
        )
