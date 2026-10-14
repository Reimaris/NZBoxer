"""Sequential multi-provider sync orchestration tests (m56-07)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core import automation
from app.db.models import MediaItem, Provider
from app.services import tmdb, watchlist_providers
from app.services.watchlist_providers import WatchlistEntry, WatchlistFetchError
from tests.conftest import make_watchlist_provider


class FakeAdapter:
    def __init__(self, calls: list[str], entries=None, error: Exception | None = None):
        self.calls, self.entries, self.error = calls, entries or [], error

    async def fetch_entries(self, provider: Provider):
        self.calls.append(provider.type)
        if self.error:
            raise self.error
        return self.entries


@pytest.fixture(autouse=True)
def _no_http(monkeypatch):
    monkeypatch.setattr(tmdb, "get_movie_details", AsyncMock(return_value=None))
    monkeypatch.setattr(tmdb, "get_digital_release_date", AsyncMock(return_value=None))


def _install(monkeypatch, adapters: dict[str, FakeAdapter]):
    monkeypatch.setattr(watchlist_providers, "get_adapter", lambda t: adapters[t])


def _movie(rid="m1", tmdb_id=603) -> WatchlistEntry:
    return WatchlistEntry(
        remote_id=rid, media_kind="movie", title="The Matrix", tmdb_id=tmdb_id
    )


async def _provider(db_session, ptype, priority, **kw) -> Provider:
    p = await make_watchlist_provider(
        db_session, ptype, priority=priority, access_token="tok123456", **kw
    )
    if ptype == "anilist":
        p.username = "someone"
    await db_session.commit()
    return p


async def _cfg(db_session, provider_id) -> dict:
    db_session.expire_all()
    p = await db_session.get(Provider, provider_id)
    assert p is not None
    return json.loads(p.config_json)


async def test_providers_synced_sequentially_by_priority_then_id(
    db_session, monkeypatch
):
    calls: list[str] = []
    await _provider(db_session, "plex", 2)
    await _provider(db_session, "mal", 1)
    await _provider(db_session, "anilist", 1)
    _install(monkeypatch, {t: FakeAdapter(calls) for t in ("plex", "mal", "anilist")})

    assert await automation.sync_all_providers() == 0
    assert calls == ["mal", "anilist", "plex"]


async def test_failure_is_isolated_recorded_and_cleared(db_session, monkeypatch):
    calls: list[str] = []
    bad = await _provider(db_session, "mal", 1)
    good = await _provider(db_session, "plex", 2)
    bad_id, good_id = bad.id, good.id
    adapters = {
        "mal": FakeAdapter(
            calls, error=WatchlistFetchError("boom access_token=abcdef123456")
        ),
        "plex": FakeAdapter(calls, entries=[_movie()]),
    }
    _install(monkeypatch, adapters)

    assert await automation.sync_all_providers() == 1
    assert calls == ["mal", "plex"]
    bad_cfg = await _cfg(db_session, bad_id)
    assert (
        "boom" in bad_cfg["last_error"] and "abcdef123456" not in bad_cfg["last_error"]
    )
    assert bad_cfg.get("last_synced_at")  # failures stamp to back off
    good_cfg = await _cfg(db_session, good_id)
    assert good_cfg["last_synced_at"] and "last_error" not in good_cfg

    # A later success clears the error.
    adapters["mal"].error = None
    assert await automation.sync_all_providers() == 0
    assert "last_error" not in await _cfg(db_session, bad_id)


async def test_sync_single_provider_ok_error_and_busy(db_session, monkeypatch):
    calls: list[str] = []
    plex = await _provider(db_session, "plex", 1)
    adapter = FakeAdapter(calls, entries=[_movie()])
    _install(monkeypatch, {"plex": adapter})

    ok = await automation.sync_single_provider(plex.id)
    assert ok == {"status": "ok", "ingested": 1, "added": 1, "pruned": 0, "error": None}
    assert (await db_session.execute(select(MediaItem))).scalars().first()

    adapter.error = WatchlistFetchError("down")
    err = await automation.sync_single_provider(plex.id)
    assert err["status"] == "error" and err["error"] == "down"

    async with automation._sync_lock:
        busy = await automation.sync_single_provider(plex.id)
    assert busy["status"] == "busy"


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"sync_interval_minutes": 60}, True),  # last_synced_at None => due
        ({"sync_interval_minutes": 0}, False),  # disabled
        (
            {
                "sync_interval_minutes": 60,
                "last_synced_at": (NOW - timedelta(minutes=59)).isoformat(),
            },
            False,
        ),
        (
            {
                "sync_interval_minutes": 60,
                "last_synced_at": (NOW - timedelta(minutes=61)).isoformat(),
            },
            True,
        ),
        # 1 is clamped to the 5 minute floor
        (
            {
                "sync_interval_minutes": 1,
                "last_synced_at": (NOW - timedelta(minutes=3)).isoformat(),
            },
            False,
        ),
        (
            {
                "sync_interval_minutes": 1,
                "last_synced_at": (NOW - timedelta(minutes=6)).isoformat(),
            },
            True,
        ),
        # 99999 is clamped to the 1440 minute ceiling
        (
            {
                "sync_interval_minutes": 99999,
                "last_synced_at": (NOW - timedelta(minutes=1441)).isoformat(),
            },
            True,
        ),
    ],
)
async def test_provider_periodic_due(db_session, config, expected):
    p = await _provider(db_session, "plex", 1, config_json=json.dumps(config))
    assert automation.is_provider_periodic_sync_due(p, NOW) is expected


async def test_periodic_due_requires_connected_and_active(db_session):
    p = await make_watchlist_provider(db_session, "plex")  # no token => not connected
    assert automation.is_provider_periodic_sync_due(p, NOW) is False
    p.access_token = "tok123456"
    assert automation.is_provider_periodic_sync_due(p, NOW) is True
    p.is_active = False
    assert automation.is_provider_periodic_sync_due(p, NOW) is False


async def test_run_periodic_watchlist_sync_if_due_covers_non_simkl(
    db_session, monkeypatch
):
    sync = AsyncMock(return_value=0)
    monkeypatch.setattr(automation, "sync_all_providers", sync)
    assert await automation.run_periodic_watchlist_sync_if_due(NOW) is False
    await _provider(db_session, "plex", 1)
    assert await automation.run_periodic_watchlist_sync_if_due(NOW) is True
    sync.assert_awaited_once()


@pytest.mark.parametrize(
    ("errors", "message"),
    [
        (0, "Watchlist sync complete."),
        (2, "Watchlist sync complete (2 provider error(s))."),
    ],
)
async def test_manual_sync_message(async_client, monkeypatch, errors, message):
    monkeypatch.setattr(
        automation, "sync_all_providers", AsyncMock(return_value=errors)
    )
    resp = await async_client.post("/sync")
    assert message in resp.text


@pytest.fixture
def maintenance_spy(monkeypatch):
    """Replace the maintenance pass with a spy returning pruned=3; yields recorded kwargs."""
    from app.services.library_maintenance import MaintenanceStats

    calls: list[dict] = []

    async def fake(session, *, include_anime=True):
        calls.append({"include_anime": include_anime})
        return MaintenanceStats(pruned=3)

    monkeypatch.setattr(automation, "run_library_maintenance", fake)
    return calls


async def test_single_provider_sync_runs_maintenance_once(
    db_session, monkeypatch, maintenance_spy
):
    plex = await _provider(db_session, "plex", 1)
    _install(monkeypatch, {"plex": FakeAdapter([], entries=[_movie()])})

    result = await automation.sync_single_provider(plex.id)

    assert maintenance_spy == [{"include_anime": True}]
    assert result["pruned"] == 3


async def test_two_providers_run_maintenance_once_with_pruned_in_each_result(
    db_session, monkeypatch, maintenance_spy
):
    await _provider(db_session, "plex", 1)
    await _provider(db_session, "mal", 2)
    _install(monkeypatch, {"plex": FakeAdapter([]), "mal": FakeAdapter([])})

    async with automation._sync_lock:
        results = await automation._run_provider_sync()

    assert len(maintenance_spy) == 1
    assert [r["pruned"] for r in results] == [3, 3]


async def test_all_failed_cycle_skips_maintenance(
    db_session, monkeypatch, maintenance_spy
):
    await _provider(db_session, "plex", 1)
    _install(monkeypatch, {"plex": FakeAdapter([], error=WatchlistFetchError("down"))})

    assert await automation.sync_all_providers() == 1
    assert maintenance_spy == []


async def test_include_anime_follows_synced_providers_config(
    db_session, monkeypatch, maintenance_spy
):
    await _provider(db_session, "plex", 1, config_json='{"sync_anime": false}')
    _install(monkeypatch, {"plex": FakeAdapter([])})

    await automation.sync_all_providers()

    assert maintenance_spy == [{"include_anime": False}]


@pytest.mark.parametrize("ptype", ["plex", "mal", "anilist", "simkl"])
async def test_failed_sync_stamps_last_synced_and_backs_off(
    db_session, monkeypatch, ptype
):
    extra = {"client_id": "cid"} if ptype == "simkl" else {}
    provider = await _provider(db_session, ptype, 1, **extra)
    pid = provider.id
    _install(monkeypatch, {ptype: FakeAdapter([], error=WatchlistFetchError("boom"))})

    results = await automation._run_provider_sync()

    assert results[0]["status"] == "error"
    cfg = await _cfg(db_session, pid)
    assert cfg["last_error"] == "boom"
    assert cfg["last_synced_at"]
    provider = await db_session.get(Provider, pid)
    now = datetime.now(timezone.utc)
    assert not automation.is_provider_periodic_sync_due(provider, now)
    assert automation.is_provider_periodic_sync_due(provider, now + timedelta(days=2))


@pytest.mark.parametrize(("age_minutes", "expected"), [(20, True), (5, False)])
async def test_dashboard_sync_trigger_covers_connected_plex(
    db_session, age_minutes, expected
):
    last = (NOW - timedelta(minutes=age_minutes)).isoformat()
    await _provider(
        db_session, "plex", 1, config_json=json.dumps({"last_synced_at": last})
    )
    assert (
        await automation.should_trigger_dashboard_watchlist_sync(db_session, now=NOW)
        is expected
    )


async def test_evaluate_periodic_sync_due_for_mal_past_interval(db_session):
    mal = await _provider(
        db_session,
        "mal",
        1,
        config_json=json.dumps(
            {
                "sync_interval_minutes": 30,
                "last_synced_at": (NOW - timedelta(minutes=45)).isoformat(),
            }
        ),
    )
    status = automation.evaluate_periodic_watchlist_sync([mal], NOW)
    assert status.state == "due"


async def test_inactive_or_unauthenticated_providers_are_not_sync_eligible(
    db_session,
):
    unauth = await make_watchlist_provider(db_session, "mal")
    inactive = await _provider(db_session, "plex", 1)
    inactive.is_active = False
    assert not unauth.sync_eligible
    assert not inactive.sync_eligible
    assert (
        automation.evaluate_periodic_watchlist_sync([unauth, inactive], NOW).state
        == "no_provider"
    )
    assert (
        await automation.should_trigger_dashboard_watchlist_sync(db_session, now=NOW)
        is False
    )


async def test_mal_sync_anime_off_keeps_existing_anime_active(db_session, monkeypatch):
    from app.db.models import MediaItemWatchlistSource, MediaType
    from tests.conftest import make_series, make_source

    mal = await _provider(
        db_session, "mal", 1, config_json=json.dumps({"sync_anime": False})
    )
    anime, _, _ = await make_series(
        db_session, provider_id=mal.id, simkl_id=1, media_type=MediaType.ANIME
    )
    await make_source(db_session, anime, mal, remote_id="5114")
    await db_session.commit()
    _install(monkeypatch, {"mal": FakeAdapter([], entries=[])})

    assert await automation.sync_all_providers() == 0

    db_session.expire_all()
    source = (await db_session.execute(select(MediaItemWatchlistSource))).scalar_one()
    assert source.list_status == "plantowatch"


async def test_plex_sync_series_off_skips_shows_but_ingests_movies(
    db_session, monkeypatch
):
    plex = await _provider(
        db_session, "plex", 1, config_json=json.dumps({"sync_series": False})
    )
    show = WatchlistEntry(
        remote_id="s1", media_kind="show", title="Some Show", tmdb_id=777
    )
    _install(monkeypatch, {"plex": FakeAdapter([], entries=[_movie(), show])})

    assert await automation.sync_all_providers() == 0

    titles = {
        i.tmdb_id for i in (await db_session.execute(select(MediaItem))).scalars()
    }
    assert titles == {603}
    assert plex.id
