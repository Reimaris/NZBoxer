"""m68-05: best-effort Jellyseerr/Overseerr availability callback."""

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.db.models import (
    MediaItemWatchlistSource,
    SeasonStatus,
    SystemSettings,
)
from app.services.availability_notifier import notify_availability
from app.services.watchlists import jellyseerr
from tests.conftest import (
    make_history,
    make_movie,
    make_series,
    make_source,
    make_watchlist_provider,
)

KEY = "test-key-0123456789abcdef"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Route the real client through a MockTransport; return captured requests."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    real = httpx.AsyncClient

    def factory(*a: Any, **kw: Any) -> httpx.AsyncClient:
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(jellyseerr.httpx, "AsyncClient", factory)
    return seen


async def _setup(session, *, remote_id="9", notify=True, show=False, **src):  # type: ignore[no-untyped-def]
    prov = await make_watchlist_provider(
        session,
        "jellyseerr",
        config_json=json.dumps(
            {
                "server_url": "http://seerr.local:5055",
                "api_key": KEY,
                "notify_available": notify,
            }
        ),
    )
    if show:
        item, seasons, _ = await make_series(
            session,
            seasons=[
                {"status": SeasonStatus.COMPLETED},
                {"status": SeasonStatus.IGNORED},
            ],
        )
    else:
        item = await make_movie(session)
    await make_source(session, item, prov, "plantowatch", remote_id)
    hist = await make_history(session, item=item)
    await session.commit()
    return item, hist


async def _notified(session):  # type: ignore[no-untyped-def]
    session.expire_all()
    return (
        (await session.execute(select(MediaItemWatchlistSource)))
        .scalar_one()
        .availability_notified_at
    )


async def test_movie_notifies_once(db_session, calls, caplog) -> None:  # type: ignore[no-untyped-def]
    item, _ = await _setup(db_session)
    with caplog.at_level(logging.INFO):
        await notify_availability(item.id)
        await notify_availability(item.id)
    assert len(calls) == 1
    assert calls[0].url.path == "/api/v1/media/9/available"
    assert json.loads(calls[0].content) == {"is4k": False}
    assert await _notified(db_session) is not None
    assert "[AVAILABILITY] Notified" in caplog.text


async def test_completed_show_notifies_ignoring_ignored_seasons(
    db_session, calls
) -> None:  # type: ignore[no-untyped-def]
    item, _ = await _setup(db_session, show=True)
    await notify_availability(item.id)
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["disabled", "bad_remote", "open_season", "dry_run"])
async def test_no_call(db_session, calls, kind) -> None:  # type: ignore[no-untyped-def]
    item, _ = await _setup(
        db_session,
        notify=kind != "disabled",
        remote_id="wl-603" if kind == "bad_remote" else "9",
        show=kind == "open_season",
    )
    if kind == "open_season":
        from app.db.models import Season

        s = (await db_session.execute(select(Season))).scalars().first()
        assert s is not None
        s.status = SeasonStatus.SEARCHING
    if kind == "dry_run":
        settings = (
            await db_session.execute(select(SystemSettings))
        ).scalar_one_or_none()
        if settings is None:
            settings = SystemSettings(id=1)
            db_session.add(settings)
        settings.dry_run = True
    await db_session.commit()
    await notify_availability(item.id)
    assert calls == []
    assert await _notified(db_session) is None


@pytest.mark.parametrize("failure", ["http500", "timeout"])
async def test_failure_is_swallowed(db_session, monkeypatch, caplog, failure) -> None:  # type: ignore[no-untyped-def]
    item, hist = await _setup(db_session)

    async def boom(*a: Any, **k: Any) -> None:
        if failure == "timeout":
            raise asyncio.TimeoutError()
        req = httpx.Request("POST", "http://seerr.local:5055/x")
        raise httpx.HTTPStatusError(
            "500", request=req, response=httpx.Response(500, request=req)
        )

    monkeypatch.setattr(jellyseerr, "mark_available", boom)
    with caplog.at_level(logging.WARNING):
        await notify_availability(item.id)
    assert "[AVAILABILITY] jellyseerr" in caplog.text
    assert await _notified(db_session) is None
    await db_session.refresh(hist)
    assert hist.status_detail == "completed"
