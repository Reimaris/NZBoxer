"""m68-01: Jellyseerr/Overseerr provider model, entry contract, redaction."""

import json

import pytest
from sqlalchemy import text

from app.core.experimental import is_experimental
from app.core.log_redaction import install_model_listeners, redact_text
from app.db.database import _migrate_availability_notified
from app.db.models import Provider
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
