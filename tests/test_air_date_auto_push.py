"""Pure-logic tests for air-date due and upgrade-window evaluation."""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.air_push_engine import is_episode_air_due, is_episode_in_upgrade_window
from app.db.models import Episode, EpisodeStatus

AIR = datetime(2026, 1, 10, 20, 0, tzinfo=timezone.utc)


def _ep(air_date=AIR, status=EpisodeStatus.SEARCHING) -> Episode:
    return Episode(air_date=air_date, status=status)


@pytest.mark.parametrize(
    ("delay", "offset", "expected"),
    [
        (0, timedelta(0), True),
        (0, timedelta(seconds=-1), False),
        (2, timedelta(hours=2), True),
        (2, timedelta(hours=2, seconds=-1), False),
        (12, timedelta(hours=12, seconds=1), True),
        (12, timedelta(hours=11, minutes=59, seconds=59), False),
    ],
)
def test_is_episode_air_due_boundaries(delay, offset, expected):
    assert is_episode_air_due(_ep(), delay, AIR + offset) is expected


def test_air_due_false_without_air_date():
    assert is_episode_air_due(_ep(air_date=None), 0, AIR) is False


@pytest.mark.parametrize(
    "status", [s for s in EpisodeStatus if s != EpisodeStatus.SEARCHING]
)
def test_air_due_false_when_not_searching(status):
    assert is_episode_air_due(_ep(status=status), 0, AIR + timedelta(days=1)) is False


def test_air_due_naive_air_date_treated_as_utc():
    ep = _ep(air_date=AIR.replace(tzinfo=None))
    assert is_episode_air_due(ep, 2, AIR + timedelta(hours=2)) is True
    assert is_episode_air_due(ep, 2, AIR + timedelta(hours=1)) is False


FIRST_PUSH = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("first_pushed_at", "offset", "expected"),
    [
        (None, timedelta(0), False),
        (FIRST_PUSH, timedelta(seconds=-1), False),
        (FIRST_PUSH, timedelta(0), True),
        (FIRST_PUSH, timedelta(hours=48), True),
        (FIRST_PUSH, timedelta(hours=48, seconds=1), False),
        (FIRST_PUSH.replace(tzinfo=None), timedelta(hours=1), True),  # naive == UTC
    ],
)
def test_is_episode_in_upgrade_window_boundaries(first_pushed_at, offset, expected):
    ep = Episode(first_pushed_at=first_pushed_at, air_date=AIR)
    assert is_episode_in_upgrade_window(ep, 48, FIRST_PUSH + offset) is expected


def test_upgrade_window_ignores_air_date():
    ep = Episode(first_pushed_at=FIRST_PUSH, air_date=None)
    assert is_episode_in_upgrade_window(ep, 48, FIRST_PUSH) is True


@pytest.mark.parametrize(
    ("delay", "max_age", "offset", "expected"),
    [
        (2, 48, timedelta(hours=50), True),  # exactly at limit
        (2, 48, timedelta(hours=50, seconds=1), False),
        (2, None, timedelta(days=365), True),  # unbounded
        (0, 48, timedelta(hours=48), True),
        (0, 48, timedelta(hours=48, seconds=1), False),
        (12, 48, timedelta(hours=60), True),
        (12, 48, timedelta(hours=60, seconds=1), False),
        (12, 48, timedelta(hours=11), False),  # not yet due
    ],
)
def test_is_episode_air_due_backlog_guard(delay, max_age, offset, expected):
    assert (
        is_episode_air_due(_ep(), delay, AIR + offset, max_age_hours=max_age)
        is expected
    )


# --- run_initial_air_push integration ---------------------------------------

from unittest.mock import AsyncMock, patch  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.air_push_engine import run_initial_air_push  # noqa: E402
from tests.conftest import make_series  # noqa: E402

NOW = AIR + timedelta(hours=3)


@pytest.fixture
def air_settings(monkeypatch):
    monkeypatch.setattr(settings, "air_delay_hours", 2)
    monkeypatch.setattr(settings, "upgrade_window_hours", 48)
    monkeypatch.setattr(settings, "sh_max_retries", 3)


async def _series(db_session, simkl_id, *, opt_in=True, air=AIR, fail_count=0):
    item, _, eps = await make_series(
        db_session,
        episodes_per_season=1,
        simkl_id=simkl_id,
        title=f"Show {simkl_id}",
        auto_push_episodes=opt_in,
    )
    eps[0].air_date = air
    eps[0].fail_count = fail_count
    await db_session.commit()
    return item, eps[0]


def _push_mock(**kw):
    return patch(
        "app.core.air_push_engine.execute_auto_push",
        new=AsyncMock(return_value={"dispatched_count": 1}, **kw),
    )


async def test_opted_in_due_episode_pushed(db_session, air_settings):
    item, ep = await _series(db_session, 1)
    with _push_mock() as m:
        assert await run_initial_air_push(db_session, NOW) == 1
    m.assert_awaited_once_with(db_session, item.id, {"episode_ids": [ep.id]})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"opt_in": False},
        {"air": AIR + timedelta(hours=2)},  # not yet due after delay
        {"fail_count": 4},  # over sh_max_retries
        {"air": AIR - timedelta(days=10)},  # backlog older than delay + window
    ],
)
async def test_ineligible_episode_ignored(db_session, air_settings, kwargs):
    await _series(db_session, 1, **kwargs)
    with _push_mock() as m:
        assert await run_initial_air_push(db_session, NOW) == 0
    m.assert_not_awaited()


async def test_one_item_raising_does_not_stop_next(db_session, air_settings):
    first, _ = await _series(db_session, 1)
    second, _ = await _series(db_session, 2)
    first_id, second_id = first.id, second.id
    calls = []

    async def fake(session, item_id, payload):
        calls.append(item_id)
        if item_id == first_id:
            raise RuntimeError("boom")
        return {"dispatched_count": 1}

    with patch("app.core.air_push_engine.execute_auto_push", new=fake):
        assert await run_initial_air_push(db_session, NOW) == 1
    assert sorted(calls) == sorted([first_id, second_id])


async def test_locked_item_skipped(db_session, air_settings):
    from app.core.search_lock import item_lock_manager

    item, _ = await _series(db_session, 1)
    assert item_lock_manager.try_acquire(item.id, owner="manual")
    try:
        with _push_mock() as m:
            assert await run_initial_air_push(db_session, NOW) == 0
        m.assert_not_awaited()
    finally:
        item_lock_manager.release(item.id)


# --- Poller tick & scheduler wiring ---------------------------------------


async def test_tick_runs_both_steps_in_order():
    from app.core import air_push_engine as eng

    order = []

    async def push(session, now, **kw):
        order.append("push")
        return 0

    async def upgrades(session, now, **kw):
        order.append("upgrade")
        return 0

    with (
        patch.object(eng, "run_initial_air_push", new=push),
        patch.object(eng, "run_episode_upgrades", new=upgrades),
    ):
        await eng.run_air_date_push_and_upgrade_tick(NOW)
    assert order == ["push", "upgrade"]


async def test_tick_step_one_failure_still_runs_step_two():
    from app.core import air_push_engine as eng

    upgrades = AsyncMock(return_value=0)
    with (
        patch.object(
            eng, "run_initial_air_push", new=AsyncMock(side_effect=RuntimeError("x"))
        ),
        patch.object(eng, "run_episode_upgrades", new=upgrades),
    ):
        await eng.run_air_date_push_and_upgrade_tick(NOW)
    upgrades.assert_awaited_once()


async def test_no_independent_air_push_job_registered():
    from app.main import app, lifespan, scheduler

    async with lifespan(app):
        assert scheduler.get_job("air_date_push_and_upgrade_job") is None


from app.core.phase_stats import WatchlistPeriodicSyncStatus  # noqa: E402


def _tick_mocks(push):
    order: list[str] = []

    async def sync():
        order.append("sync")

    async def _push(now=None):
        order.append("push")
        await push(now)

    return order, (
        patch("app.core.automation.sync_all_providers", new=sync),
        patch(
            "app.core.automation.evaluate_periodic_watchlist_sync",
            return_value=WatchlistPeriodicSyncStatus("due", 60, None, 0),
        ),
        patch("app.core.air_push_engine.run_air_date_push_and_upgrade_tick", new=_push),
        patch(
            "app.core.automation.rollover_due_future_episodes_and_seasons",
            new=AsyncMock(),
        ),
        patch(
            "app.core.air_dates.enrich_active_shows_episode_air_dates",
            new=AsyncMock(return_value=0),
        ),
    )


async def test_hourly_tick_runs_push_after_sync():
    from app.core.automation import run_orchestrator_tick

    order, ps = _tick_mocks(AsyncMock())
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        await run_orchestrator_tick(
            now=datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)
        )
    assert order == ["sync", "push"]


@pytest.mark.parametrize("minute", [15, 30, 45])
async def test_non_hourly_tick_skips_push(minute):
    from app.core.automation import run_orchestrator_tick

    order, ps = _tick_mocks(AsyncMock())
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        await run_orchestrator_tick(
            now=datetime(2026, 10, 12, 14, minute, tzinfo=timezone.utc)
        )
    assert order == ["sync"]


async def test_push_tick_exception_does_not_propagate():
    from app.core.automation import run_orchestrator_tick

    order, ps = _tick_mocks(AsyncMock(side_effect=RuntimeError("boom")))
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        await run_orchestrator_tick(
            now=datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)
        )
    assert order == ["sync", "push"]
