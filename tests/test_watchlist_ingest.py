"""Cross-provider ingestion tests (m56-04)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MediaItem, MediaItemWatchlistSource, MediaType
from app.services import anilist, tmdb
from app.services.watchlist_ingest import (
    CATEGORY_TOGGLE_BY_KIND,
    filter_enabled_entries,
    ingest_entries,
    mark_absent_sources_completed,
    seen_ids_with_disabled_categories,
)
from app.services.watchlist_providers import WatchlistEntry
from tests.conftest import make_movie, make_series, make_source, make_watchlist_provider


@pytest.fixture(autouse=True)
def _no_http(monkeypatch):
    monkeypatch.setattr(tmdb, "get_movie_details", AsyncMock(return_value=None))
    monkeypatch.setattr(tmdb, "get_show_details", AsyncMock(return_value=None))
    monkeypatch.setattr(tmdb, "get_digital_release_date", AsyncMock(return_value=None))
    monkeypatch.setattr(anilist, "get_anime_ids_by_mal_ids", AsyncMock(return_value={}))
    hier = AsyncMock(return_value=None)
    monkeypatch.setattr(anilist, "get_anime_root_and_hierarchy", hier)
    return hier


async def _count(session: AsyncSession, model) -> int:
    return (await session.execute(select(func.count(model.id)))).scalar_one()


def _movie(**kw) -> WatchlistEntry:
    return WatchlistEntry(
        **{
            "remote_id": "m1",
            "media_kind": "movie",
            "title": "The Matrix",
            "tmdb_id": 603,
            **kw,
        }
    )


def _anime(**kw) -> WatchlistEntry:
    return WatchlistEntry(
        **{"remote_id": "16498", "media_kind": "anime", "title": "AoT", **kw}
    )


async def test_plex_and_simkl_same_tmdb_id_one_item_two_sources(db_session):
    simkl = await make_watchlist_provider(db_session, "simkl")
    plex = await make_watchlist_provider(db_session, "plex")
    item = await make_movie(db_session, provider_id=simkl.id, tmdb_id=603, simkl_id=5)
    await make_source(db_session, item, simkl, remote_id="5")
    await db_session.commit()

    result = await ingest_entries(db_session, plex, [_movie()])

    assert (result.ingested, result.added, result.linked) == (1, 0, 1)
    assert await _count(db_session, MediaItem) == 1
    sources = (
        (await db_session.execute(select(MediaItemWatchlistSource))).scalars().all()
    )
    assert {s.provider_id for s in sources} == {simkl.id, plex.id}


async def test_anilist_and_mal_same_anime_via_idmal_one_item(db_session, monkeypatch):
    al = await make_watchlist_provider(db_session, "anilist")
    mal = await make_watchlist_provider(db_session, "mal")
    await db_session.commit()
    monkeypatch.setattr(
        anilist, "get_anime_ids_by_mal_ids", AsyncMock(return_value={5114: 121})
    )

    await ingest_entries(
        db_session, al, [_anime(remote_id="121", anilist_id=121, mal_id=5114)]
    )
    result = await ingest_entries(
        db_session, mal, [_anime(remote_id="5114", anilist_id=None, mal_id=5114)]
    )

    assert (result.added, result.linked) == (0, 1)
    assert await _count(db_session, MediaItem) == 1
    assert await _count(db_session, MediaItemWatchlistSource) == 2


async def test_new_non_simkl_item_has_zero_simkl_id_and_origin_provider(db_session):
    plex = await make_watchlist_provider(db_session, "plex")
    await db_session.commit()

    result = await ingest_entries(db_session, plex, [_movie(imdb_id="tt0133093")])

    item = (await db_session.execute(select(MediaItem))).scalars().one()
    assert result.added == 1
    assert (item.simkl_id, item.provider_id, item.media_type) == (
        0,
        plex.id,
        MediaType.MOVIE,
    )
    assert item.imdb_id == "tt0133093"


async def test_hierarchy_called_once_per_new_anime(db_session, _no_http):
    al = await make_watchlist_provider(db_session, "anilist")
    await db_session.commit()
    entries = [_anime(), _anime(remote_id="2", anilist_id=2, title="B")]
    entries[0] = _anime(anilist_id=16498)

    await ingest_entries(db_session, al, entries)
    await ingest_entries(db_session, al, entries)  # re-ingest: no new enrichment

    assert sorted(c.args[0] for c in _no_http.await_args_list) == [2, 16498]


async def test_idless_entry_skipped(db_session):
    plex = await make_watchlist_provider(db_session, "plex")
    await db_session.commit()

    result = await ingest_entries(
        db_session, plex, [_movie(tmdb_id=None), _anime(anilist_id=None, mal_id=None)]
    )

    assert (result.ingested, result.added, result.linked) == (2, 0, 0)
    assert await _count(db_session, MediaItem) == 0


async def test_fill_missing_ids_never_overwrites(db_session):
    simkl = await make_watchlist_provider(db_session, "simkl")
    plex = await make_watchlist_provider(db_session, "plex")
    item = await make_movie(
        db_session, provider_id=simkl.id, tmdb_id=603, imdb_id="tt1", tvdb_id=None
    )
    await db_session.commit()

    await ingest_entries(db_session, plex, [_movie(imdb_id="tt2", tvdb_id=169)])

    await db_session.refresh(item)
    assert (item.imdb_id, item.tvdb_id, item.tmdb_id) == ("tt1", 169, 603)


async def test_progress_max_reconciliation_and_status_transition(
    db_session, monkeypatch
):
    al = await make_watchlist_provider(db_session, "anilist")
    simkl = await make_watchlist_provider(db_session, "simkl")
    item, seasons, _ = await make_series(
        db_session,
        seasons=[
            {"season_number": 1, "watch_order": 1, "anilist_id": 10},
            {"season_number": 2, "watch_order": 2, "anilist_id": 20},
        ],
        provider_id=simkl.id,
        media_type=MediaType.ANIME,
        anilist_id=10,
        last_watched_order=1,
    )
    await db_session.commit()

    r1 = await ingest_entries(
        db_session,
        al,
        [
            _anime(
                remote_id="20",
                anilist_id=20,
                list_status="watching",
                watched_episodes=2,
            )
        ],
    )
    await db_session.refresh(item)
    assert item.last_watched_order == 2  # raised from 1 via matched season order
    assert r1.linked == 1

    r2 = await ingest_entries(
        db_session,
        al,
        [
            _anime(
                remote_id="10",
                anilist_id=10,
                list_status="completed",
                watched_episodes=1,
            )
        ],
    )
    await db_session.refresh(item)
    assert item.last_watched_order == 2  # max retained
    src = (await db_session.execute(select(MediaItemWatchlistSource))).scalars().one()
    assert (
        src.list_status == "watching"
    )  # one franchise stays active while any entry active
    assert r2.transitioned == 0


async def test_anilist_then_simkl_progress_fires_auto_advance_once(
    db_session, monkeypatch
):
    from app.core import push_engine

    al = await make_watchlist_provider(db_session, "anilist")
    simkl = await make_watchlist_provider(db_session, "simkl")
    item, _, _ = await make_series(
        db_session,
        seasons=[
            {"season_number": 1, "watch_order": 1, "anilist_id": 10},
            {"season_number": 2, "watch_order": 2, "anilist_id": 20},
        ],
        provider_id=simkl.id,
        media_type=MediaType.ANIME,
        anilist_id=10,
        auto_advance_seasons=True,
    )
    await db_session.commit()
    advance = AsyncMock(return_value=[])
    monkeypatch.setattr(push_engine, "advance_season_buffer", advance)

    await ingest_entries(
        db_session,
        al,
        [
            _anime(
                remote_id="20",
                anilist_id=20,
                list_status="watching",
                watched_episodes=1,
            )
        ],
    )
    await ingest_entries(
        db_session,
        simkl,
        [
            _anime(
                remote_id="10",
                anilist_id=10,
                list_status="watching",
                watched_episodes=1,
            )
        ],
    )

    await db_session.refresh(item)
    assert item.last_watched_order == 2
    assert advance.await_count == 1
    orders = {
        s.provider_id: s.last_watched_order
        for s in (await db_session.execute(select(MediaItemWatchlistSource))).scalars()
    }
    assert orders == {al.id: 2, simkl.id: 1}


async def test_absent_sources_marked_completed_only_when_active(db_session):
    plex = await make_watchlist_provider(db_session, "plex")
    a = await make_movie(db_session, provider_id=plex.id, tmdb_id=1, simkl_id=1)
    b = await make_movie(db_session, provider_id=plex.id, tmdb_id=2, simkl_id=2)
    c = await make_movie(db_session, provider_id=plex.id, tmdb_id=3, simkl_id=3)
    await make_source(db_session, a, plex, list_status="watching", remote_id="a")
    await make_source(db_session, b, plex, list_status="plantowatch", remote_id="b")
    dropped = await make_source(
        db_session, c, plex, list_status="dropped", remote_id="c"
    )
    await db_session.commit()

    n = await mark_absent_sources_completed(db_session, plex, {"a"})

    statuses = {
        s.remote_id: s.list_status
        for s in (await db_session.execute(select(MediaItemWatchlistSource))).scalars()
    }
    assert n == 1
    assert statuses == {"a": "watching", "b": "completed", "c": "dropped"}
    assert dropped.list_status == "dropped"


async def test_failed_fetch_causes_no_transitions(db_session):
    from app.services.watchlist_providers import WatchlistFetchError

    plex = await make_watchlist_provider(db_session, "plex")
    item = await make_movie(db_session, provider_id=plex.id, tmdb_id=1)
    src = await make_source(
        db_session, item, plex, list_status="watching", remote_id="a"
    )
    await db_session.commit()

    class Boom:
        async def fetch_entries(self, provider):
            raise WatchlistFetchError("down")

    try:
        entries = await Boom().fetch_entries(plex)
        await ingest_entries(db_session, plex, entries)
        await mark_absent_sources_completed(db_session, plex, set())
    except WatchlistFetchError:
        pass

    await db_session.refresh(src)
    assert src.list_status == "watching"


async def test_simkl_sync_upserts_source(db_session, watchlist_provider, monkeypatch):
    from tests.conftest import ingest_simkl_payloads

    monkeypatch.setattr(tmdb, "get_movie_details", AsyncMock(return_value=None))
    payload = [
        {
            "status": "watching",
            "movie": {"title": "X", "year": 2020, "ids": {"simkl": 777, "tmdb": "55"}},
        }
    ]
    await ingest_simkl_payloads(
        db_session, watchlist_provider, payload, MediaType.MOVIE
    )

    src = (await db_session.execute(select(MediaItemWatchlistSource))).scalars().one()
    assert (src.remote_id, src.list_status, src.provider_id) == (
        "777",
        "watching",
        watchlist_provider.id,
    )


TOGGLE_CASES = [
    ("sync_movies", "movie", MediaType.MOVIE),
    ("sync_series", "show", MediaType.SHOW),
    ("sync_anime", "anime", MediaType.ANIME),
    ("sync_anime", "anime_movie", MediaType.ANIME),
]


@pytest.mark.parametrize(("toggle", "kind", "media_type"), TOGGLE_CASES)
async def test_disabled_category_filtered_and_not_completed(
    db_session, toggle, kind, media_type
):
    import json

    plex = await make_watchlist_provider(
        db_session, "plex", config_json=json.dumps({toggle: False})
    )
    entries = [
        WatchlistEntry(remote_id=k, media_kind=k, title="T")
        for k in ("movie", "show", "anime", "anime_movie")
    ]
    kept = filter_enabled_entries(plex, entries)
    assert {e.remote_id for e in kept} == {
        e.remote_id for e in entries if CATEGORY_TOGGLE_BY_KIND[e.media_kind] != toggle
    }

    disabled_item, _, _ = (
        (await make_movie(db_session, provider_id=plex.id, simkl_id=1), None, None)
        if media_type == MediaType.MOVIE
        else await make_series(
            db_session, provider_id=plex.id, simkl_id=1, media_type=media_type
        )
    )
    enabled_item = await make_movie(db_session, provider_id=plex.id, simkl_id=2)
    if media_type == MediaType.MOVIE:
        enabled_item = (
            await make_series(
                db_session, provider_id=plex.id, simkl_id=2, media_type=MediaType.SHOW
            )
        )[0]
    await make_source(db_session, disabled_item, plex, remote_id="disabled")
    await make_source(db_session, enabled_item, plex, remote_id="enabled")
    await db_session.commit()

    seen = await seen_ids_with_disabled_categories(db_session, plex, set())
    assert seen == {"disabled"}
    await mark_absent_sources_completed(db_session, plex, seen)

    statuses = {
        s.remote_id: s.list_status
        for s in (await db_session.execute(select(MediaItemWatchlistSource))).scalars()
    }
    assert statuses == {"disabled": "plantowatch", "enabled": "completed"}


async def test_all_categories_enabled_by_default(db_session):
    plex = await make_watchlist_provider(db_session, "plex")
    entries = [
        WatchlistEntry(remote_id=k, media_kind=k, title="T")
        for k in ("movie", "show", "anime", "anime_movie")
    ]
    assert filter_enabled_entries(plex, entries) == entries
    assert await seen_ids_with_disabled_categories(db_session, plex, {"a"}) == {"a"}


@pytest.mark.parametrize(("first", "second"), [(2, 3), (3, 2), (0, 3), (3, 0)])
async def test_furthest_progress_wins_in_both_ingest_orders(db_session, first, second):
    from app.core.push_engine import effective_watch_order

    simkl = await make_watchlist_provider(db_session, "simkl")
    p1 = await make_watchlist_provider(db_session, "anilist")
    p2 = await make_watchlist_provider(db_session, "mal")
    item, _, _ = await make_series(
        db_session,
        seasons=[
            {"season_number": n, "watch_order": n, "anilist_id": n * 10}
            for n in (1, 2, 3)
        ],
        provider_id=simkl.id,
        media_type=MediaType.ANIME,
        anilist_id=10,
    )
    await db_session.commit()

    def entry(order: int) -> WatchlistEntry:
        return _anime(
            remote_id=str(order * 10 or 10),
            anilist_id=order * 10 or 10,
            list_status="watching" if order else "plantowatch",
            watched_episodes=1 if order else 0,
        )

    await ingest_entries(db_session, p1, [entry(first)])
    await ingest_entries(db_session, p2, [entry(second)])
    # A later low-progress sync must not roll anything back.
    await ingest_entries(db_session, p1, [entry(0)])

    await db_session.refresh(item, ["watchlist_sources"])
    assert item.last_watched_order == max(first, second)
    assert effective_watch_order(item) == max(first, second)


async def test_effective_watch_order_takes_furthest_source(db_session):
    from app.core.push_engine import effective_watch_order

    plex = await make_watchlist_provider(db_session, "plex")
    mal = await make_watchlist_provider(db_session, "mal")
    item = await make_movie(db_session, provider_id=plex.id, simkl_id=9)
    item.last_watched_order = 2
    await make_source(db_session, item, plex, remote_id="a", last_watched_order=1)
    await make_source(db_session, item, mal, remote_id="b", last_watched_order=4)
    await db_session.commit()
    await db_session.refresh(item, ["watchlist_sources"])

    assert effective_watch_order(item) == 4


async def test_completed_source_keeps_item_active_while_other_source_active(db_session):
    plex = await make_watchlist_provider(db_session, "plex")
    mal = await make_watchlist_provider(db_session, "mal")
    item = await make_movie(db_session, provider_id=plex.id, simkl_id=9)
    await make_source(db_session, item, plex, list_status="completed", remote_id="a")
    await make_source(db_session, item, mal, list_status="plantowatch", remote_id="b")
    await db_session.commit()
    await db_session.refresh(item, ["watchlist_sources"])

    assert item.is_watchlist_active


async def test_new_anime_consolidated_into_existing_root_does_not_crash(
    db_session, _no_http
):
    """A new sequel entry whose AniList root already exists is merged; ingest keeps using the root."""
    provider = await make_watchlist_provider(db_session, "anilist")
    root = MediaItem(
        provider_id=provider.id,
        simkl_id=0,
        anilist_id=100,
        title="Root",
        media_type=MediaType.ANIME,
    )
    db_session.add(root)
    await db_session.commit()
    _no_http.return_value = {
        "root": {"id": 100, "title": {"english": "Root"}, "startDate": None},
        "hierarchy": [],
    }

    result = await ingest_entries(
        db_session, provider, [_anime(remote_id="200", anilist_id=200)]
    )

    assert result.ingested == 1
    assert await _count(db_session, MediaItem) == 1
    sources = (
        (await db_session.execute(select(MediaItemWatchlistSource))).scalars().all()
    )
    assert [s.media_item_id for s in sources] == [root.id]


async def test_consolidated_anime_entry_is_not_requeried_on_next_sync(
    db_session, _no_http
):
    """The merged entry's AniList id sticks to the root, so a repeat sync makes no hierarchy call."""
    provider = await make_watchlist_provider(db_session, "anilist")
    db_session.add(
        MediaItem(
            provider_id=provider.id,
            simkl_id=0,
            anilist_id=100,
            title="Root",
            media_type=MediaType.ANIME,
        )
    )
    await db_session.commit()
    _no_http.return_value = {
        "root": {"id": 100, "title": {"english": "Root"}, "startDate": None},
        "hierarchy": [],
    }
    entry = _anime(remote_id="200", anilist_id=200)

    await ingest_entries(db_session, provider, [entry])
    calls_after_first = _no_http.await_count
    result = await ingest_entries(db_session, provider, [entry])

    assert _no_http.await_count == calls_after_first
    assert result.added == 0
    assert await _count(db_session, MediaItem) == 1


async def test_consolidated_entry_keeps_root_source_when_remote_id_taken(
    db_session, _no_http
):
    """Root's source must not adopt a remote id already held by another item's source."""
    provider = await make_watchlist_provider(db_session, "simkl")
    root = MediaItem(
        provider_id=provider.id,
        simkl_id=0,
        anilist_id=100,
        title="Root",
        media_type=MediaType.ANIME,
    )
    other = MediaItem(
        provider_id=provider.id,
        simkl_id=0,
        anilist_id=300,
        title="Other",
        media_type=MediaType.ANIME,
    )
    db_session.add_all([root, other])
    await db_session.commit()
    await make_source(db_session, root, provider, remote_id="100")
    await make_source(db_session, other, provider, remote_id="200")
    await db_session.commit()
    _no_http.return_value = {
        "root": {"id": 100, "title": {"english": "Root"}, "startDate": None},
        "hierarchy": [],
    }

    await ingest_entries(
        db_session, provider, [_anime(remote_id="200", anilist_id=200)]
    )

    remote_ids = (
        (await db_session.execute(select(MediaItemWatchlistSource.remote_id)))
        .scalars()
        .all()
    )
    assert sorted(remote_ids) == ["100", "200"]
