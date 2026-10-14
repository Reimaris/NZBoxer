"""Schema tests for the multi-provider watchlist source junction (m56-01)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ACTIVE_LIST_STATUSES,
    INACTIVE_LIST_STATUSES,
    MediaItem,
    MediaItemWatchlistSource,
    Provider,
)
from tests.conftest import (
    make_movie,
    make_source,
    make_watchlist_provider,
)


async def _provider(session: AsyncSession, ptype: str = "anilist", **kw) -> Provider:
    p = Provider(name=ptype, type=ptype, category="watchlist", **kw)
    session.add(p)
    await session.flush()
    return p


def _src(item: MediaItem, provider: Provider, remote_id: str = "1", **kw):
    return MediaItemWatchlistSource(
        media_item_id=item.id, provider_id=provider.id, remote_id=remote_id, **kw
    )


async def _count(session: AsyncSession) -> int:
    return (
        await session.execute(select(func.count(MediaItemWatchlistSource.id)))
    ).scalar_one()


def test_status_constants():
    assert ACTIVE_LIST_STATUSES == {"plantowatch", "watching"}
    assert INACTIVE_LIST_STATUSES == {"completed", "dropped"}


async def test_defaults(db_session: AsyncSession):
    prov = await _provider(db_session)
    item = await make_movie(db_session, provider_id=prov.id)
    src = _src(item, prov)
    db_session.add(src)
    await db_session.commit()
    await db_session.refresh(src)
    assert src.list_status == "plantowatch"
    assert src.last_watched_order == 0
    assert src.last_synced_at is None
    assert src.created_at is not None


async def test_unique_provider_remote(db_session: AsyncSession):
    prov = await _provider(db_session)
    a = await make_movie(db_session, provider_id=prov.id)
    b = await make_movie(db_session, provider_id=prov.id)
    db_session.add_all([_src(a, prov, "9"), _src(b, prov, "9")])
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_unique_item_provider(db_session: AsyncSession):
    prov = await _provider(db_session)
    item = await make_movie(db_session, provider_id=prov.id)
    db_session.add_all([_src(item, prov, "1"), _src(item, prov, "2")])
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_cascade_on_item_delete(db_session: AsyncSession):
    prov = await _provider(db_session)
    item = await make_movie(db_session, provider_id=prov.id)
    db_session.add(_src(item, prov))
    await db_session.commit()
    await db_session.refresh(item)
    await db_session.delete(item)
    await db_session.commit()
    assert await _count(db_session) == 0


async def test_cascade_on_provider_delete(db_session: AsyncSession):
    home = await _provider(db_session, "simkl")
    other = await _provider(db_session, "anilist")
    item = await make_movie(db_session, provider_id=home.id)
    db_session.add_all([_src(item, home, "1"), _src(item, other, "2")])
    await db_session.commit()
    await db_session.delete(other)
    await db_session.commit()
    remaining = (
        (await db_session.execute(select(MediaItemWatchlistSource))).scalars().all()
    )
    assert [s.provider_id for s in remaining] == [home.id]


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["watching"], True),
        (["plantowatch"], True),
        (["completed"], False),
        (["dropped"], False),
        (["completed", "watching"], True),
        ([], False),
    ],
)
async def test_is_watchlist_active(db_session: AsyncSession, statuses, expected):
    item = await make_movie(db_session)
    for i, status in enumerate(statuses):
        prov = await _provider(db_session, f"p{i}")
        db_session.add(_src(item, prov, str(i), list_status=status))
    await db_session.commit()
    await db_session.refresh(item)
    assert item.is_watchlist_active is expected


@pytest.mark.parametrize(
    ("ptype", "kwargs", "expected"),
    [
        ("simkl", {}, "not_connected"),
        ("simkl", {"access_token": "t"}, "reconnect_required"),
        (
            "simkl",
            {"access_token": "t", "config_json": json.dumps({"refresh_token": "r"})},
            "connected",
        ),
        ("anilist", {}, "not_connected"),
        ("anilist", {"username": "bob"}, "connected"),
        ("plex", {}, "not_connected"),
        ("plex", {"access_token": "tok"}, "connected"),
        ("mal", {}, "not_connected"),
        ("mal", {"access_token": "t"}, "connected"),
        (
            "mal",
            {
                "access_token": "t",
                "config_json": json.dumps(
                    {"token_expires_at": "2000-01-01T00:00:00+00:00"}
                ),
            },
            "reconnect_required",
        ),
        (
            "mal",
            {
                "access_token": "t",
                "config_json": json.dumps(
                    {
                        "token_expires_at": "2000-01-01T00:00:00+00:00",
                        "refresh_token": "r",
                    }
                ),
            },
            "connected",
        ),
    ],
)
def test_connection_status(ptype, kwargs, expected):
    p = Provider(name=ptype, type=ptype, category="watchlist", **kwargs)
    assert p.watchlist_connection_status == expected


def test_watchlist_config_defaults_and_aliases():
    p = Provider(name="a", type="anilist", config_json=json.dumps({"x": 1}))
    cfg = p.watchlist_config
    assert cfg["sync_interval_minutes"] == 60
    assert cfg["last_synced_at"] is None
    assert cfg["x"] == 1
    assert p.watchlist_config == cfg


async def test_backfill_idempotent(db_session: AsyncSession):
    from app.db.database import backfill_watchlist_sources

    prov = await _provider(db_session, "simkl")
    watched = await make_movie(
        db_session, provider_id=prov.id, simkl_id=77, last_watched_order=2
    )
    fresh = await make_movie(db_session, provider_id=prov.id, simkl_id=0)
    await db_session.commit()

    assert await backfill_watchlist_sources(db_session) == 2
    assert await backfill_watchlist_sources(db_session) == 0

    rows = {
        r.media_item_id: r
        for r in (await db_session.execute(select(MediaItemWatchlistSource))).scalars()
    }
    assert len(rows) == 2
    assert (rows[watched.id].remote_id, rows[watched.id].list_status) == (
        "77",
        "watching",
    )
    assert rows[watched.id].last_watched_order == 2
    assert (rows[fresh.id].remote_id, rows[fresh.id].list_status) == (
        str(fresh.id),
        "plantowatch",
    )


async def test_provider_delete_rehomes_shared_and_deletes_sole(
    async_client, db_session
):
    home = await make_watchlist_provider(db_session, "simkl")
    other = await make_watchlist_provider(db_session, "anilist")
    shared = await make_movie(db_session, provider_id=home.id, simkl_id=1)
    sole = await make_movie(db_session, provider_id=home.id, simkl_id=2)
    await make_source(db_session, shared, home, remote_id="1")
    await make_source(db_session, shared, other, remote_id="a1")
    await make_source(db_session, sole, home, remote_id="2")
    await db_session.commit()
    shared_id, sole_id, other_id, home_id = shared.id, sole.id, other.id, home.id
    db_session.expire_all()

    resp = await async_client.delete(f"/settings/provider/{home_id}")
    assert resp.status_code == 200

    db_session.expire_all()
    kept = await db_session.get(MediaItem, shared_id)
    assert kept is not None and kept.provider_id == other_id
    assert await db_session.get(MediaItem, sole_id) is None
    assert await db_session.get(Provider, home_id) is None
    remaining = (
        (await db_session.execute(select(MediaItemWatchlistSource))).scalars().all()
    )
    assert [(s.media_item_id, s.provider_id) for s in remaining] == [
        (shared_id, other_id)
    ]
