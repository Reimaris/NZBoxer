"""Tests for the persisted TorBox rate-limit push queue (ADR-094)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.core.push_engine import execute_auto_push
from app.core.rate_limit_queue import (
    RESUME_JOB_ID,
    RateLimitQueueState,
    count_rate_limited_pushes,
    is_rate_limit_error,
    resume_rate_limited_pushes,
)
from app.core.transfer_poller import dismiss_failed_push, get_active_pushes
from app.db.models import DownloadHistory, MediaStatus
from app.services.downloaders import torbox
from tests.conftest import VALID_NZB_BYTES, make_movie

MOVIE_RESULT = [
    {
        "title": "Test.Movie.2024.1080p.BluRay.x264-GRP",
        "guid": "guid-tm",
        "size_bytes": 8 * 1024**3,
    }
]


def _patches(send_nzb=None):
    return (
        patch(
            "app.services.indexers.newznab.search_movie",
            new=AsyncMock(return_value=[dict(r) for r in MOVIE_RESULT]),
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(VALID_NZB_BYTES, "t.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new=send_nzb or AsyncMock(return_value={"id": "tb1", "hash": "h1"}),
        ),
    )


async def _rows(session, item):
    await session.commit()
    return (
        (
            await session.execute(
                select(DownloadHistory)
                .where(DownloadHistory.media_item_id == item.id)
                .order_by(DownloadHistory.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.parametrize(
    ("msg", "expected"),
    [
        ("TorBox rate limit exceeded (cooldown: 5s)", True),
        ("failed: TorBox rate limit exceeded (cooldown: 5s)", True),
        ("failed: No matching releases found on indexers", False),
        (None, False),
    ],
)
def test_is_rate_limit_error(msg, expected):
    assert is_rate_limit_error(msg) is expected


async def test_auto_push_during_cooldown_queues_instead_of_failing(db_session):
    item = await make_movie(db_session)
    torbox.set_cooldown(600.0)

    with _patches()[0], _patches()[1], _patches()[2]:
        res = await execute_auto_push(db_session, item.id, {})

    assert res["pushed"] is False
    rows = await _rows(db_session, item)
    assert len(rows) == 1
    assert rows[0].status_detail == "rate_limited"
    assert rows[0].is_dismissed is False and rows[0].downloader_transfer_id is None
    await db_session.refresh(item)
    assert item.status == MediaStatus.RATE_LIMITED
    assert await count_rate_limited_pushes(db_session) == 1


async def test_mid_dispatch_429_queues_target(db_session):
    item = await make_movie(db_session)

    async def send_429(*_a, **_k):
        torbox.set_cooldown(300.0)
        raise torbox.DownloaderNetworkError(
            "TorBox rate limit exceeded (cooldown: 300s)"
        )

    p1, p2, p3 = _patches(AsyncMock(side_effect=send_429))
    with p1, p2, p3:
        await execute_auto_push(db_session, item.id, {})

    rows = await _rows(db_session, item)
    assert [r.status_detail for r in rows] == ["rate_limited"]
    await db_session.refresh(item)
    assert item.status == MediaStatus.RATE_LIMITED


async def test_repeat_enqueue_keeps_single_row(db_session):
    item = await make_movie(db_session)
    torbox.set_cooldown(600.0)
    with _patches()[0], _patches()[1], _patches()[2]:
        await execute_auto_push(db_session, item.id, {})
        await execute_auto_push(db_session, item.id, {})
    live = [r for r in await _rows(db_session, item) if not r.is_dismissed]
    assert len(live) == 1


async def test_resume_waits_for_cooldown_then_dispatches(db_session):
    item = await make_movie(db_session)
    torbox.set_cooldown(600.0)
    with _patches()[0], _patches()[1], _patches()[2]:
        await execute_auto_push(db_session, item.id, {})

    # Still cooling down: nothing happens
    assert await resume_rate_limited_pushes() == 0
    assert await count_rate_limited_pushes(db_session) == 1

    torbox.set_cooldown(0.0)
    send = AsyncMock(return_value={"id": "tb-resume", "hash": "h-resume"})
    p1, p2, p3 = _patches(send)
    with p1, p2, p3:
        assert await resume_rate_limited_pushes() == 1

    send.assert_awaited_once()
    live_ids = (
        (
            await db_session.execute(
                select(DownloadHistory.downloader_transfer_id).where(
                    DownloadHistory.media_item_id == item.id,
                    DownloadHistory.is_dismissed.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    assert await count_rate_limited_pushes(db_session) == 0
    assert live_ids == ["tb-resume"]
    await db_session.refresh(item)
    assert item.status == MediaStatus.DOWNLOADING


async def test_resume_is_fifo_and_requeues_on_new_429(db_session):
    m1 = await make_movie(db_session, simkl_id=1)
    m2 = await make_movie(db_session, simkl_id=2)
    torbox.set_cooldown(600.0)
    with _patches()[0], _patches()[1], _patches()[2]:
        await execute_auto_push(db_session, m1.id, {})
        await execute_auto_push(db_session, m2.id, {})
    torbox.set_cooldown(0.0)

    calls: list[str] = []

    async def send(*_a, **_k):
        calls.append("sent")
        if len(calls) == 1:
            torbox.set_cooldown(300.0)  # first resumed push hits 429 again
            raise torbox.DownloaderNetworkError(
                "TorBox rate limit exceeded (cooldown: 300s)"
            )
        return {"id": "x", "hash": "y"}

    p1, p2, p3 = _patches(AsyncMock(side_effect=send))
    with p1, p2, p3:
        await resume_rate_limited_pushes()

    # First item re-queued, second never attempted (cooldown active again)
    assert calls == ["sent"]
    assert await count_rate_limited_pushes(db_session) == 2


async def test_dismiss_cancels_queued_push(db_session):
    item = await make_movie(db_session)
    torbox.set_cooldown(600.0)
    with _patches()[0], _patches()[1], _patches()[2]:
        await execute_auto_push(db_session, item.id, {})
    row = (await _rows(db_session, item))[0]

    pushes = await get_active_pushes(db_session)
    assert len(pushes) == 1
    assert pushes[0]["is_rate_limited"] is True
    assert pushes[0]["status"].startswith("Paused — rate limit")

    assert await dismiss_failed_push(db_session, row.id) is True
    await db_session.refresh(item)
    assert item.status == MediaStatus.SEARCHING
    assert await count_rate_limited_pushes(db_session) == 0


def test_queue_state_wakes_and_sleeps_job():
    sched = MagicMock()
    job = MagicMock(next_run_time=None)
    sched.get_job.return_value = job
    state = RateLimitQueueState()
    state.set_scheduler(sched)

    state.wake()
    sched.resume_job.assert_called_once_with(RESUME_JOB_ID)

    job.next_run_time = object()
    state.sleep()
    sched.pause_job.assert_called_once_with(RESUME_JOB_ID)

    RateLimitQueueState().wake()  # no scheduler: harmless


async def test_manual_grab_queues_and_resumes_chosen_release(db_session):
    from app.core.push_engine import execute_manual_grab

    item = await make_movie(db_session, title="Manual Movie")
    torbox.set_cooldown(600.0)

    p1, p2, p3 = _patches()
    with p1, p2, p3:
        res = await execute_manual_grab(
            db_session,
            item.id,
            {
                "guid": "guid-manual-pick",
                "title": "Manual.Movie.2024.2160p.Remux.HEVC-GRP",
                "size_bytes": 40 * 1024**3,
                "score": 6200.0,
            },
        )

    assert res["pushed"] is True
    assert res["queued"] is True
    assert res["rate_limit_source"] == "TorBox"
    rows = await _rows(db_session, item)
    assert len(rows) == 1
    assert rows[0].status_detail == "rate_limited"
    assert rows[0].push_mode == "manual"
    assert rows[0].nzb_guid == "guid-manual-pick"
    assert rows[0].nzb_title == "Manual.Movie.2024.2160p.Remux.HEVC-GRP"

    # Resume dispatches the exact manually picked release without re-searching indexers
    torbox.set_cooldown(0.0)
    search_mock = AsyncMock(return_value=[])
    send_mock = AsyncMock(return_value={"id": "tb-man", "hash": "h-man"})
    with (
        patch("app.services.indexers.newznab.search_movie", new=search_mock),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(VALID_NZB_BYTES, "man.nzb")),
        ),
        patch("app.services.downloaders.torbox.send_nzb_file", new=send_mock),
    ):
        assert await resume_rate_limited_pushes() == 1

    search_mock.assert_not_awaited()
    send_mock.assert_awaited_once()
    live = [r for r in await _rows(db_session, item) if not r.is_dismissed]
    assert len(live) == 1
    assert live[0].downloader_transfer_id == "tb-man"
    assert live[0].push_mode == "manual"


async def test_multi_season_and_advance_season_buffer_queue_remaining_seasons(
    db_session,
):
    from app.core.push_engine import advance_season_buffer
    from app.db.models import SeasonStatus
    from tests.conftest import make_series

    item, seasons, _ = await make_series(
        db_session,
        title="Multi Season Show",
        seasons=3,
        episodes_per_season=1,
        allow_season_packs=True,
        prefer_season_packs=True,
        auto_advance_seasons=False,
    )
    s1, s2, s3 = seasons
    await db_session.commit()

    calls = 0

    async def send_first_then_429(*_a, **_k):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"id": "tb-s1", "hash": "h-s1"}
        torbox.set_cooldown(300.0)
        raise torbox.DownloaderNetworkError(
            "TorBox rate limit exceeded (cooldown: 300s)"
        )

    with (
        patch(
            "app.services.indexers.newznab.search_show",
            new=AsyncMock(
                return_value=[
                    {
                        "title": "Multi.Season.Show.S01.1080p.BluRay.x264-GRP",
                        "guid": "guid-s1",
                        "size_bytes": 10 * 1024**3,
                    },
                    {
                        "title": "Multi.Season.Show.S02.1080p.BluRay.x264-GRP",
                        "guid": "guid-s2",
                        "size_bytes": 10 * 1024**3,
                    },
                ]
            ),
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(VALID_NZB_BYTES, "s.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            side_effect=send_first_then_429,
        ),
    ):
        res = await execute_auto_push(
            db_session,
            item.id,
            {
                "season_ids": [s1.id, s2.id, s3.id],
                "allow_season_packs": True,
                "prefer_season_packs": True,
            },
        )

    assert res["pushed"] is True
    assert res["dispatched_count"] == 1
    await db_session.refresh(s1)
    await db_session.refresh(s2)
    await db_session.refresh(s3)
    assert s1.status == SeasonStatus.DOWNLOADING
    assert s2.status == SeasonStatus.RATE_LIMITED
    assert s3.status == SeasonStatus.RATE_LIMITED

    # Direct advance_season_buffer call while cooldown is active also queues target season
    from datetime import datetime, timezone

    item2, seasons2, _ = await make_series(
        db_session, title="Advance Show", seasons=2, episodes_per_season=1
    )
    seasons2[1].air_date = datetime(2024, 1, 1, tzinfo=timezone.utc)
    await db_session.commit()
    await advance_season_buffer(db_session, item2, base_watch_order=1)
    await db_session.refresh(seasons2[1])
    assert seasons2[1].status == SeasonStatus.RATE_LIMITED


async def _settle(gate):
    """Release the settle gate and let pending alert tasks finish."""
    import asyncio

    gate.set()
    await asyncio.sleep(0.05)
    gate.clear()


@pytest.fixture
def alerts(monkeypatch):
    """Capture rate-limit alerts sent through the shared alert dispatcher."""
    import asyncio

    from app.core import rate_limit_queue as rlq

    sent: list[tuple[str, str]] = []

    async def fake_alert(_session, title, body):
        sent.append((title, body))

    monkeypatch.setattr(rlq, "_send_alert", fake_alert)
    gate = asyncio.Event()

    async def settle():
        await gate.wait()

    monkeypatch.setattr(rlq, "_reached_settle_delay", settle)
    return sent, gate


async def test_episode_sends_exactly_two_alerts_for_many_items(db_session, alerts):
    sent, gate = alerts
    items = [await make_movie(db_session, simkl_id=100 + i) for i in range(50)]
    torbox.set_cooldown(600.0)
    with _patches()[0], _patches()[1], _patches()[2]:
        for it in items:
            await execute_auto_push(db_session, it.id, {})
    await _settle(gate)

    assert len(sent) == 1
    assert "50 push(es) paused" in sent[0][1] and "resumes" in sent[0][1]

    torbox.set_cooldown(0.0)
    p1, p2, p3 = _patches()
    with p1, p2, p3:
        await resume_rate_limited_pushes()
    assert len(sent) == 2
    assert "Resuming 50" in sent[1][1]

    # Episode ended (queue empty): a new limit starts a fresh episode
    assert await count_rate_limited_pushes(db_session) == 0
    extra = await make_movie(db_session, simkl_id=999)
    torbox.set_cooldown(600.0)
    with _patches()[0], _patches()[1], _patches()[2]:
        await execute_auto_push(db_session, extra.id, {})
    await _settle(gate)
    assert len(sent) == 3


async def test_re_429_during_resume_does_not_send_extra_alerts(db_session, alerts):
    sent, gate = alerts
    item = await make_movie(db_session)
    torbox.set_cooldown(600.0)
    with _patches()[0], _patches()[1], _patches()[2]:
        await execute_auto_push(db_session, item.id, {})
    await _settle(gate)
    torbox.set_cooldown(0.0)

    async def send_429(*_a, **_k):
        torbox.set_cooldown(300.0)
        raise torbox.DownloaderNetworkError(
            "TorBox rate limit exceeded (cooldown: 300s)"
        )

    p1, p2, p3 = _patches(AsyncMock(side_effect=send_429))
    with p1, p2, p3:
        await resume_rate_limited_pushes()
    await _settle(gate)

    assert len(sent) == 2  # one reached + one resumed, nothing more
    assert await count_rate_limited_pushes(db_session) == 1


async def test_alerts_gated_by_notify_on_errors(db_session):
    from app.core.rate_limit_queue import _send_alert
    from app.db.models import SystemSettings

    db_session.add(SystemSettings(id=1, notify_on_errors=False))
    await db_session.commit()
    with patch(
        "app.services.discord.telegram.send_notification", new=AsyncMock()
    ) as tg:
        await _send_alert(db_session, "t", "b")
    tg.assert_not_awaited()


async def test_reached_alert_waits_for_late_enqueues(db_session, alerts):
    """Pushes queued while the alert is settling are included in its count."""
    sent, gate = alerts
    first = await make_movie(db_session, simkl_id=301)
    torbox.set_cooldown(600.0)
    with _patches()[0], _patches()[1], _patches()[2]:
        await execute_auto_push(db_session, first.id, {})
        for i in range(3):  # queued after the episode (and alert task) started
            late = await make_movie(db_session, simkl_id=310 + i)
            await execute_auto_push(db_session, late.id, {})
    await _settle(gate)
    assert len(sent) == 1
    assert "4 push(es) paused" in sent[0][1]


async def test_adopted_queue_after_restart_sends_only_resumed(db_session, alerts):
    from app.core.rate_limit_queue import rate_limit_episode

    sent, gate = alerts
    item = await make_movie(db_session)
    torbox.set_cooldown(600.0)
    with _patches()[0], _patches()[1], _patches()[2]:
        await execute_auto_push(db_session, item.id, {})
    await _settle(gate)
    assert len(sent) == 1

    rate_limit_episode.reset()  # simulate restart
    rate_limit_episode.adopt_existing_queue()
    torbox.set_cooldown(0.0)
    p1, p2, p3 = _patches()
    with p1, p2, p3:
        await resume_rate_limited_pushes()
    assert len(sent) == 2 and "Resuming 1" in sent[1][1]


# --- m59-17: alerts/labels name the limited source ---------------------------


@pytest.fixture
def indexer_cooldown():
    from app.services.indexers import newznab

    def _set(seconds=600):
        newznab.register_indexer_names(
            [MagicMock(api_url="https://api.nzbgeek.info/api", name="x")]
        )
        newznab._indexer_names["api.nzbgeek.info"] = "NZBgeek"
        newznab.set_indexer_cooldown("https://api.nzbgeek.info/api", seconds)

    yield _set
    newznab.clear_all_indexer_cooldowns()
    newznab._indexer_names.clear()


@pytest.mark.parametrize(
    ("tb", "idx", "expected"),
    [
        (True, False, "TorBox"),
        (False, True, "NZBgeek"),
        (True, True, "TorBox + NZBgeek"),
        (False, False, "Rate limit"),
    ],
)
def test_rate_limit_source_label(indexer_cooldown, tb, idx, expected):
    from app.core.rate_limit_queue import (
        active_rate_limit_sources,
        rate_limit_source_label,
    )

    torbox.set_cooldown(600.0 if tb else 0.0)
    if idx:
        indexer_cooldown()
    sources = active_rate_limit_sources()
    assert rate_limit_source_label(sources) == expected
    assert {s.kind for s in sources} == (
        ({"downloader"} if tb else set()) | ({"indexer"} if idx else set())
    )


async def test_alert_titles_name_source_and_over_alert_after_clear(
    db_session, alerts, indexer_cooldown
):
    from app.core.rate_limit_queue import rate_limit_episode
    from app.services.indexers import newznab

    sent, gate = alerts
    rate_limit_episode.reset()
    torbox.set_cooldown(0.0)
    indexer_cooldown()
    rate_limit_episode.on_enqueue()
    await _settle(gate)
    assert sent[0][0] == "⏸️ NZBgeek rate limit reached"

    newznab.clear_all_indexer_cooldowns()  # cooldown cleared before resume
    await rate_limit_episode.on_resume(1)
    assert sent[1][0] == "▶️ NZBgeek rate limit over — queue resumed"
    rate_limit_episode.reset()
