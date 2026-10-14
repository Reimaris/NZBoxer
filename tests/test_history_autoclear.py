import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import reload_settings_from_db, settings
from app.db.models import SystemSettings


@pytest.mark.asyncio
async def test_defaults_on_fresh_row(db_session: AsyncSession):
    row = SystemSettings(id=1)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.history_autoclear_pruned is False
    assert row.history_retention_days == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [0, 90])
async def test_retention_days_valid_persists_and_reloads(
    async_client: AsyncClient, db_session: AsyncSession, valid: int
):
    resp = await async_client.post(
        "/settings/auto-save", data={"history_retention_days": str(valid)}
    )
    assert resp.status_code < 400
    await reload_settings_from_db(db_session)
    assert settings.history_retention_days == valid


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["45", "abc"])
async def test_retention_days_invalid_falls_back_to_30(
    async_client: AsyncClient, db_session: AsyncSession, invalid: str
):
    await async_client.post(
        "/settings/auto-save", data={"history_retention_days": "90"}
    )
    await async_client.post(
        "/settings/auto-save", data={"history_retention_days": invalid}
    )
    db_session.expire_all()
    row = await db_session.get(SystemSettings, 1)
    assert row is not None
    assert row.history_retention_days == 30
    await reload_settings_from_db(db_session)
    assert settings.history_retention_days == 30


@pytest.mark.asyncio
@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False)])
async def test_autoclear_pruned_toggle(
    async_client: AsyncClient, db_session: AsyncSession, raw: str, expected: bool
):
    await async_client.post(
        "/settings/auto-save", data={"history_autoclear_pruned": "true"}
    )
    await async_client.post(
        "/settings/auto-save", data={"history_autoclear_pruned": raw}
    )
    db_session.expire_all()
    row = await db_session.get(SystemSettings, 1)
    assert row is not None
    assert row.history_autoclear_pruned is expected
    await reload_settings_from_db(db_session)
    assert settings.history_autoclear_pruned is expected


# --- Autoclear engine (ticket 02) ---

from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.core.phase_stats import WatchlistPeriodicSyncStatus  # noqa: E402
from app.core.transfer_poller import autoclear_history_ledger  # noqa: E402
from app.db.models import (  # noqa: E402
    DownloadHistory,
    EpisodeStatus,
    MediaStatus,
)
from tests.conftest import make_history, make_movie, make_series  # noqa: E402

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=45)
RECENT = NOW - timedelta(days=3)


async def _ids(session: AsyncSession) -> set[int]:
    return set((await session.execute(select(DownloadHistory.id))).scalars())


async def _hist(session, n: int, sent, *, detached=False, **kw):
    item = None if detached else await make_movie(session, simkl_id=20000 + n)
    return await make_history(
        session,
        item=item,
        nzb_guid=f"g{n}",
        downloader_transfer_id=f"tb{n}",
        dispatched_at=sent,
        **kw,
    )


@pytest.mark.asyncio
async def test_autoclear_both_off_is_noop(db_session: AsyncSession):
    h = await _hist(db_session, 1, OLD, detached=True)
    res = await autoclear_history_ledger(
        db_session, autoclear_pruned=False, retention_days=0, now=NOW
    )
    assert res == {"pruned_count": 0, "aged_count": 0, "total": 0}
    assert await _ids(db_session) == {h.id}


@pytest.mark.asyncio
async def test_autoclear_rule_a_only(db_session: AsyncSession):
    det = await _hist(db_session, 1, RECENT, detached=True)
    att = await _hist(db_session, 2, OLD)
    res = await autoclear_history_ledger(
        db_session, autoclear_pruned=True, retention_days=0, now=NOW
    )
    assert res == {"pruned_count": 1, "aged_count": 0, "total": 1}
    assert await _ids(db_session) == {att.id}
    assert det.id not in await _ids(db_session)


@pytest.mark.asyncio
async def test_autoclear_rule_b_only(db_session: AsyncSession):
    old = await _hist(db_session, 1, OLD)
    recent = await _hist(db_session, 2, RECENT)
    det_recent = await _hist(db_session, 3, RECENT, detached=True)
    res = await autoclear_history_ledger(
        db_session, autoclear_pruned=False, retention_days=30, now=NOW
    )
    assert res == {"pruned_count": 0, "aged_count": 1, "total": 1}
    assert await _ids(db_session) == {recent.id, det_recent.id}
    assert old.id not in await _ids(db_session)


@pytest.mark.asyncio
async def test_autoclear_zero_retention_keeps_ancient_row(db_session: AsyncSession):
    h = await _hist(db_session, 1, NOW - timedelta(days=400))
    await autoclear_history_ledger(
        db_session, autoclear_pruned=False, retention_days=0, now=NOW
    )
    assert await _ids(db_session) == {h.id}


@pytest.mark.asyncio
async def test_autoclear_or_counts_double_match_once(db_session: AsyncSession):
    both = await _hist(db_session, 1, OLD, detached=True)
    aged = await _hist(db_session, 2, OLD)
    res = await autoclear_history_ledger(
        db_session, autoclear_pruned=True, retention_days=30, now=NOW
    )
    assert res == {"pruned_count": 1, "aged_count": 1, "total": 2}
    assert await _ids(db_session) == set()
    assert both.id and aged.id


@pytest.mark.asyncio
async def test_autoclear_protects_inflight_and_upgrade_linked(
    db_session: AsyncSession,
):
    inflight = await _hist(
        db_session,
        1,
        NOW - timedelta(days=400),
        detached=True,
        status_detail="downloading",
        is_dismissed=False,
    )
    old = await _hist(db_session, 2, OLD, detached=True)
    new = await _hist(
        db_session,
        3,
        RECENT,
        detached=True,
        status_detail="downloading",
        is_dismissed=False,
        replaces_history_id=old.id,
    )
    res = await autoclear_history_ledger(
        db_session, autoclear_pruned=True, retention_days=30, now=NOW
    )
    assert res["total"] == 0
    assert await _ids(db_session) == {inflight.id, old.id, new.id}


@pytest.mark.asyncio
async def test_autoclear_falls_back_to_created_at(db_session: AsyncSession):
    h = await _hist(db_session, 1, None)
    h.created_at = OLD
    await db_session.flush()
    res = await autoclear_history_ledger(
        db_session, autoclear_pruned=False, retention_days=30, now=NOW
    )
    assert res["aged_count"] == 1
    assert h.id not in await _ids(db_session)


@pytest.mark.asyncio
async def test_autoclear_reconciles_parent_and_skips_torbox(
    db_session: AsyncSession,
):
    item, _, eps = await make_series(
        db_session,
        episodes_per_season=1,
        episode_statuses=[EpisodeStatus.COMPLETED],
        status=MediaStatus.COMPLETED,
    )
    await make_history(
        db_session,
        item=item,
        episode=eps[0],
        dispatched_at=OLD,
        status_detail="completed",
    )
    await db_session.commit()
    with patch(
        "app.services.downloaders.torbox.delete_transfer",
        new_callable=AsyncMock,
        create=True,
    ) as tb_del:
        res = await autoclear_history_ledger(
            db_session, autoclear_pruned=False, retention_days=30, now=NOW
        )
    assert res["aged_count"] == 1
    tb_del.assert_not_called()
    db_session.expire_all()
    await db_session.refresh(eps[0])
    assert eps[0].status != EpisodeStatus.COMPLETED


# --- Triggers (ticket 03) ---

TICK_NOW = datetime(2026, 10, 13, 12, 7, tzinfo=timezone.utc)


@pytest.fixture
def tick_patches():
    sync = AsyncMock()
    with (
        patch(
            "app.core.automation.rollover_due_future_episodes_and_seasons",
            new=AsyncMock(),
        ),
        patch(
            "app.core.air_dates.enrich_active_shows_episode_air_dates",
            new=AsyncMock(return_value=0),
        ),
        patch("app.core.automation.sync_all_providers", new=sync),
        patch(
            "app.core.automation.evaluate_periodic_watchlist_sync",
            return_value=WatchlistPeriodicSyncStatus("due", 60, None, 0),
        ),
    ):
        yield sync


@pytest.fixture
def retention(request):
    prev = settings.history_retention_days
    settings.history_retention_days = getattr(request, "param", 30)
    yield
    settings.history_retention_days = prev


@pytest.mark.asyncio
@pytest.mark.usefixtures("retention")
async def test_tick_autoclears_old_terminal_row(db_session: AsyncSession, tick_patches):
    from app.core.automation import run_orchestrator_tick

    await _hist(db_session, 1, TICK_NOW - timedelta(days=45))
    await db_session.commit()
    await run_orchestrator_tick(now=TICK_NOW)
    assert await _ids(db_session) == set()


@pytest.mark.asyncio
async def test_tick_autoclear_failure_does_not_block_sync(tick_patches):
    from app.core.automation import run_orchestrator_tick

    with patch(
        "app.core.transfer_poller.autoclear_history_ledger",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await run_orchestrator_tick(now=TICK_NOW)
    tick_patches.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retention", "total"), [(30, 0), (0, 1)], indirect=["retention"]
)
async def test_api_history_autoclears_on_load(
    async_client: AsyncClient, db_session: AsyncSession, retention, total: int
):
    await _hist(db_session, 1, datetime.now(timezone.utc) - timedelta(days=45))
    await db_session.commit()
    resp = await async_client.get("/api/history")
    assert resp.json()["total"] == total


@pytest.mark.asyncio
async def test_settings_page_renders_history_section(async_client: AsyncClient):
    html = (await async_client.get("/settings")).text
    for needle in (
        'name="history_retention_days"',
        'name="history_autoclear_pruned"',
        "30 days (Default)",
        "History Retention &amp; Autoclearing",
    ):
        assert needle in html


@pytest.mark.asyncio
async def test_settings_page_selects_saved_retention(async_client: AsyncClient):
    await async_client.post(
        "/settings/auto-save", data={"history_retention_days": "60"}
    )
    html = (await async_client.get("/settings")).text
    assert 'value="60" selected' in html
