"""m68-01: Jellyseerr/Overseerr provider model, entry contract, redaction."""

import json

import pytest
from sqlalchemy import text

from app.core.experimental import is_experimental
from app.core.log_redaction import install_model_listeners, redact_text
from app.db.database import _migrate_availability_notified
from app.db.models import Episode, MediaItemWatchlistSource, Provider
from app.services.watchlist_providers import WatchlistEntry

KEY = "test-key-0123456789abcdef"


def _provider(ptype: str, **cfg: object) -> Provider:
    return Provider(
        name=ptype, type=ptype, category="watchlist", config_json=json.dumps(cfg)
    )


def test_watchlist_config_defaults() -> None:
    cfg = _provider("jellyseerr").watchlist_config
    assert cfg["server_url"] == ""
    assert cfg["api_key"] == ""
    assert cfg["sync_admin_watchlist"] is False
    assert cfg["notify_available"] is True


@pytest.mark.parametrize("ptype", ["jellyseerr", "overseerr"])
@pytest.mark.parametrize(
    ("cfg", "connected"),
    [
        ({"server_url": "http://seerr.local:5055", "api_key": KEY}, True),
        ({"server_url": "http://seerr.local:5055", "api_key": "  "}, False),
        ({"server_url": " ", "api_key": KEY}, False),
        ({}, False),
    ],
)
def test_connection_status(ptype: str, cfg: dict, connected: bool) -> None:
    p = _provider(ptype, **cfg)
    assert p.is_authenticated is connected
    assert p.watchlist_connection_status == (
        "connected" if connected else "not_connected"
    )


def test_entry_requested_seasons_default_and_value() -> None:
    base = {"remote_id": "1", "media_kind": "series", "title": "X"}
    assert WatchlistEntry(**base).requested_seasons is None  # type: ignore[arg-type]
    e = WatchlistEntry(**base, requested_seasons=frozenset({1, 2}))  # type: ignore[arg-type]
    assert e.requested_seasons == frozenset({1, 2})


@pytest.mark.parametrize("ptype", ["jellyseerr", "overseerr"])
def test_experimental(ptype: str) -> None:
    assert is_experimental(ptype)


async def test_api_key_in_config_json_redacted_after_save(db_session) -> None:
    install_model_listeners()
    db_session.add(_provider("jellyseerr", server_url="http://x", api_key=KEY))
    await db_session.commit()
    assert KEY not in redact_text(f"key {KEY}")


async def test_availability_notified_migration_idempotent(db_session) -> None:
    conn = await db_session.connection()
    await conn.execute(
        text("ALTER TABLE watchlist_sources DROP COLUMN availability_notified_at")
    )
    for _ in range(2):
        await _migrate_availability_notified(conn)
    cols = {
        r[1] for r in await conn.execute(text('PRAGMA table_info("watchlist_sources")'))
    }
    assert "availability_notified_at" in cols


# --- m68-02: HTTP client -----------------------------------------------------

import httpx  # noqa: E402

from app.config import DEFAULT_USER_AGENT  # noqa: E402
from app.services.watchlists import jellyseerr as jc  # noqa: E402

URL = "http://jellyseerr.example.com:5055"


@pytest.fixture
def seerr_http(monkeypatch):
    """Route the client through a MockTransport; returns (requests, set_handler)."""
    seen: list[httpx.Request] = []
    box = {"handler": lambda r: httpx.Response(200, json={})}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return box["handler"](request)

    real = httpx.AsyncClient

    def factory(**kw):
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(jc.httpx, "AsyncClient", factory)
    jc._title_cache.clear()

    def set_handler(fn):
        box["handler"] = fn

    return seen, set_handler


async def test_approved_requests_paginate_and_filter(seerr_http) -> None:
    seen, set_handler = seerr_http

    def handler(request: httpx.Request) -> httpx.Response:
        skip = int(request.url.params["skip"])
        if skip == 0:
            results = [{"id": i, "status": 2} for i in range(99)] + [
                {"id": 999, "status": 1}
            ]
            results.append({"id": 1000, "status": 2})
        else:
            results = [{"id": 2000, "status": 2}, {"id": 2001, "status": 3}]
        return httpx.Response(200, json={"results": results})

    set_handler(handler)
    out = await jc.get_approved_requests(URL, KEY)
    assert [r["id"] for r in out][-2:] == [1000, 2000]
    assert len(out) == 101
    assert [r.url.params["skip"] for r in seen] == ["0", "100"]
    first = seen[0]
    assert first.headers["X-Api-Key"] == KEY
    assert first.headers["Accept"] == "application/json"
    assert first.headers["User-Agent"] == DEFAULT_USER_AGENT
    assert first.url.path == "/api/v1/request"
    assert first.url.params["filter"] == "approved"
    assert first.url.params["take"] == "100"


@pytest.mark.parametrize("code", [401, 403])
async def test_unauthorized(seerr_http, code: int) -> None:
    _, set_handler = seerr_http
    set_handler(lambda r: httpx.Response(code))
    with pytest.raises(jc.JellyseerrUnauthorizedError):
        await jc.get_approved_requests(URL, KEY)


async def test_admin_watchlist_pages(seerr_http) -> None:
    seen, set_handler = seerr_http

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={"pageInfo": {"pages": 2}, "results": [{"tmdbId": page}]},
        )

    set_handler(handler)
    out = await jc.get_admin_watchlist(URL + "/", KEY)
    assert [r["tmdbId"] for r in out] == [1, 2]
    assert seen[0].url.path == "/api/v1/user/watchlist"


@pytest.mark.parametrize(
    ("media_type", "payload", "path", "expected"),
    [
        (
            "movie",
            {"title": "The Matrix", "releaseDate": "1999-03-30"},
            "/api/v1/movie/603",
            ("The Matrix", 1999),
        ),
        (
            "tv",
            {"name": "Severance", "firstAirDate": "2022-02-18"},
            "/api/v1/tv/603",
            ("Severance", 2022),
        ),
        (
            "tv",
            {"name": "Undated", "firstAirDate": ""},
            "/api/v1/tv/603",
            ("Undated", None),
        ),
    ],
)
async def test_get_title(seerr_http, media_type, payload, path, expected) -> None:
    seen, set_handler = seerr_http
    set_handler(lambda r: httpx.Response(200, json=payload))
    assert await jc.get_title(URL, KEY, media_type, 603) == expected
    assert seen[0].url.path == path
    await jc.get_title(URL, KEY, media_type, 603)
    assert len(seen) == 1  # cached


async def test_mark_available(seerr_http) -> None:
    seen, set_handler = seerr_http
    set_handler(lambda r: httpx.Response(204))
    await jc.mark_available(URL, KEY, 42)
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/media/42/available"
    assert json.loads(req.content) == {"is4k": False}
    set_handler(lambda r: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await jc.mark_available(URL, KEY, 42)


# --- m68-03: adapter + ingest ------------------------------------------------

from unittest.mock import AsyncMock  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.db.models import (  # noqa: E402
    DownloadHistory,
    MediaItem,
    MediaStatus,
)
from app.services import anilist, tmdb  # noqa: E402
from app.services.watchlist_ingest import (  # noqa: E402
    ingest_entries,
    mark_absent_sources_completed,
    prune_inactive_media_items,
)
from app.services.watchlist_providers import (  # noqa: E402
    WatchlistFetchError,
    get_adapter,
)
from tests.conftest import make_history, make_watchlist_provider  # noqa: E402

TV_REQ = {
    "id": 12,
    "status": 2,
    "type": "tv",
    "media": {"id": 7, "tmdbId": 1399, "tvdbId": 121361},
    "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}],
}
MOVIE_REQ = {
    "id": 15,
    "status": 2,
    "type": "movie",
    "media": {"id": 9, "tmdbId": 603},
    "seasons": [],
}


@pytest.fixture
def seerr_api(monkeypatch):
    """Patch client functions; returns a dict the test can tweak."""
    state: dict = {"requests": [TV_REQ, MOVIE_REQ], "watchlist": [], "wl_calls": 0}

    async def requests(url, key):
        return state["requests"]

    async def watchlist(url, key):
        state["wl_calls"] += 1
        return state["watchlist"]

    async def title(url, key, media_type, tmdb_id):
        if state.get("title_error"):
            raise httpx.ConnectError("boom")
        return (
            ("The Matrix", 1999) if media_type == "movie" else ("Game of Thrones", 2011)
        )

    monkeypatch.setattr(jc, "get_approved_requests", requests)
    monkeypatch.setattr(jc, "get_admin_watchlist", watchlist)
    monkeypatch.setattr(jc, "get_title", title)
    return state


async def _seerr_provider(db_session, **cfg):
    cfg = {"server_url": URL, "api_key": KEY, **cfg}
    return await make_watchlist_provider(
        db_session, "jellyseerr", config_json=json.dumps(cfg)
    )


async def test_adapter_maps_requests(db_session, seerr_api) -> None:
    seerr_api["requests"] = [
        TV_REQ,
        MOVIE_REQ,
        {"id": 13, "status": 1, "type": "movie", "media": {"id": 8, "tmdbId": 604}},
        {"id": 16, "status": 2, "type": "movie", "media": {"id": 10, "tmdbId": 0}},
    ]
    entries = await get_adapter("jellyseerr").fetch_entries(
        await _seerr_provider(db_session)
    )
    by_id = {e.remote_id: e for e in entries}
    assert set(by_id) == {"7", "9"}  # TMDB-less request skipped
    tv = by_id["7"]
    assert (tv.media_kind, tv.tmdb_id, tv.tvdb_id) == ("show", 1399, 121361)
    assert tv.list_status == "plantowatch"
    assert tv.requested_seasons == frozenset({1, 2})
    assert (tv.title, tv.year) == ("Game of Thrones", 2011)
    movie = by_id["9"]
    assert (movie.media_kind, movie.tvdb_id, movie.requested_seasons) == (
        "movie",
        None,
        None,
    )


def test_adapters_shared() -> None:
    assert get_adapter("jellyseerr") is get_adapter("overseerr")


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ([{"seasonNumber": 3}], frozenset({1, 2, 3})),
        ([], None),
    ],
)
async def test_merged_requests(db_session, seerr_api, extra, expected) -> None:
    second = {**TV_REQ, "id": 99, "seasons": extra}
    seerr_api["requests"] = [TV_REQ, second]
    entries = await get_adapter("jellyseerr").fetch_entries(
        await _seerr_provider(db_session)
    )
    assert len(entries) == 1
    assert entries[0].requested_seasons == expected


async def test_title_fallback(db_session, seerr_api) -> None:
    seerr_api["title_error"] = True
    entries = await get_adapter("jellyseerr").fetch_entries(
        await _seerr_provider(db_session)
    )
    tv = next(e for e in entries if e.remote_id == "7")
    assert (tv.title, tv.year) == ("TMDB 1399", None)


async def test_admin_watchlist(db_session, seerr_api) -> None:
    seerr_api["watchlist"] = [
        {"tmdbId": 603, "mediaType": "movie", "title": "The Matrix"},  # duplicate
        {"tmdbId": 27205, "mediaType": "movie", "title": "Inception"},
    ]
    adapter = get_adapter("jellyseerr")
    off = await adapter.fetch_entries(await _seerr_provider(db_session))
    assert seerr_api["wl_calls"] == 0
    assert {e.remote_id for e in off} == {"7", "9"}

    on = await adapter.fetch_entries(
        await _seerr_provider(db_session, sync_admin_watchlist=True)
    )
    assert seerr_api["wl_calls"] == 1
    extra = [e for e in on if e.remote_id.startswith("wl-")]
    assert [(e.remote_id, e.title, e.tvdb_id) for e in extra] == [
        ("wl-27205", "Inception", None)
    ]


async def test_adapter_errors(db_session, monkeypatch) -> None:
    provider = await _seerr_provider(db_session)

    async def unauthorized(url, key):
        raise jc.JellyseerrUnauthorizedError("HTTP 401")

    monkeypatch.setattr(jc, "get_approved_requests", unauthorized)
    with pytest.raises(WatchlistFetchError, match="API key rejected"):
        await get_adapter("jellyseerr").fetch_entries(provider)

    async def broken(url, key):
        raise httpx.ConnectError(f"cannot reach {KEY}")

    monkeypatch.setattr(jc, "get_approved_requests", broken)
    install_model_listeners()
    await db_session.commit()
    with pytest.raises(WatchlistFetchError) as exc:
        await get_adapter("jellyseerr").fetch_entries(provider)
    assert KEY not in str(exc.value)


@pytest.fixture
def _no_metadata(monkeypatch):
    monkeypatch.setattr(tmdb, "get_movie_details", AsyncMock(return_value=None))
    monkeypatch.setattr(tmdb, "get_show_details", AsyncMock(return_value=None))
    monkeypatch.setattr(tmdb, "get_digital_release_date", AsyncMock(return_value=None))
    monkeypatch.setattr(anilist, "get_anime_ids_by_mal_ids", AsyncMock(return_value={}))


async def _sync(db_session, provider, seerr_api) -> set[str]:
    entries = await get_adapter("jellyseerr").fetch_entries(provider)
    await ingest_entries(db_session, provider, entries)
    seen = {e.remote_id for e in entries}
    await mark_absent_sources_completed(db_session, provider, seen)
    await prune_inactive_media_items(db_session)
    await db_session.commit()
    return seen


async def test_ingest_creates_pending_without_dispatch(
    db_session, seerr_api, _no_metadata
) -> None:
    provider = await _seerr_provider(db_session)
    await _sync(db_session, provider, seerr_api)
    items = (await db_session.execute(select(MediaItem))).scalars().all()
    assert len(items) == 2
    # Created PENDING; the standard enrichment may settle on FUTURE/SEARCHING,
    # but nothing is ever dispatched.
    assert {i.status for i in items} <= {
        MediaStatus.PENDING,
        MediaStatus.FUTURE,
        MediaStatus.SEARCHING,
    }
    assert not (await db_session.execute(select(DownloadHistory))).scalars().all()
    sources = (await db_session.execute(select(MediaItemWatchlistSource))).scalars()
    assert {(s.remote_id, s.list_status) for s in sources} == {
        ("7", "plantowatch"),
        ("9", "plantowatch"),
    }


async def test_departed_request_prunes_item_keeps_history(
    db_session, seerr_api, _no_metadata
) -> None:
    provider = await _seerr_provider(db_session)
    await _sync(db_session, provider, seerr_api)
    movie = (
        await db_session.execute(select(MediaItem).where(MediaItem.tmdb_id == 603))
    ).scalar_one()
    await make_history(db_session, item=movie)
    await db_session.commit()

    seerr_api["requests"] = [TV_REQ]
    await _sync(db_session, provider, seerr_api)

    tmdb_ids = {
        i.tmdb_id for i in (await db_session.execute(select(MediaItem))).scalars()
    }
    assert tmdb_ids == {1399}
    assert len((await db_session.execute(select(DownloadHistory))).scalars().all()) == 1


async def test_departed_request_with_active_transfer_not_pruned(
    db_session, seerr_api, _no_metadata
) -> None:
    provider = await _seerr_provider(db_session)
    await _sync(db_session, provider, seerr_api)
    movie = (
        await db_session.execute(select(MediaItem).where(MediaItem.tmdb_id == 603))
    ).scalar_one()
    movie.status = MediaStatus.DOWNLOADING
    await db_session.commit()

    seerr_api["requests"] = [TV_REQ]
    await _sync(db_session, provider, seerr_api)

    assert (
        await db_session.execute(select(MediaItem).where(MediaItem.tmdb_id == 603))
    ).scalar_one_or_none() is not None


# ---- m68-04: partial-season request handling -------------------------------

from sqlalchemy import select as _select  # noqa: E402

from app.db.models import EpisodeStatus, MediaType, SeasonStatus  # noqa: E402
from app.services.watchlist_ingest import _apply_requested_seasons  # noqa: E402
from tests.conftest import (  # noqa: E402
    make_series,
    make_source,
)


def _req(seasons: set[int] | None) -> WatchlistEntry:
    return WatchlistEntry(
        remote_id="r1",
        media_kind="show",
        title="Show",
        tvdb_id=1,
        requested_seasons=None if seasons is None else frozenset(seasons),
    )


async def _statuses(session, item) -> dict[int, SeasonStatus]:  # type: ignore[no-untyped-def]
    await session.refresh(item, ["seasons"])
    return {s.season_number: s.status for s in item.seasons}


async def _show(session, statuses=None):  # type: ignore[no-untyped-def]
    item, seasons, eps = await make_series(
        session,
        seasons=[
            {"season_number": n, "status": st}
            for n, st in enumerate(
                statuses or [SeasonStatus.SEARCHING] * 4
            )  # index 0 = season 0
        ],
        episodes_per_season=2,
    )
    await session.refresh(item, ["seasons"])
    return item, seasons


async def test_requested_seasons_search_only_requested_then_expand(db_session) -> None:  # type: ignore[no-untyped-def]
    item, _ = await _show(db_session)
    await _apply_requested_seasons(db_session, item, _req({1}))
    assert await _statuses(db_session, item) == {
        0: SeasonStatus.SEARCHING,
        1: SeasonStatus.SEARCHING,
        2: SeasonStatus.IGNORED,
        3: SeasonStatus.IGNORED,
    }
    eps = (await db_session.execute(_select(Episode))).scalars().all()
    s2 = next(s for s in item.seasons if s.season_number == 2)
    assert {e.status for e in eps if e.season_id == s2.id} == {EpisodeStatus.IGNORED}

    await _apply_requested_seasons(db_session, item, _req({1, 2}))
    await _apply_requested_seasons(db_session, item, _req({1, 2}))  # idempotent
    st = await _statuses(db_session, item)
    assert st[2] == SeasonStatus.SEARCHING and st[3] == SeasonStatus.IGNORED
    eps = (await db_session.execute(_select(Episode))).scalars().all()
    assert {e.status for e in eps if e.season_id == s2.id} == {EpisodeStatus.PENDING}


async def test_requested_seasons_leaves_finished_and_history_seasons(
    db_session,
) -> None:  # type: ignore[no-untyped-def]
    item, seasons = await _show(
        db_session,
        [
            SeasonStatus.SEARCHING,
            SeasonStatus.COMPLETED,
            SeasonStatus.SEARCHING,
            SeasonStatus.PENDING,
        ],
    )
    await make_history(db_session, item=item, season=seasons[2])
    await _apply_requested_seasons(db_session, item, _req(set()))
    assert await _statuses(db_session, item) == {
        0: SeasonStatus.SEARCHING,
        1: SeasonStatus.COMPLETED,
        2: SeasonStatus.SEARCHING,
        3: SeasonStatus.IGNORED,
    }


async def test_requested_seasons_skipped_with_other_active_source(db_session) -> None:  # type: ignore[no-untyped-def]
    item, _ = await _show(db_session)
    plex = await make_watchlist_provider(db_session, "plex")
    await make_source(db_session, item, plex, "plantowatch", "p1")
    await _apply_requested_seasons(db_session, item, _req({1}))
    assert set((await _statuses(db_session, item)).values()) == {SeasonStatus.SEARCHING}

    # an inactive other source no longer blocks
    plex_src = (
        await db_session.execute(_select(MediaItemWatchlistSource))
    ).scalar_one()
    plex_src.list_status = "completed"
    await db_session.flush()
    await _apply_requested_seasons(db_session, item, _req({1}))
    assert (await _statuses(db_session, item))[2] == SeasonStatus.IGNORED


async def test_requested_seasons_noop_for_none_and_movies(db_session) -> None:  # type: ignore[no-untyped-def]
    item, _ = await _show(db_session)
    await _apply_requested_seasons(db_session, item, _req(None))
    assert set((await _statuses(db_session, item)).values()) == {SeasonStatus.SEARCHING}
    item.media_type = MediaType.MOVIE
    await _apply_requested_seasons(db_session, item, _req({1}))
    assert set((await _statuses(db_session, item)).values()) == {SeasonStatus.SEARCHING}
