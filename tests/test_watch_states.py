"""Five-state watch model: constants, precedence and lifecycle (m69)."""

from __future__ import annotations

from datetime import datetime, timezone
from itertools import combinations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.automation import (
    classify_v3_status_tier,
    has_future_seasons,
    is_awaiting_seasons,
    latest_released_order,
)
from app.db.models import (
    ACTIVE_LIST_STATUSES,
    INACTIVE_LIST_STATUSES,
    LIST_STATUS_PRECEDENCE,
    MediaItem,
    Season,
    best_list_status,
)
from app.services.watchlist_ingest import (
    mark_absent_sources_completed,
    prune_inactive_media_items,
)
from tests.conftest import make_movie, make_series, make_source, make_watchlist_provider

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


def _entry(remote_id: str, status: str, tmdb_id: int = 603):
    from app.services.watchlist_providers import WatchlistEntry

    return WatchlistEntry(
        remote_id=remote_id,
        media_kind="movie",
        title="Example Movie",
        list_status=status,  # type: ignore[arg-type]
        tmdb_id=tmdb_id,
    )


@pytest.mark.parametrize("status", ["completed", "dropped"])
async def test_inactive_unmatched_entry_creates_no_item(db_session, status):
    from app.services.watchlist_ingest import ingest_entries

    provider = await make_watchlist_provider(db_session, "simkl")
    result = await ingest_entries(db_session, provider, [_entry("x1", status)])
    assert result.added == 0
    assert (await db_session.execute(select(MediaItem.id))).first() is None


async def test_inactive_matched_entry_updates_source(db_session):
    from app.db.models import MediaItemWatchlistSource
    from app.services.watchlist_ingest import ingest_entries

    provider = await make_watchlist_provider(db_session, "simkl")
    item = await make_movie(db_session, provider_id=provider.id, tmdb_id=603)
    src = await make_source(db_session, item, provider, "plantowatch", "x1")
    await db_session.commit()

    await ingest_entries(db_session, provider, [_entry("x1", "completed")])
    src = (await db_session.execute(select(MediaItemWatchlistSource))).scalar_one()
    assert src.list_status == "completed"


@pytest.mark.parametrize(
    ("existing", "incoming", "expected"),
    [
        ("watching", "plantowatch", "watching"),
        ("on_hold", "completed", "on_hold"),
        ("plantowatch", "on_hold", "on_hold"),
        ("completed", "watching", "watching"),
    ],
)
async def test_consolidated_ids_never_regress_active_status(
    db_session, existing, incoming, expected
):
    from app.db.models import MediaItemWatchlistSource
    from app.services.watchlist_ingest import ingest_entries

    provider = await make_watchlist_provider(db_session, "simkl")
    item = await make_movie(db_session, provider_id=provider.id, tmdb_id=603)
    await make_source(db_session, item, provider, existing, "old-id")
    await db_session.commit()

    await ingest_entries(db_session, provider, [_entry("new-id", incoming)])
    src = (await db_session.execute(select(MediaItemWatchlistSource))).scalar_one()
    assert src.list_status == expected


# --- On-hold tier classification (m69-04) ---

_PAST = datetime(2020, 1, 1, tzinfo=timezone.utc)


async def _awaiting_series(session, *, s2: dict | None = None, watched: int = 1):
    from app.db.models import SeasonStatus

    provider = await make_watchlist_provider(session, "simkl")
    s2_cfg = {
        "season_number": 2,
        "watch_order": 2,
        "air_date": None,
        "status": SeasonStatus.FUTURE,
    }
    s2_cfg.update(s2 or {})
    item, _, _ = await make_series(
        session,
        seasons=[
            {"season_number": 1, "watch_order": 1, "air_date": _PAST},
            s2_cfg,
        ],
        provider_id=provider.id,
    )
    await make_source(session, item, provider, "watching", "s", watched)
    await session.commit()
    return await _load_full(session, item.id)


async def _load_full(session, item_id) -> MediaItem:
    return (
        await session.execute(
            select(MediaItem)
            .where(MediaItem.id == item_id)
            .options(
                selectinload(MediaItem.watchlist_sources),
                selectinload(MediaItem.seasons).selectinload(Season.episodes),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_fully_watched_with_future_season_is_on_hold(db_session):
    item = await _awaiting_series(db_session)
    assert latest_released_order(item) == 1
    assert has_future_seasons(item)
    assert is_awaiting_seasons(item)
    assert classify_v3_status_tier(item) == "on_hold"


async def test_released_second_season_is_not_on_hold(db_session):
    from app.db.models import SeasonStatus

    item = await _awaiting_series(
        db_session, s2={"air_date": _PAST, "status": SeasonStatus.SEARCHING}
    )
    assert not has_future_seasons(item)
    assert classify_v3_status_tier(item) != "on_hold"


async def test_unwatched_released_season_is_not_awaiting(db_session):
    item = await _awaiting_series(db_session, watched=0)
    assert not is_awaiting_seasons(item)
    assert classify_v3_status_tier(item) != "on_hold"


async def test_active_download_beats_on_hold(db_session):
    from app.db.models import SeasonStatus

    item = await _awaiting_series(db_session)
    item.seasons[0].status = SeasonStatus.DOWNLOADING
    assert classify_v3_status_tier(item) == "in_progress"


async def test_list_on_hold_source_classifies_on_hold(db_session):
    provider = await make_watchlist_provider(db_session, "simkl")
    item = await make_movie(db_session, provider_id=provider.id)
    await make_source(db_session, item, provider, "on_hold", "h")
    await db_session.commit()
    item = await _load_full(db_session, item.id)
    assert not is_awaiting_seasons(item)
    assert classify_v3_status_tier(item) == "on_hold"


async def test_watching_beats_on_hold_source(db_session):
    item = await _awaiting_series(db_session, watched=0)
    other = await make_watchlist_provider(db_session, "simkl", name="other")
    await make_source(db_session, item, other, "on_hold", "o")
    await db_session.commit()
    item = await _load_full(db_session, item.id)
    assert item.effective_list_status == "watching"
    assert classify_v3_status_tier(item) != "on_hold"
