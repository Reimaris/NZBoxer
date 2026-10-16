"""Background pacing during outages: air-date pushes and auto-advance pause (ADR-160)."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.core import air_push_engine as eng
from app.core.outage import clear_outages, record_outage
from app.core.push_engine import evaluate_watch_progress_and_advance
from tests.test_watch_states import _hold_series

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def outage():
    record_outage("indexer", "nzbgeek.example", "NZBgeek", "HTTP 503", 1)
    yield
    clear_outages()


async def test_air_tick_skips_during_outage_and_runs_after(outage):
    push = AsyncMock(return_value=0)
    upgrades = AsyncMock(return_value=0)
    with (
        patch.object(eng, "run_initial_air_push", new=push),
        patch.object(eng, "run_episode_upgrades", new=upgrades),
    ):
        await eng.run_air_date_push_and_upgrade_tick(NOW)
        push.assert_not_awaited()
        upgrades.assert_not_awaited()

        clear_outages()
        await eng.run_air_date_push_and_upgrade_tick(NOW)
    push.assert_awaited_once()
    upgrades.assert_awaited_once()


async def test_auto_advance_skips_during_outage_and_runs_after(db_session, outage):
    item = await _hold_series(db_session, s2_released=True, status="watching")
    with patch(
        "app.core.push_engine.advance_season_buffer", new=AsyncMock(return_value=[])
    ) as adv:
        await evaluate_watch_progress_and_advance(db_session, item, {"status": "x"})
        adv.assert_not_awaited()
        assert not item.last_watched_order  # progress kept for the retry

        clear_outages()
        await evaluate_watch_progress_and_advance(db_session, item, {"status": "x"})
    adv.assert_awaited_once()
    assert item.last_watched_order == 1
