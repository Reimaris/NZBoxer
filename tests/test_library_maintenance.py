"""Library maintenance pass (m61): characterization of the Simkl pipeline's outcomes."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select

from app.core.automation import sync_all_providers
from app.db.models import (
    MediaItem,
    MediaStatus,
    MediaType,
    Provider,
    SystemSettings,
)
from app.services.library_maintenance import run_library_maintenance
from tests.conftest import make_movie, make_series, make_source, make_watchlist_provider

SIMKL_CFG = (
    '{"access_token": "tok", "client_id": "cid", "sync_movies": true,'
    ' "sync_series": false, "sync_anime": true}'
)


@pytest.mark.asyncio
async def test_simkl_sync_characterization_consolidates_merges_and_prunes(
    session_factory, monkeypatch
):
    """Sequel consolidated, duplicate merged, completed-only pruned, active transfer deferred."""
    async with session_factory() as session:
        session.add(SystemSettings(id=1))
        provider = await make_watchlist_provider(
            session,
            "simkl",
            name="Simkl",
            config_json=SIMKL_CFG,
            client_id="cid",
            access_token="tok",
        )

        # Anime sequel pair: parent with S1+S2, plus a standalone item for S2.
        parent, seasons, _ = await make_series(
            session,
            seasons=2,
            media_type=MediaType.ANIME,
            title="Foo Saga",
            simkl_id=6001,
            anilist_id=5001,
            year=2020,
        )
        seasons[1].anilist_id = 5002
        seasons[1].title = "Foo Saga 2nd Season"
        sequel = await make_movie(
            session,
            media_type=MediaType.ANIME,
            title="Foo Saga 2nd Season",
            simkl_id=6002,
            anilist_id=5002,
            year=2022,
            provider_id=provider.id,
        )
        # Duplicate movie across two items.
        dup1 = await make_movie(
            session,
            title="District 9",
            simkl_id=74006,
            tmdb_id=17654,
            provider_id=provider.id,
        )
        dup2 = await make_movie(
            session,
            title="District 9",
            simkl_id=74006,
            tmdb_id=17654,
            provider_id=provider.id,
        )
        # Completed-only (absent from feed) and active-transfer (absent, downloading).
        done = await make_movie(
            session,
            title="Old Done",
            simkl_id=7001,
            status=MediaStatus.COMPLETED,
            provider_id=provider.id,
        )
        active = await make_movie(
            session,
            title="Mid Download",
            simkl_id=7002,
            status=MediaStatus.DOWNLOADING,
            provider_id=provider.id,
        )
        for it in (parent, sequel, dup1, done, active):
            await make_source(
                session,
                it,
                provider,
                list_status="watching",
                remote_id=str(it.simkl_id),
            )
        await session.commit()
        ids = {
            n: i.id
            for n, i in dict(
                parent=parent,
                sequel=sequel,
                dup1=dup1,
                dup2=dup2,
                done=done,
                active=active,
            ).items()
        }

    async def get_watchlist(media_type, client_id=None, access_token=None):
        if media_type == "movies":
            return [
                {
                    "movie": {
                        "title": "District 9",
                        "year": 2009,
                        "ids": {"simkl": 74006, "tmdb": "17654"},
                    }
                }
            ]
        if media_type == "anime":
            return [
                {"show": {"title": "Foo Saga", "year": 2020, "ids": {"simkl": 6001}}},
                {
                    "show": {
                        "title": "Foo Saga 2nd Season",
                        "year": 2022,
                        "ids": {"simkl": 6002},
                    }
                },
            ]
        return []

    async def movie_details(tmdb_id):
        return {"title": "District 9", "release_date": "2009-08-14"}

    async def no_digital(tmdb_id):
        return None

    monkeypatch.setattr("app.services.watchlists.simkl.get_watchlist", get_watchlist)
    monkeypatch.setattr("app.services.tmdb.get_movie_details", movie_details)
    monkeypatch.setattr("app.services.tmdb.get_digital_release_date", no_digital)

    await sync_all_providers()

    async with session_factory() as session:
        remaining = {
            i.id: i for i in (await session.execute(select(MediaItem))).scalars().all()
        }
        # sequels consolidated: standalone entry soft-ignored, parent untouched
        assert remaining[ids["sequel"]].status == MediaStatus.IGNORED
        assert remaining[ids["parent"]].status != MediaStatus.IGNORED
        # duplicates merged: exactly one District 9 row survives
        assert sum(1 for i in remaining.values() if i.simkl_id == 74006) == 1
        # completed-only title pruned
        assert ids["done"] not in remaining
        # active transfer deferred (kept)
        assert ids["active"] in remaining


# --- run_library_maintenance -------------------------------------------------


@pytest.fixture
async def sequel_pair(db_session):
    """Anime parent (S1+S2) plus standalone sequel item, sourced via the given provider type."""

    async def _make(provider_type: str):
        provider = await make_watchlist_provider(db_session, provider_type)
        parent, seasons, _ = await make_series(
            db_session,
            seasons=2,
            provider_id=provider.id,
            media_type=MediaType.ANIME,
            title="Foo Saga",
            simkl_id=0,
            anilist_id=5001,
            year=2020,
        )
        seasons[1].anilist_id = 5002
        seasons[1].title = "Foo Saga 2nd Season"
        sequel = await make_movie(
            db_session,
            media_type=MediaType.ANIME,
            title="Foo Saga 2nd Season",
            simkl_id=0,
            anilist_id=5002,
            year=2022,
            provider_id=provider.id,
        )
        await make_source(db_session, parent, provider, remote_id="a")
        await make_source(db_session, sequel, provider, remote_id="b")
        ids = (provider.id, sequel.id)
        await db_session.commit()
        return ids

    return _make


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", ["plex", "mal"])
async def test_non_simkl_library_is_franchised_deduplicated_and_pruned(
    db_session, sequel_pair, provider_type
):
    provider_id, sequel_id = await sequel_pair(provider_type)
    provider = await db_session.get(Provider, provider_id)
    dup1 = await make_movie(
        db_session, title="D9", simkl_id=0, tmdb_id=17654, provider_id=provider_id
    )
    await make_movie(
        db_session, title="D9", simkl_id=0, tmdb_id=17654, provider_id=provider_id
    )
    done = await make_movie(
        db_session, title="Done", simkl_id=0, tmdb_id=1, provider_id=provider_id
    )
    await make_source(db_session, dup1, provider, remote_id="d")
    await make_source(
        db_session, done, provider, list_status="completed", remote_id="e"
    )
    await db_session.commit()
    done_id = done.id

    stats = await run_library_maintenance(db_session)

    assert stats.sequels_consolidated == 1
    assert stats.duplicates_merged == 1
    assert stats.pruned == 1
    db_session.expire_all()
    assert (await db_session.get(MediaItem, sequel_id)).status == MediaStatus.IGNORED
    assert await db_session.get(MediaItem, done_id) is None


@pytest.mark.asyncio
async def test_include_anime_false_skips_anime_steps(db_session, sequel_pair):
    _, sequel_id = await sequel_pair("plex")
    stats = await run_library_maintenance(db_session, include_anime=False)
    assert stats.sequels_consolidated == 0
    assert stats.anime_seasons_updated == 0
    db_session.expire_all()
    assert (await db_session.get(MediaItem, sequel_id)).status != MediaStatus.IGNORED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("statuses", "kept"),
    [
        (("completed", "dropped"), False),
        (("completed", "watching"), True),
        ((), True),  # zero-source ad-hoc item is never pruned
    ],
)
async def test_prune_rules_by_source_status(db_session, statuses, kept):
    plex = await make_watchlist_provider(db_session, "plex")
    mal = await make_watchlist_provider(db_session, "mal")
    item = await make_movie(db_session, simkl_id=0, provider_id=plex.id)
    for prov, status in zip((plex, mal), statuses):
        await make_source(db_session, item, prov, list_status=status, remote_id="x")
    await db_session.commit()
    item_id = item.id

    await run_library_maintenance(db_session)

    db_session.expire_all()
    assert (await db_session.get(MediaItem, item_id) is not None) is kept


@pytest.mark.asyncio
async def test_maintenance_logs_four_phases(db_session, caplog):
    caplog.set_level(logging.INFO, logger="app.core.automation")
    await run_library_maintenance(db_session)
    text = caplog.text
    for marker in (
        "[SYNC] [MAINT] [1/4] 0 sequels consolidated, 0 TBA seasons updated",
        "[SYNC] [MAINT] [2/4] 0 TBA seasons discovered",
        "[SYNC] [MAINT] [3/4] 0 duplicates merged",
        "[SYNC] [MAINT] [4/4] 0 inactive titles pruned",
    ):
        assert marker in text
