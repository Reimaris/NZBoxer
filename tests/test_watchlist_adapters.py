"""Watchlist adapter tests (m56): AniList mapping and fetch errors."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.db.models import Provider
from app.services import anilist
from app.services.watchlist_providers import (
    WatchlistEntry,
    WatchlistFetchError,
    get_adapter,
)

ANILIST_ENTRY = {
    "status": "CURRENT",
    "progress": 3,
    "media": {
        "id": 16498,
        "idMal": 16498,
        "format": "TV",
        "title": {"romaji": "Shingeki no Kyojin", "english": "Attack on Titan"},
        "startDate": {"year": 2013},
        "coverImage": {"large": "https://example.com/aot.jpg"},
    },
}


def _provider(token: str | None = None) -> Provider:
    return Provider(
        name="AniList",
        type="anilist",
        category="watchlist",
        username="bob",
        access_token=token,
    )


def _patch_transport(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(anilist.httpx, "AsyncClient", factory)

    async def no_wait():
        return None

    monkeypatch.setattr(anilist._limiter, "wait", no_wait)


async def test_anilist_entry_maps_to_watchlist_entry(monkeypatch):
    async def fake(username, access_token=None):
        assert (username, access_token) == ("bob", None)
        return [ANILIST_ENTRY]

    monkeypatch.setattr(anilist, "get_user_media_list", fake)
    entries = await get_adapter("anilist").fetch_entries(_provider())
    assert entries == [
        WatchlistEntry(
            remote_id="16498",
            media_kind="anime",
            title="Attack on Titan",
            year=2013,
            list_status="watching",
            watched_episodes=3,
            anilist_id=16498,
            mal_id=16498,
            poster_url="https://example.com/aot.jpg",
        )
    ]


@pytest.mark.parametrize(
    ("status", "expected_status"),
    [
        ("CURRENT", "watching"),
        ("REPEATING", "watching"),
        ("PAUSED", "watching"),
        ("COMPLETED", "completed"),
    ],
)
async def test_anilist_progress_maps_to_watched_episodes(
    monkeypatch, status, expected_status
):
    entry = {
        "status": status,
        "progress": 7,
        "media": {"id": 154587, "title": {"english": "Frieren"}, "format": "TV"},
    }

    async def fake(username, access_token=None):
        return [entry]

    monkeypatch.setattr(anilist, "get_user_media_list", fake)
    (result,) = await get_adapter("anilist").fetch_entries(_provider())
    assert (result.list_status, result.watched_episodes) == (expected_status, 7)


@pytest.mark.parametrize(
    ("status", "fmt", "expected_status", "expected_kind"),
    [
        ("PLANNING", "TV", "plantowatch", "anime"),
        ("REPEATING", "TV", "watching", "anime"),
        ("PAUSED", "TV", "watching", "anime"),
        ("COMPLETED", "MOVIE", "completed", "anime_movie"),
        ("DROPPED", "OVA", "dropped", "anime"),
    ],
)
async def test_anilist_status_and_format_mapping(
    monkeypatch, status, fmt, expected_status, expected_kind
):
    entry = {
        **ANILIST_ENTRY,
        "status": status,
        "media": {**ANILIST_ENTRY["media"], "format": fmt},
    }

    async def fake(username, access_token=None):
        return [entry]

    monkeypatch.setattr(anilist, "get_user_media_list", fake)
    (result,) = await get_adapter("anilist").fetch_entries(_provider())
    assert (result.list_status, result.media_kind) == (expected_status, expected_kind)


def test_get_adapter_simkl_has_fetch_entries():
    assert callable(get_adapter("simkl").fetch_entries)


def test_get_adapter_unknown_type():
    with pytest.raises(KeyError):
        get_adapter("bogus")


@pytest.mark.parametrize("status", [403, 404])
async def test_anilist_private_profile_error(monkeypatch, status):
    _patch_transport(monkeypatch, lambda req: httpx.Response(status, json={}))
    with pytest.raises(WatchlistFetchError, match="not found or private"):
        await anilist.get_user_media_list("bob")


async def test_anilist_rate_limit_error_mentions_retry_after(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(anilist.asyncio, "sleep", no_sleep)
    _patch_transport(
        monkeypatch, lambda req: httpx.Response(429, headers={"Retry-After": "42"})
    )
    with pytest.raises(WatchlistFetchError, match="42"):
        await anilist.get_user_media_list("bob")


@pytest.mark.parametrize(("token", "expected"), [(None, None), ("tok", "Bearer tok")])
async def test_anilist_authorization_header_only_with_token(
    monkeypatch, token, expected
):
    seen: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("Authorization"))
        body = {
            "data": {"MediaListCollection": {"lists": [{"entries": [ANILIST_ENTRY]}]}}
        }
        return httpx.Response(200, json=body)

    _patch_transport(monkeypatch, handler)
    result = await anilist.get_user_media_list("bob", token)
    assert seen == [expected]
    assert result == [ANILIST_ENTRY]


PLEX_ITEM = {
    "ratingKey": "5d776825",
    "type": "movie",
    "title": "The Matrix",
    "year": 1999,
    "Guid": [
        {"id": "imdb://tt0133093"},
        {"id": "tmdb://603"},
        {"id": "tvdb://169"},
    ],
}


def _plex_provider() -> Provider:
    return Provider(
        name="Plex",
        type="plex",
        category="watchlist",
        access_token="plex-tok",
        config_json='{"plex_client_identifier": "cid-1"}',
    )


def _patch_plex(monkeypatch, handler):
    from app.services.watchlists import plex

    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(plex.httpx, "AsyncClient", factory)


async def test_plex_entry_maps_ids_and_defaults(monkeypatch):
    from app.services.watchlists import plex

    seen: dict = {}

    async def fake(token, cid):
        seen.update(token=token, cid=cid)
        return [PLEX_ITEM, {"ratingKey": "9", "type": "episode", "title": "x"}]

    monkeypatch.setattr(plex, "get_watchlist", fake)
    entries = await get_adapter("plex").fetch_entries(_plex_provider())
    assert seen == {"token": "plex-tok", "cid": "cid-1"}
    assert entries == [
        WatchlistEntry(
            remote_id="5d776825",
            media_kind="movie",
            title="The Matrix",
            year=1999,
            list_status="plantowatch",
            watched_episodes=0,
            tmdb_id=603,
            imdb_id="tt0133093",
            tvdb_id=169,
        )
    ]


async def test_plex_entries_never_carry_progress(monkeypatch):
    from app.services.watchlists import plex

    shows = [
        {**PLEX_ITEM, "ratingKey": f"k{i}", "viewCount": 5, "viewOffset": 999}
        for i in range(2)
    ]
    monkeypatch.setattr(plex, "get_watchlist", AsyncMock(return_value=shows))
    entries = await get_adapter("plex").fetch_entries(_plex_provider())
    assert len(entries) == 2
    assert {(e.watched_episodes, e.list_status) for e in entries} == {
        (0, "plantowatch")
    }


async def test_plex_watchlist_paginates_with_headers(monkeypatch):
    from app.services.watchlists import plex

    starts: list[str] = []
    headers: list[httpx.Headers] = []

    def handler(req: httpx.Request) -> httpx.Response:
        start = int(req.url.params["X-Plex-Container-Start"])
        starts.append(str(start))
        headers.append(req.headers)
        assert req.url.params["includeGuids"] == "1"
        assert req.url.params["X-Plex-Container-Size"] == "50"
        n = 50 if start == 0 else 3
        meta = [{"ratingKey": str(start + i), "type": "movie"} for i in range(n)]
        return httpx.Response(
            200, json={"MediaContainer": {"totalSize": 53, "Metadata": meta}}
        )

    _patch_plex(monkeypatch, handler)
    items = await plex.get_watchlist("tok", "cid-1")
    assert len(items) == 53
    assert starts == ["0", "50"]
    assert headers[0]["X-Plex-Token"] == "tok"
    assert headers[0]["X-Plex-Client-Identifier"] == "cid-1"
    assert headers[0]["X-Plex-Product"] == "NZBoxer"
    assert headers[0]["User-Agent"]


async def test_plex_watchlist_401_raises(monkeypatch):
    from app.services.watchlists import plex

    _patch_plex(monkeypatch, lambda req: httpx.Response(401))
    with pytest.raises(WatchlistFetchError, match="reconnect"):
        await plex.get_watchlist("bad", "cid-1")


async def test_plex_pin_create_and_poll(monkeypatch):
    from app.services.watchlists import plex

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            assert req.url.params["strong"] == "true"
            return httpx.Response(200, json={"id": 7, "code": "ABCD"})
        assert req.url.path.endswith("/pins/7")
        return httpx.Response(200, json={"authToken": None})

    _patch_plex(monkeypatch, handler)
    assert await plex.create_pin("cid-1") == {"id": 7, "code": "ABCD"}
    assert await plex.poll_pin(7, "ABCD", "cid-1") is None


MAL_ITEM = {
    "node": {
        "id": 5114,
        "title": "Fullmetal Alchemist: Brotherhood",
        "media_type": "tv",
        "num_episodes": 64,
    },
    "list_status": {"status": "plan_to_watch", "num_episodes_watched": 0},
}


def _mal_provider(expires: str | None = None) -> Provider:
    from datetime import datetime, timedelta, timezone

    exp = expires or (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()
    return Provider(
        name="MAL",
        type="mal",
        category="watchlist",
        client_id="mal-cid",
        access_token="old-access",
        config_json=json.dumps(
            {"refresh_token": "old-refresh", "token_expires_at": exp}
        ),
    )


def _patch_mal(monkeypatch, handler):
    from app.services.watchlists import mal

    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(mal.httpx, "AsyncClient", factory)


def test_mal_pkce_pair_and_authorize_url():
    from app.services.watchlists import mal

    v1, c1 = mal.generate_pkce_pair()
    v2, _ = mal.generate_pkce_pair()
    assert len(v1) == 86 and v1 == c1 and v1 != v2
    url = mal.build_authorize_url("cid", c1, "st", "http://x/cb")
    assert "code_challenge_method=plain" in url and "client_secret" not in url


@pytest.mark.parametrize(
    ("status", "kind", "expected", "watched"),
    [
        ("plan_to_watch", "tv", "plantowatch", 0),
        ("on_hold", "tv", "watching", 3),
        ("completed", "movie", "completed", 1),
        ("dropped", "tv", "dropped", 2),
    ],
)
async def test_mal_adapter_mapping(monkeypatch, status, kind, expected, watched):
    from app.services import anilist
    from app.services.watchlists import mal

    item = {
        "node": {**MAL_ITEM["node"], "media_type": kind},
        "list_status": {"status": status, "num_episodes_watched": watched},
    }
    monkeypatch.setattr(mal, "get_animelist", AsyncMock(return_value=[item]))
    monkeypatch.setattr(
        anilist, "get_anime_ids_by_mal_ids", AsyncMock(return_value={5114: 5114})
    )
    (entry,) = await get_adapter("mal").fetch_entries(_mal_provider())
    assert entry.list_status == expected
    assert entry.watched_episodes == watched
    assert entry.media_kind == ("anime_movie" if kind == "movie" else "anime")
    assert (entry.mal_id, entry.anilist_id, entry.remote_id) == (5114, 5114, "5114")


async def test_mal_adapter_maps_main_picture_to_poster(monkeypatch):
    from app.services import anilist
    from app.services.watchlists import mal

    node = {
        **MAL_ITEM["node"],
        "main_picture": {
            "medium": "https://example.com/m.jpg",
            "large": "https://example.com/l.jpg",
        },
    }
    monkeypatch.setattr(
        mal, "get_animelist", AsyncMock(return_value=[{**MAL_ITEM, "node": node}])
    )
    monkeypatch.setattr(anilist, "get_anime_ids_by_mal_ids", AsyncMock(return_value={}))
    (entry,) = await get_adapter("mal").fetch_entries(_mal_provider())
    assert entry.poster_url == "https://example.com/l.jpg"


async def test_mal_refresh_near_expiry_persists_without_secret(monkeypatch, db_session):
    from datetime import datetime, timedelta, timezone

    from app.services.watchlists import mal

    forms: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        forms.append(req.content.decode())
        return httpx.Response(
            200,
            json={
                "access_token": "new-a",
                "refresh_token": "new-r",
                "expires_in": 2592000,
            },
        )

    _patch_mal(monkeypatch, handler)
    soon = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    prov = _mal_provider(soon)
    db_session.add(prov)
    await db_session.flush()
    assert await mal.ensure_valid_mal_token(db_session, prov) == "new-a"
    assert "grant_type=refresh_token" in forms[0] and "client_secret" not in forms[0]
    assert prov.access_token == "new-a"
    assert prov.watchlist_config["refresh_token"] == "new-r"

    # fresh token: no further refresh
    assert await mal.ensure_valid_mal_token(db_session, prov) == "new-a"
    assert len(forms) == 1


async def test_mal_401_triggers_single_refresh_retry(monkeypatch):
    from app.services import anilist
    from app.services.watchlists import mal

    monkeypatch.setattr(anilist, "get_anime_ids_by_mal_ids", AsyncMock(return_value={}))
    calls: list[str] = []

    async def fake_list(token):
        calls.append(token)
        if token == "old-access":
            raise mal.MalUnauthorizedError("x")
        return [MAL_ITEM]

    async def fake_ensure(session, provider, force=False):
        return "fresh" if force else "old-access"

    monkeypatch.setattr(mal, "get_animelist", fake_list)
    monkeypatch.setattr(mal, "ensure_valid_mal_token", fake_ensure)
    entries = await get_adapter("mal").fetch_entries(_mal_provider())
    assert calls == ["old-access", "fresh"] and len(entries) == 1


async def test_mal_get_animelist_follows_paging(monkeypatch):
    from app.services.watchlists import mal

    def handler(req: httpx.Request) -> httpx.Response:
        if "page2" in str(req.url):
            return httpx.Response(200, json={"data": [MAL_ITEM], "paging": {}})
        assert req.url.params["limit"] == "100"
        assert req.headers["Authorization"] == "Bearer t"
        nxt = "https://api.myanimelist.net/v2/users/@me/animelist?page2=1"
        return httpx.Response(200, json={"data": [MAL_ITEM], "paging": {"next": nxt}})

    _patch_mal(monkeypatch, handler)
    assert len(await mal.get_animelist("t")) == 2
