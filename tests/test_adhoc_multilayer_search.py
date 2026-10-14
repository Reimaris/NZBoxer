"""Ad-hoc multi-layer search: Tier 1 routing, caching and Tier 2 hierarchy (m52)."""

from typing import Any

import pytest

from app.core.ttl_cache import METADATA_CACHE_TTL_SECONDS
from app.services import media_search
from app.services.media_search import (
    classify_query,
    list_episodes,
    list_seasons,
    normalize_category,
    search_media,
)

TMDB_MOVIE_HIT = {
    "id": 603,
    "title": "The Matrix",
    "original_title": "The Matrix",
    "release_date": "1999-03-30",
    "overview": "A hacker learns the truth.",
    "poster_path": "/matrix.jpg",
}
TMDB_TV_HIT = {
    "id": 1396,
    "name": "Breaking Bad",
    "original_name": "Breaking Bad",
    "first_air_date": "2008-01-20",
    "overview": "A teacher turns to crime.",
    "poster_path": "/bb.jpg",
}
TMDB_SHOW_DETAILS = {
    "id": 1396,
    "name": "Breaking Bad",
    "seasons": [
        {
            "season_number": 0,
            "name": "Specials",
            "episode_count": 5,
            "air_date": "2009-02-17",
        },
        {
            "season_number": 1,
            "name": "Season 1",
            "episode_count": 7,
            "air_date": "2008-01-20",
        },
        {
            "season_number": 2,
            "name": "Season 2",
            "episode_count": 13,
            "air_date": "2009-03-08",
        },
    ],
}
TMDB_SEASON_DETAILS = {
    "episodes": [
        {"episode_number": 1, "name": "Pilot", "air_date": "2008-01-20"},
        {"episode_number": 2, "name": "Cat's in the Bag...", "air_date": "2008-01-27"},
    ]
}
TMDB_FIND_IMDB = {"movie_results": [TMDB_MOVIE_HIT], "tv_results": []}
ANILIST_HIT = {
    "id": 16498,
    "title": {
        "romaji": "Shingeki no Kyojin",
        "english": "Attack on Titan",
        "native": "進撃の巨人",
    },
    "synonyms": ["AoT"],
    "startDate": {"year": 2013},
    "format": "TV",
    "episodes": 25,
    "status": "FINISHED",
    "description": "Humanity fights titans.",
    "coverImage": {"large": "https://img/aot.jpg"},
    "nextAiringEpisode": None,
    "idMal": 16498,
}
ANILIST_MOVIE_HIT = {**ANILIST_HIT, "id": 9253, "format": "MOVIE", "episodes": 1}


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    media_search.clear_metadata_cache()


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch every tmdb/anilist call; `calls` counts invocations per name."""
    state: dict[str, Any] = {
        "calls": {},
        "search_movies": [TMDB_MOVIE_HIT],
        "search_tv": [TMDB_TV_HIT],
        "search_anime_media": [ANILIST_HIT],
        "find": TMDB_FIND_IMDB,
        "anime_by_id": ANILIST_HIT,
        "fail": set(),
    }

    def fake(name: str, key: str) -> Any:
        async def _inner(*args: Any, **kwargs: Any) -> Any:
            state["calls"][name] = state["calls"].get(name, 0) + 1
            if name in state["fail"]:
                raise RuntimeError(f"{name} down")
            return state[key]

        return _inner

    for mod, name, key in [
        ("tmdb", "search_movies", "search_movies"),
        ("tmdb", "search_tv", "search_tv"),
        ("tmdb", "find_all_by_imdb_id", "find"),
        ("anilist", "search_anime_media", "search_anime_media"),
        ("anilist", "get_anime_media_by_id", "anime_by_id"),
    ]:
        monkeypatch.setattr(f"app.services.media_search.{mod}.{name}", fake(name, key))

    async def show_details(*a: Any, **k: Any) -> Any:
        return TMDB_SHOW_DETAILS

    async def season_details(*a: Any, **k: Any) -> Any:
        return TMDB_SEASON_DETAILS

    monkeypatch.setattr("app.services.media_search.tmdb.get_show_details", show_details)
    monkeypatch.setattr(
        "app.services.media_search.tmdb.get_season_details", season_details
    )
    return state


@pytest.mark.parametrize(
    ("raw", "mode", "value"),
    [
        ("tt0133093", "imdb", "tt0133093"),
        ("TT0133093", "imdb", "tt0133093"),
        ("603", "numeric", "603"),
        ("the matrix", "title", "the matrix"),
        ("  ", "title", ""),
        ("tt", "title", "tt"),
    ],
)
def test_classify_query(raw: str, mode: str, value: str) -> None:
    q = classify_query(raw)
    assert (q.mode, q.value) == (mode, value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("any", "any"),
        ("movie", "movie"),
        ("series", "series"),
        ("anime", "anime"),
        ("anime-movie", "anime-movie"),
        ("", "any"),
        (None, "any"),
        ("Movies", "any"),
    ],
)
def test_normalize_category(raw: str | None, expected: str) -> None:
    assert normalize_category(raw) == expected


async def test_title_any_queries_all_branches_in_order(
    upstream: dict[str, Any],
) -> None:
    hits = await search_media("matrix")
    assert [h["kind"] for h in hits] == ["movie", "series", "anime"]
    assert upstream["calls"] == {
        "search_movies": 1,
        "search_tv": 1,
        "search_anime_media": 1,
    }
    movie = hits[0]
    assert movie["poster_url"] == "https://image.tmdb.org/t/p/w185/matrix.jpg"
    assert movie["year"] == 1999
    anime = hits[2]
    assert anime["title"] == "Attack on Titan"
    assert anime["year"] == 2013
    assert anime["anilist_id"] == 16498
    assert anime["poster_url"] == "https://img/aot.jpg"


async def test_failed_branch_is_skipped_and_not_cached(
    upstream: dict[str, Any],
) -> None:
    upstream["fail"] = {"search_anime_media"}
    hits = await search_media("matrix")
    assert [h["provider"] for h in hits] == ["tmdb", "tmdb"]
    upstream["fail"] = set()
    hits = await search_media("matrix")
    assert [h["provider"] for h in hits] == ["tmdb", "tmdb", "anilist"]


async def test_imdb_mode_returns_movie_hit(upstream: dict[str, Any]) -> None:
    hits = await search_media("tt0133093")
    assert [(h["kind"], h["ext_id"], h["imdb_id"]) for h in hits] == [
        ("movie", 603, "tt0133093")
    ]
    assert await search_media("tt0133093", "series") == []


async def test_imdb_anime_resolves_title_then_searches_anilist(
    upstream: dict[str, Any],
) -> None:
    hits = await search_media("tt0133093", "anime")
    assert [h["provider"] for h in hits] == ["anilist"]
    upstream["find"] = {"movie_results": [], "tv_results": []}
    media_search.clear_metadata_cache()
    assert await search_media("tt0000001", "anime") == []


async def test_advanced_ids_take_precedence(upstream: dict[str, Any]) -> None:
    hits = await search_media("ignored", "movie", imdb_id="tt0133093", tmdb_id=1)
    assert upstream["calls"] == {"find_all_by_imdb_id": 1}
    assert hits[0]["ext_id"] == 603


async def test_force_title_skips_numeric_mode(upstream: dict[str, Any]) -> None:
    await search_media("1917", "movie", force_title=True)
    assert upstream["calls"] == {"search_movies": 1}


async def test_numeric_anime_uses_anilist_id_lookup(upstream: dict[str, Any]) -> None:
    hits = await search_media("16498", "anime")
    assert upstream["calls"] == {"get_anime_media_by_id": 1}
    assert hits[0]["ext_id"] == 16498


@pytest.mark.parametrize(("category", "kept"), [("anime", 0), ("anime-movie", 1)])
async def test_anime_movie_filtering(
    upstream: dict[str, Any], category: str, kept: int
) -> None:
    upstream["search_anime_media"] = [ANILIST_MOVIE_HIT]
    hits = await search_media("movie", category)
    assert len(hits) == kept
    if kept:
        assert hits[0]["kind"] == "anime-movie"


async def test_tvdb_only_yields_no_hit_and_tags_hits(upstream: dict[str, Any]) -> None:
    assert await search_media("", "any", tvdb_id=81189) == []
    hits = await search_media("matrix", "movie", tvdb_id=81189)
    assert hits[0]["tvdb_id"] == 81189


async def test_cache_hit_then_expiry(
    upstream: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    monkeypatch.setattr("app.core.ttl_cache.time.monotonic", lambda: now[0])
    assert METADATA_CACHE_TTL_SECONDS == 600
    assert isinstance(METADATA_CACHE_TTL_SECONDS, int)

    await search_media("matrix", "movie")
    await search_media("matrix", "movie")
    assert upstream["calls"]["search_movies"] == 1
    now[0] += 601
    await search_media("matrix", "movie")
    assert upstream["calls"]["search_movies"] == 2


async def test_empty_results_are_not_cached(upstream: dict[str, Any]) -> None:
    upstream["search_movies"] = []
    await search_media("nothing", "movie")
    await search_media("nothing", "movie")
    assert upstream["calls"]["search_movies"] == 2


async def test_tmdb_series_seasons_omit_specials(upstream: dict[str, Any]) -> None:
    seasons = await list_seasons("tmdb", 1396, "series")
    assert [
        (s["season_number"], s["episode_count"], s["air_year"]) for s in seasons
    ] == [
        (1, 7, 2008),
        (2, 13, 2009),
    ]
    episodes = await list_episodes("tmdb", 1396, "series", 1)
    assert [(e["episode_number"], e["name"], e["air_date"]) for e in episodes] == [
        (1, "Pilot", "2008-01-20"),
        (2, "Cat's in the Bag...", "2008-01-27"),
    ]


async def test_anime_has_one_synthetic_season(upstream: dict[str, Any]) -> None:
    seasons = await list_seasons("anilist", 16498, "anime")
    assert seasons == [
        {"season_number": 1, "name": "Season 1", "episode_count": 25, "air_year": 2013}
    ]
    episodes = await list_episodes("anilist", 16498, "anime", 1)
    assert len(episodes) == 25
    assert episodes[0] == {"episode_number": 1, "name": "Episode 1", "air_date": None}
    assert await list_episodes("anilist", 16498, "anime", 2) == []


async def test_airing_anime_episode_count_and_movies(upstream: dict[str, Any]) -> None:
    upstream["anime_by_id"] = {
        **ANILIST_HIT,
        "episodes": None,
        "nextAiringEpisode": {"episode": 6},
    }
    seasons = await list_seasons("anilist", 16498, "anime")
    assert seasons[0]["episode_count"] == 5
    assert await list_seasons("tmdb", 603, "movie") == []
    assert await list_episodes("anilist", 9253, "anime-movie", 1) == []


# --------------------------------------------------------------------------
# Ad-hoc push (ticket 06)
# --------------------------------------------------------------------------

NEWZNAB_RELEASE = {
    "title": "Breaking.Bad.S01E01.1080p.BluRay.x265-GRP",
    "guid": "guid-bb-s01e01",
    "link": "https://indexer.example/getnzb/guid-bb-s01e01",
    "size": 3_000_000_000,
}


@pytest.fixture
async def push_env(db_session: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from app.config import settings
    from app.db.models import Provider, ProviderCategory
    from app.services.downloaders import torbox
    from app.services.indexers import newznab as newznab_client

    db_session.add(
        Provider(
            name="Idx",
            type="newznab",
            category=ProviderCategory.INDEXER.value,
            api_url="https://indexer.example/api",
            api_key="k",
            priority=1,
            is_active=True,
        )
    )
    await db_session.commit()

    state: dict[str, Any] = {"result": {"id": "tb-1", "hash": "h1"}}

    async def fetch(link: str, **kwargs: Any) -> tuple[bytes, str]:
        state["fetched"] = link
        return b"<nzb></nzb>", "x.nzb"

    async def send(*args: Any, **kwargs: Any) -> Any:
        return state["result"]

    monkeypatch.setattr(newznab_client, "fetch_nzb_bytes", fetch)
    monkeypatch.setattr(torbox, "send_nzb_file", send)
    monkeypatch.setattr(torbox, "get_cooldown_remaining", lambda is_manual=False: 0.0)
    monkeypatch.setattr(settings, "dry_run", False)
    return state


def _push_form(**overrides: str) -> dict[str, str]:
    form = {
        "release_link": NEWZNAB_RELEASE["link"],
        "nzb_title": NEWZNAB_RELEASE["title"],
        "nzb_guid": NEWZNAB_RELEASE["guid"],
        "score": "8000",
        "size_bytes": str(NEWZNAB_RELEASE["size"]),
        "media_title": "Breaking Bad",
        "media_year": "2008",
        "media_kind": "series",
        "tmdb_id": "1396",
        "season": "1",
        "episode": "1",
    }
    form.update(overrides)
    return form


async def _history_rows(db_session: Any) -> list[Any]:
    from sqlalchemy import select

    from app.db.models import DownloadHistory

    return list((await db_session.execute(select(DownloadHistory))).scalars().all())


async def test_push_success_creates_ad_hoc_row(
    async_client: Any, db_session: Any, push_env: dict[str, Any]
) -> None:
    resp = await async_client.post("/api/search/manual/push", data=_push_form())
    assert resp.status_code == 200
    db_session.expire_all()
    (row,) = await _history_rows(db_session)
    assert row.media_item_id is None
    assert row.is_ad_hoc is True
    assert row.nzb_title == NEWZNAB_RELEASE["title"]
    assert row.downloader_transfer_id == "tb-1"
    assert row.downloader_hash == "h1"
    assert (row.media_title, row.media_year, row.tmdb_id) == (
        "Breaking Bad",
        2008,
        1396,
    )
    assert (row.media_type_label, row.target_label) == ("series", "S01E01")
    assert row.status_detail == "downloading"
    assert row.is_dry_run is False


async def test_push_torbox_failure_returns_400_without_row(
    async_client: Any, db_session: Any, push_env: dict[str, Any]
) -> None:
    push_env["result"] = {"error": "TorBox down"}
    resp = await async_client.post("/api/search/manual/push", data=_push_form())
    assert resp.status_code == 400
    assert "TorBox down" in resp.text
    assert await _history_rows(db_session) == []


@pytest.mark.parametrize(
    "link",
    ["https://evil.example/getnzb/x", "file:///etc/passwd", "ftp://indexer.example/x"],
)
async def test_push_rejects_non_indexer_links(
    async_client: Any, db_session: Any, push_env: dict[str, Any], link: str
) -> None:
    resp = await async_client.post(
        "/api/search/manual/push", data=_push_form(release_link=link)
    )
    assert resp.status_code == 400
    assert "fetched" not in push_env
    assert await _history_rows(db_session) == []


async def test_push_accepts_indexer_subdomain_link(
    async_client: Any, push_env: dict[str, Any]
) -> None:
    link = "https://file.indexer.example/getnzb/abc?r=1"
    resp = await async_client.post(
        "/api/search/manual/push", data=_push_form(release_link=link)
    )
    assert resp.status_code == 200
    assert push_env["fetched"] == link


async def test_push_rejects_lookalike_host(
    async_client: Any, push_env: dict[str, Any]
) -> None:
    resp = await async_client.post(
        "/api/search/manual/push",
        data=_push_form(release_link="https://evilindexer.example/getnzb/x"),
    )
    assert resp.status_code == 400
    assert "fetched" not in push_env


async def test_status_span_error_is_flagged_for_htmx_swap(
    async_client: Any,
) -> None:
    resp = await async_client.post(
        "/api/search/manual/push",
        data=_push_form(release_link="https://evil.example/x"),
    )
    assert resp.status_code == 400
    assert resp.headers.get("X-Status-Span") == "1"


async def test_push_cooldown_blocks_without_row(
    async_client: Any,
    db_session: Any,
    push_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.downloaders import torbox

    monkeypatch.setattr(torbox, "get_cooldown_remaining", lambda is_manual=False: 30.0)
    resp = await async_client.post("/api/search/manual/push", data=_push_form())
    assert resp.status_code == 400
    assert "cooldown" in resp.text
    assert await _history_rows(db_session) == []


async def test_push_dry_run_creates_simulated_row(
    async_client: Any,
    db_session: Any,
    push_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "dry_run", True)
    caplog.set_level("INFO", logger="app.core.adhoc_search")
    resp = await async_client.post("/api/search/manual/push", data=_push_form())
    assert resp.status_code == 200
    assert "fetched" not in push_env
    db_session.expire_all()
    (row,) = await _history_rows(db_session)
    assert (row.is_dry_run, row.is_ad_hoc, row.status_detail) == (
        True,
        True,
        "simulated",
    )
    assert row.downloader_transfer_id is None
    assert any(
        "[DRY RUN] Simulated ad-hoc push" in r.getMessage() for r in caplog.records
    )


# --------------------------------------------------------------------------
# Auto-push & promote (ticket 07)
# --------------------------------------------------------------------------


def _scored(title: str, score: float, **extra: Any) -> dict[str, Any]:
    return {
        "title": title,
        "guid": f"guid-{title}",
        "link": f"https://indexer.example/getnzb/{title}",
        "size": 1_000,
        "score": score,
        **extra,
    }


def _auto_form(**overrides: str) -> dict[str, str]:
    form = {
        "scope": "episode",
        "kind": "series",
        "title": "Breaking Bad",
        "season": "1",
        "episode": "1",
    }
    form.update(overrides)
    return form


@pytest.fixture
def scored_results(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from app.core.adhoc_search import AdHocSearchResult

    state: dict[str, Any] = {
        "primary": [],
        "fallback": [],
        "mismatch": [],
        "error": None,
    }

    async def fake(**kwargs: Any) -> AdHocSearchResult:
        state["kwargs"] = kwargs
        return AdHocSearchResult(
            state["primary"], state["fallback"], state["mismatch"], state["error"]
        )

    monkeypatch.setattr("app.core.adhoc_search.search_and_score_releases", fake)
    return state


async def test_auto_push_prefers_primary_over_fallback(
    async_client: Any,
    db_session: Any,
    push_env: dict[str, Any],
    scored_results: dict[str, Any],
) -> None:
    scored_results["primary"] = [_scored("Best.Primary", 9000), _scored("Second", 100)]
    scored_results["fallback"] = [_scored("Fallback", 99999, is_fallback=True)]
    resp = await async_client.post("/api/search/manual/auto-push", data=_auto_form())
    assert resp.status_code == 200
    assert "Pushed best: Best.Primary" in resp.text
    assert scored_results["kwargs"]["season"] == 1
    assert scored_results["kwargs"]["episode"] == 1
    db_session.expire_all()
    (row,) = await _history_rows(db_session)
    assert (row.nzb_title, row.is_ad_hoc, row.target_label) == (
        "Best.Primary",
        True,
        "S01E01",
    )


async def test_auto_push_falls_back_and_never_pushes_mismatch(
    async_client: Any,
    db_session: Any,
    push_env: dict[str, Any],
    scored_results: dict[str, Any],
) -> None:
    scored_results["fallback"] = [_scored("Fallback", 50, is_fallback=True)]
    scored_results["mismatch"] = [_scored("Mismatch", 99999, is_mismatch=True)]
    resp = await async_client.post("/api/search/manual/auto-push", data=_auto_form())
    assert "Pushed best: Fallback" in resp.text
    db_session.expire_all()
    (row,) = await _history_rows(db_session)
    assert row.is_fallback is True

    scored_results["fallback"] = []
    resp = await async_client.post("/api/search/manual/auto-push", data=_auto_form())
    assert "No suitable release found." in resp.text
    db_session.expire_all()
    assert len(await _history_rows(db_session)) == 1


async def test_auto_push_validation_errors(
    async_client: Any, scored_results: dict[str, Any]
) -> None:
    for bad in (
        _auto_form(scope="bogus"),
        _auto_form(title=" "),
        _auto_form(season=""),
        _auto_form(episode=""),
    ):
        resp = await async_client.post("/api/search/manual/auto-push", data=bad)
        assert resp.status_code == 200
        assert "kwargs" not in scored_results


# --------------------------------------------------------------------------
# Ledger flag & autoclear exclusion (ticket 08)
# --------------------------------------------------------------------------


async def test_autoclear_pruned_rule_spares_ad_hoc_rows(db_session: Any) -> None:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.core.transfer_poller import autoclear_history_ledger
    from app.db.models import DownloadHistory
    from tests.conftest import make_history

    ad_hoc = await make_history(
        db_session, media_item_id=None, is_ad_hoc=True, status_detail="completed"
    )
    pruned = await make_history(
        db_session, media_item_id=None, is_ad_hoc=False, status_detail="completed"
    )
    ad_hoc_id, pruned_id = ad_hoc.id, pruned.id
    await db_session.commit()

    result = await autoclear_history_ledger(
        db_session, autoclear_pruned=True, retention_days=0
    )
    assert result["pruned_count"] == 1
    db_session.expire_all()
    ids = {r.id for r in (await db_session.execute(select(DownloadHistory))).scalars()}
    assert ids == {ad_hoc_id}
    assert pruned_id not in ids

    # The age rule still removes an old ad-hoc row.
    result = await autoclear_history_ledger(
        db_session,
        autoclear_pruned=True,
        retention_days=7,
        now=datetime.now(timezone.utc) + timedelta(days=30),
    )
    assert result["aged_count"] == 1


async def test_ledger_exposes_is_ad_hoc(db_session: Any) -> None:
    from app.core.transfer_poller import get_push_history_ledger
    from tests.conftest import make_history

    await make_history(
        db_session, media_item_id=None, is_ad_hoc=True, status_detail="completed"
    )
    await make_history(
        db_session, media_item_id=None, is_ad_hoc=False, status_detail="completed"
    )
    await db_session.commit()
    flags = sorted(e["is_ad_hoc"] for e in await get_push_history_ledger(db_session))
    assert flags == [False, True]


# --------------------------------------------------------------------------
# Tier 1 / Tier 2 UI (ticket 09)
# --------------------------------------------------------------------------

SEASONS_URL = "/api/search/manual/media/tmdb/1396/seasons?kind=series"


async def test_tier1_empty_input_shows_error(async_client: Any) -> None:
    resp = await async_client.post("/api/search/manual", data={"query": "  "})
    assert resp.status_code == 200
    assert "Please enter a title or ID." in resp.text


async def test_tier1_movie_hit_has_no_season_expander(
    async_client: Any, upstream: dict[str, Any]
) -> None:
    resp = await async_client.post(
        "/api/search/manual", data={"query": "matrix", "category": "movie"}
    )
    assert "The Matrix" in resp.text
    assert "1999" in resp.text
    assert "/seasons" not in resp.text


async def test_tier1_series_hit_links_to_seasons_url(
    async_client: Any, upstream: dict[str, Any]
) -> None:
    resp = await async_client.post(
        "/api/search/manual", data={"query": "breaking", "category": "series"}
    )
    assert "Breaking Bad" in resp.text
    assert "/api/search/manual/media/tmdb/1396/seasons?kind=series" in resp.text
    assert 'hx-trigger="click once"' in resp.text


async def test_tier1_numeric_query_offers_title_search(
    async_client: Any, upstream: dict[str, Any]
) -> None:
    resp = await async_client.post(
        "/api/search/manual", data={"query": "603", "category": "movie"}
    )
    assert "Search as title instead" in resp.text
    assert 'name="force_title"' in resp.text
    forced = await async_client.post(
        "/api/search/manual",
        data={"query": "1917", "category": "movie", "force_title": "1"},
    )
    assert "Search as title instead" not in forced.text


async def test_tier1_save_defaults_persists(
    async_client: Any, upstream: dict[str, Any], db_session: Any
) -> None:
    from app.db.models import SystemSettings

    db_session.add(SystemSettings(id=1))
    await db_session.commit()
    await async_client.post(
        "/api/search/manual",
        data={
            "query": "matrix",
            "category": "movie",
            "resolution": "2160p",
            "save_defaults": "true",
        },
    )
    db_session.expire_all()
    row = await db_session.get(SystemSettings, 1)
    assert row.scoring_settings["manual_search_defaults"]["resolution"] == "2160p"


async def test_tier2_seasons_partial(
    async_client: Any, upstream: dict[str, Any]
) -> None:
    resp = await async_client.get(f"{SEASONS_URL}&title=Breaking+Bad")
    assert resp.status_code == 200
    assert "Season 1" in resp.text and "Season 2" in resp.text
    assert "Specials" not in resp.text
    assert "/api/search/manual/media/tmdb/1396/seasons/1/episodes?kind=series" in (
        resp.text
    )


async def test_tier2_episodes_partial_lists_pilot(
    async_client: Any, upstream: dict[str, Any]
) -> None:
    resp = await async_client.get(
        "/api/search/manual/media/tmdb/1396/seasons/1/episodes?kind=series"
    )
    assert resp.status_code == 200
    assert "Pilot" in resp.text


async def test_tier2_invalid_provider_404(async_client: Any) -> None:
    resp = await async_client.get("/api/search/manual/media/imdb/1/seasons?kind=series")
    assert resp.status_code == 404
    resp = await async_client.get(
        "/api/search/manual/media/imdb/1/seasons/1/episodes?kind=series"
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Tier 3 drawers (ticket 10)
# --------------------------------------------------------------------------


@pytest.fixture
def releases_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from app.core.adhoc_search import AdHocSearchResult

    state: dict[str, Any] = {"primary": [], "error": None}

    async def fake(**kwargs: Any) -> AdHocSearchResult:
        state["kwargs"] = kwargs
        return AdHocSearchResult(state["primary"], [], [], state["error"])

    monkeypatch.setattr("app.core.adhoc_search.search_and_score_releases", fake)
    return state


def _release_form(**overrides: str) -> dict[str, str]:
    form = {
        "scope": "episode",
        "kind": "series",
        "title": "Breaking Bad",
        "tmdb_id": "1396",
        "season": "1",
        "episode": "1",
        "drawer_id": "drawer-tmdb-1396-s1-e1",
        "resolution": "2160p",
        "limit": "25",
    }
    form.update(overrides)
    return form


async def test_releases_drawer_renders_results_and_forwards_filters(
    async_client: Any, releases_spy: dict[str, Any]
) -> None:
    releases_spy["primary"] = [
        {**_scored(NEWZNAB_RELEASE["title"], 9000.0), "feature_pills": []}
    ]
    resp = await async_client.post("/api/search/manual/releases", data=_release_form())
    assert resp.status_code == 200
    assert NEWZNAB_RELEASE["title"] in resp.text
    assert "Primary Language Releases (1)" in resp.text
    assert "drawer-tmdb-1396-s1-e1" in resp.text
    kw = releases_spy["kwargs"]
    assert (kw["scope"], kw["season"], kw["episode"], kw["limit"]) == (
        "episode",
        1,
        1,
        25,
    )
    assert kw["filters"].resolution == "2160p"
    assert kw["tmdb_id"] == 1396


@pytest.mark.parametrize(
    "bad",
    [
        {"scope": "bogus"},
        {"title": " "},
        {"scope": "season", "season": ""},
        {"scope": "episode", "episode": "x"},
    ],
)
async def test_releases_validation_errors_render_in_drawer(
    async_client: Any, releases_spy: dict[str, Any], bad: dict[str, str]
) -> None:
    resp = await async_client.post(
        "/api/search/manual/releases", data=_release_form(**bad)
    )
    assert resp.status_code == 200
    assert "border-red-500/30" in resp.text
    assert "kwargs" not in releases_spy


async def test_releases_indexer_error_rendered(
    async_client: Any, releases_spy: dict[str, Any]
) -> None:
    releases_spy["error"] = "Indexer down"
    resp = await async_client.post("/api/search/manual/releases", data=_release_form())
    assert "Indexer down" in resp.text


async def test_rows_carry_search_buttons_with_drawer_ids(
    async_client: Any, upstream: dict[str, Any]
) -> None:
    hits = await async_client.post(
        "/api/search/manual", data={"query": "breaking", "category": "series"}
    )
    assert 'hx-post="/api/search/manual/releases"' in hits.text
    assert 'id="drawer-tmdb-1396"' in hits.text
    assert 'hx-include="#manual-search-form"' in hits.text
    seasons = await async_client.get(SEASONS_URL + "&title=Breaking+Bad")
    assert 'id="drawer-tmdb-1396-s1"' in seasons.text
    episodes = await async_client.get(
        "/api/search/manual/media/tmdb/1396/seasons/1/episodes?kind=series"
    )
    assert 'id="drawer-tmdb-1396-s1-e1"' in episodes.text


# --------------------------------------------------------------------------
# Push / auto-push / watchlist wiring (ticket 11)
# --------------------------------------------------------------------------


async def test_drawer_rows_post_to_push_with_context_and_auto_push(
    async_client: Any, releases_spy: dict[str, Any]
) -> None:
    releases_spy["primary"] = [
        {**_scored(NEWZNAB_RELEASE["title"], 9000.0), "feature_pills": []}
    ]
    html = (
        await async_client.post(
            "/api/search/manual/releases", data=_release_form(year="2008")
        )
    ).text
    assert 'hx-post="/api/search/manual/push"' in html
    assert 'hx-post="/api/search/manual/auto-push"' in html
    assert "/api/torbox/add" not in html
    for field in ("release_link", "nzb_title", "media_title", "media_year", "tmdb_id"):
        assert f'"{field}":' in html


async def test_hit_rows_do_not_offer_add_to_watchlist(
    async_client: Any, upstream: dict[str, Any]
) -> None:
    html = (
        await async_client.post(
            "/api/search/manual", data={"query": "matrix", "category": "movie"}
        )
    ).text
    assert "/api/watchlist/promote" not in html
    assert "Add to Watchlist" not in html


async def test_card4_badges(async_client: Any, db_session: Any) -> None:
    from tests.conftest import make_history

    await make_history(
        db_session,
        media_item_id=None,
        is_ad_hoc=True,
        status_detail="completed",
        is_dismissed=False,
        media_title="Adhoc Movie",
        tmdb_id=603,
    )
    await make_history(
        db_session,
        media_item_id=None,
        is_ad_hoc=False,
        status_detail="completed",
        is_dismissed=False,
        media_title="Pruned Movie",
    )
    await db_session.commit()
    html = (await async_client.get("/")).text
    assert ">Ad-Hoc<" in html
    assert html.count("Pruned from watchlist") == 1
    assert "Add to Watchlist" not in html
