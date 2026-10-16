"""Five-state watch model: constants, precedence and lifecycle (m69)."""

from __future__ import annotations

from itertools import combinations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.models import (
    ACTIVE_LIST_STATUSES,
    INACTIVE_LIST_STATUSES,
    LIST_STATUS_PRECEDENCE,
    MediaItem,
    best_list_status,
)
from app.services.watchlist_ingest import (
    mark_absent_sources_completed,
    prune_inactive_media_items,
)
from tests.conftest import make_movie, make_source, make_watchlist_provider

ORDER = ("watching", "on_hold", "plantowatch", "completed", "dropped")


def test_constants():
    assert LIST_STATUS_PRECEDENCE == ORDER
    assert ACTIVE_LIST_STATUSES == {"plantowatch", "watching", "on_hold"}
    assert INACTIVE_LIST_STATUSES == {"completed", "dropped"}


@pytest.mark.parametrize(("a", "b"), list(combinations(ORDER, 2)))
def test_best_list_status_pairs(a, b):
    assert best_list_status([b, a]) == a
    assert best_list_status([a, b]) == a


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], None),
        (["bogus"], None),
        (["bogus", "dropped"], "dropped"),
        (iter(["on_hold"]), "on_hold"),
    ],
)
def test_best_list_status_edges(statuses, expected):
    assert best_list_status(statuses) == expected


async def _load(session, item_id) -> MediaItem:
    return (
        await session.execute(
            select(MediaItem)
            .where(MediaItem.id == item_id)
            .options(selectinload(MediaItem.watchlist_sources))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


@pytest.mark.parametrize(
    ("statuses", "effective", "on_hold"),
    [
        ([], None, False),
        (["on_hold"], "on_hold", True),
        (["on_hold", "watching"], "watching", False),
        (["on_hold", "plantowatch"], "on_hold", True),
        (["completed", "dropped"], "completed", False),
    ],
)
async def test_item_effective_status(db_session, statuses, effective, on_hold):
    simkl = await make_watchlist_provider(db_session, "simkl")
    item = await make_movie(db_session, provider_id=simkl.id)
    for i, status in enumerate(statuses):
        prov = await make_watchlist_provider(db_session, "simkl", name=f"p{i}")
        await make_source(db_session, item, prov, status, f"r{i}")
    await db_session.commit()
    item = await _load(db_session, item.id)
    assert item.effective_list_status == effective
    assert item.is_list_on_hold is on_hold


async def test_absent_on_hold_source_completed_and_present_one_pruned_protected(
    db_session,
):
    simkl = await make_watchlist_provider(db_session, "simkl")
    held = await make_movie(db_session, provider_id=simkl.id, tmdb_id=1, simkl_id=1)
    absent = await make_movie(db_session, provider_id=simkl.id, tmdb_id=2, simkl_id=2)
    src_held = await make_source(db_session, held, simkl, "on_hold", "h")
    src_absent = await make_source(db_session, absent, simkl, "on_hold", "a")
    await db_session.commit()

    assert await mark_absent_sources_completed(db_session, simkl, {"h"}) == 1
    await db_session.refresh(src_held)
    await db_session.refresh(src_absent)
    assert (src_held.list_status, src_absent.list_status) == ("on_hold", "completed")

    assert await prune_inactive_media_items(db_session) == 1
    await db_session.commit()
    ids = set((await db_session.execute(select(MediaItem.id))).scalars().all())
    assert ids == {held.id}


async def test_simkl_get_watchlist_requests_all_five_statuses(monkeypatch):
    import httpx

    from app.services.watchlists import simkl

    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"shows": []})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        simkl.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr(simkl._limiter, "wait", lambda: _noop())
    await simkl.get_watchlist("shows", "test-id", "test-token")
    assert requested == ["plantowatch", "watching", "hold", "completed", "dropped"]


async def _noop() -> None:
    return None


def test_simkl_hold_row_maps_to_on_hold():
    from app.services.watchlists.simkl_adapter import _simkl_list_status

    assert _simkl_list_status("hold") == "on_hold"
