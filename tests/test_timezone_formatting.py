"""Tests for central timezone conversion helpers and Jinja2 filter."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import pytest

from app.core.time_utils import format_local_datetime, to_local_datetime
from app.main import templates
from tests.conftest import seed_ready_setup


def test_to_local_datetime_none() -> None:
    assert to_local_datetime(None) is None


def test_to_local_datetime_naive_utc() -> None:
    naive_dt = datetime(2026, 10, 10, 12, 0, 0)
    local_dt = to_local_datetime(naive_dt)
    assert local_dt is not None
    assert local_dt.tzinfo is not None

    # Compare UTC timestamps
    expected_timestamp = datetime(
        2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc
    ).timestamp()
    assert local_dt.timestamp() == pytest.approx(expected_timestamp)


def test_to_local_datetime_aware_utc() -> None:
    aware_dt = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
    local_dt = to_local_datetime(aware_dt)
    assert local_dt is not None
    assert local_dt.tzinfo is not None
    assert local_dt.timestamp() == pytest.approx(aware_dt.timestamp())


def test_format_local_datetime_none() -> None:
    assert format_local_datetime(None) == "—"


def test_format_local_datetime_formats_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()

    dt = datetime(2026, 10, 10, 12, 34, 56)
    assert format_local_datetime(dt) == "2026-10-10 12:34"
    assert format_local_datetime(dt, fmt="%Y-%m-%d %H:%M:%S") == "2026-10-10 12:34:56"


def test_jinja_filter_registration() -> None:
    filter_fn = templates.env.filters.get("local_datetime")
    assert filter_fn is not None
    assert filter_fn is format_local_datetime
    assert templates.env.from_string("{{ dt | local_datetime }}").render(dt=None) == "—"


@pytest.mark.asyncio
async def test_run_orchestrator_tick_logs_local_datetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging
    from unittest.mock import AsyncMock, patch

    from app.core.automation import run_orchestrator_tick

    # Set timezone to UTC+2 (Etc/GMT-2)
    monkeypatch.setenv("TZ", "Etc/GMT-2")
    time.tzset()

    # UTC datetime at 10:15:00 -> in GMT-2 (UTC+2) it is 12:15:00
    now_utc = datetime(2026, 10, 10, 10, 15, 0, tzinfo=timezone.utc)

    records: list[logging.LogRecord] = []

    class TestHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = TestHandler()
    logger = logging.getLogger("app.core.automation")
    logger.addHandler(handler)
    try:
        with (
            patch(
                "app.core.automation.rollover_due_future_episodes_and_seasons",
                new_callable=AsyncMock,
            ),
            patch(
                "app.core.automation.sync_all_providers",
                new_callable=AsyncMock,
            ),
        ):
            await run_orchestrator_tick(now=now_utc)
    finally:
        logger.removeHandler(handler)

    log_messages = [r.getMessage() for r in records]
    assert any(
        "[ORCHESTRATOR] Tick start at 2026-10-10 12:15:00" in msg
        for msg in log_messages
    )


@pytest.mark.asyncio
async def test_push_history_ledger_pushed_at_str_local_timezone(
    db_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.transfer_poller import get_push_history_ledger
    from tests.conftest import make_history, make_movie

    # Fix timezone to UTC+2
    monkeypatch.setenv("TZ", "Etc/GMT-2")
    time.tzset()

    item = await make_movie(
        db_session,
        title="Timezone Movie",
        year=2026,
    )

    # UTC naive datetime at 10:00:00 -> in GMT-2 (UTC+2) it is 12:00:00
    pushed_utc = datetime(2026, 10, 10, 10, 0, 0)
    await make_history(
        db_session,
        item=item,
        nzb_title="Timezone.Movie.2026.1080p",
        dispatched_at=pushed_utc,
    )

    ledger = await get_push_history_ledger(db_session)
    assert len(ledger) == 1
    entry = ledger[0]
    assert entry["pushed_at_str"] == "2026-10-10 12:00"
    assert entry["pushed_at_ts"] == pytest.approx(
        datetime(2026, 10, 10, 10, 0, 0, tzinfo=timezone.utc).timestamp()
    )


@pytest.mark.asyncio
async def test_blacklist_slice_renders_local_datetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Set timezone to UTC+3 (Etc/GMT-3)
    monkeypatch.setenv("TZ", "Etc/GMT-3")
    time.tzset()

    # UTC naive datetime at 09:15 -> in UTC+3 it is 12:15
    created_at_utc = datetime(2026, 10, 10, 9, 15, 0)
    mock_release = type(
        "MockBlacklistRelease",
        (),
        {
            "id": 42,
            "created_at": created_at_utc,
            "media_item_id": 1,
            "nzb_title": "Blacklisted.Release.2026.1080p",
            "reason": "Corrupted NZB",
        },
    )()

    rendered = templates.env.get_template("partials/blacklist_slice.html").render(
        blacklisted_releases=[mock_release]
    )

    assert "2026-10-10 12:15" in rendered


@pytest.mark.asyncio
async def test_dashboard_renders_local_datetime_in_item_row(
    async_client: Any,
    db_session: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db.models import FailureLog
    from tests.conftest import make_movie

    # Set timezone to UTC+4 (Etc/GMT-4)
    monkeypatch.setenv("TZ", "Etc/GMT-4")
    time.tzset()

    # UTC naive datetimes
    updated_at_utc = datetime(2026, 10, 10, 8, 30, 45)  # UTC+4 -> 12:30:45
    failure_created_at_utc = datetime(2026, 10, 10, 9, 0, 0)  # UTC+4 -> 13:00

    movie = await make_movie(
        db_session,
        title="Local Time Movie",
        year=2026,
    )
    movie.updated_at = updated_at_utc
    db_session.add(movie)
    await db_session.flush()

    fail_log = FailureLog(
        media_item_id=movie.id,
        category="indexer",
        message="Connection timed out",
        created_at=failure_created_at_utc,
    )
    db_session.add(fail_log)
    await db_session.commit()

    await seed_ready_setup(db_session)
    await db_session.commit()
    resp = await async_client.get("/")
    assert resp.status_code == 200
    rendered = resp.text

    # Acceptance criteria:
    # 1. data-last-checked formatted with '%Y-%m-%d %H:%M:%S'
    assert 'data-last-checked="2026-10-10 12:30:45"' in rendered
    # 2. failure log created_at formatted with | local_datetime
    assert "2026-10-10 13:00: Connection timed out" in rendered
