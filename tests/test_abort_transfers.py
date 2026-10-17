"""
Tests for Core Abort Transfer Operations and Background Task Cancellation
========================================================================
Verifies abort_all_active_transfers, abort_item_transfer, and
cancel_in_flight_push_tasks.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.search_lock import item_lock_manager
from app.core.transfer_poller import (
    abort_all_active_transfers,
    abort_item_transfer,
    transfer_poller,
)
from app.db.models import (
    DownloadHistory,
    Episode,
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    Season,
    SeasonStatus,
)
from app.main import (
    _background_push_tasks,
    cancel_in_flight_push_tasks,
)
from tests.conftest import make_movie, make_series


@pytest.mark.asyncio
async def test_cancel_in_flight_push_tasks():
    """Verify that in-flight background tasks are cancelled, locks released, and poller state cleaned."""
    item_id = 999
    item_lock_manager.try_acquire(item_id, owner="manual")
    transfer_poller.register_in_flight_push(item_id, "In Flight Item")

    cancelled_event = asyncio.Event()

    async def dummy_coro():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled_event.set()
            raise

    task = asyncio.create_task(dummy_coro())
    setattr(task, "item_id", item_id)
    _background_push_tasks.add(task)
    task.add_done_callback(_background_push_tasks.discard)

    # Let the task enter its sleep
    await asyncio.sleep(0)

    canceled_count = await cancel_in_flight_push_tasks()

    assert canceled_count == 1
    assert cancelled_event.is_set()
    assert not item_lock_manager.is_locked(item_id)
    assert not transfer_poller.has_in_flight_pushes
    assert len(_background_push_tasks) == 0


@pytest.mark.asyncio
async def test_abort_all_active_transfers(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify abort_all_active_transfers deletes from TorBox, resets all active/rate-limited entities to SEARCHING, and marks history canceled."""
    import app.core.transfer_poller as poller_module

    deleted_tb_ids: list[str] = []

    async def fake_delete_purge(
        session: AsyncSession,
        tb_id: str | None,
        tb_item: dict[str, Any] | None = None,
        downloader_type: str | None = "torbox",
    ) -> None:
        if tb_id:
            deleted_tb_ids.append(str(tb_id))

    monkeypatch.setattr(
        poller_module,
        "_delete_and_purge_transfer",
        fake_delete_purge,
    )

    # 1. Movie in DOWNLOADING
    movie = await make_movie(
        db_session,
        title="Active Movie",
        status=MediaStatus.DOWNLOADING,
        simkl_id=101,
    )

    h_movie = DownloadHistory(
        media_item_id=movie.id,
        nzb_guid="guid-movie-1",
        nzb_title="Active.Movie.2024.1080p",
        downloader_transfer_id="tb-movie-1",
        dispatched_at=datetime.now(timezone.utc),
        status_detail="downloading",
        progress_pct=50.0,
        download_speed_bytes=5_000_000,
        eta_seconds=120,
        is_dismissed=False,
    )
    db_session.add(h_movie)

    # 2. Series with one downloading episode, one rate-limited episode, and one completed episode
    show, seasons, episodes = await make_series(
        db_session,
        seasons=1,
        episodes_per_season=3,
        title="Active Series",
        status=MediaStatus.DOWNLOADING,
        simkl_id=102,
    )
    season = seasons[0]
    season.status = SeasonStatus.DOWNLOADING
    ep1, ep2, ep3 = episodes
    ep1.status = EpisodeStatus.DOWNLOADING
    ep2.status = EpisodeStatus.RATE_LIMITED
    ep3.status = EpisodeStatus.COMPLETED

    h_ep1 = DownloadHistory(
        media_item_id=show.id,
        season_id=season.id,
        episode_id=ep1.id,
        nzb_guid="guid-ep-1",
        nzb_title="Active.Series.S01E01",
        downloader_transfer_id="tb-ep-1",
        dispatched_at=datetime.now(timezone.utc),
        status_detail="downloading",
        progress_pct=25.0,
        is_dismissed=False,
    )
    h_ep2 = DownloadHistory(
        media_item_id=show.id,
        season_id=season.id,
        episode_id=ep2.id,
        nzb_guid="guid-ep-2",
        nzb_title="Active.Series.S01E02",
        downloader_transfer_id="tb-ep-2",
        dispatched_at=datetime.now(timezone.utc),
        status_detail="rate_limited",
        progress_pct=0.0,
        is_dismissed=False,
    )
    h_ep3 = DownloadHistory(
        media_item_id=show.id,
        season_id=season.id,
        episode_id=ep3.id,
        nzb_guid="guid-ep-3",
        nzb_title="Active.Series.S01E03",
        downloader_transfer_id="tb-ep-3",
        dispatched_at=datetime.now(timezone.utc),
        status_detail="completed",
        progress_pct=100.0,
        is_dismissed=False,
    )
    db_session.add_all([h_ep1, h_ep2, h_ep3])
    await db_session.commit()

    # Register an in-flight push task as well
    transfer_poller.register_in_flight_push(888, "In Flight Auto Push")
    item_lock_manager.try_acquire(888, owner="manual")

    result = await abort_all_active_transfers(db_session)

    assert result["ok"] is True
    assert result["aborted_count"] == 3  # movie, ep1, ep2 (ep3 completed is untouched)
    assert set(result["canceled_transfer_ids"]) == {"tb-movie-1", "tb-ep-1", "tb-ep-2"}
    assert set(deleted_tb_ids) == {"tb-movie-1", "tb-ep-1", "tb-ep-2"}

    # Verify movie reverted to SEARCHING
    m = await db_session.get(MediaItem, movie.id)
    assert m is not None
    assert m.status == MediaStatus.SEARCHING

    # Verify show & episodes
    e1 = await db_session.get(Episode, ep1.id)
    assert e1 is not None
    assert e1.status == EpisodeStatus.SEARCHING

    e2 = await db_session.get(Episode, ep2.id)
    assert e2 is not None
    assert e2.status == EpisodeStatus.SEARCHING

    e3 = await db_session.get(Episode, ep3.id)
    assert e3 is not None
    assert e3.status == EpisodeStatus.COMPLETED

    # Verify history records
    hm = await db_session.get(DownloadHistory, h_movie.id)
    assert hm is not None
    assert hm.status_detail == "canceled"
    assert hm.is_dismissed is True
    assert hm.progress_pct == 0.0

    he1 = await db_session.get(DownloadHistory, h_ep1.id)
    assert he1 is not None
    assert he1.status_detail == "canceled"
    assert he1.is_dismissed is True

    he2 = await db_session.get(DownloadHistory, h_ep2.id)
    assert he2 is not None
    assert he2.status_detail == "canceled"
    assert he2.is_dismissed is True

    he3 = await db_session.get(DownloadHistory, h_ep3.id)
    assert he3 is not None
    assert he3.status_detail == "completed"
    assert he3.is_dismissed is False

    # Verify in-flight task cleared and poller is asleep
    assert not transfer_poller.has_in_flight_pushes
    assert not transfer_poller.is_awake


@pytest.mark.asyncio
async def test_abort_item_transfer_specific_episode(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify abort_item_transfer can target a specific episode transfer without affecting others."""
    import app.core.transfer_poller as poller_module

    deleted_tb_ids: list[str] = []

    async def fake_delete_purge(
        session: AsyncSession,
        tb_id: str | None,
        tb_item: dict[str, Any] | None = None,
        downloader_type: str | None = "torbox",
    ) -> None:
        if tb_id:
            deleted_tb_ids.append(str(tb_id))

    monkeypatch.setattr(
        poller_module,
        "_delete_and_purge_transfer",
        fake_delete_purge,
    )

    show, seasons, episodes = await make_series(
        db_session,
        seasons=1,
        episodes_per_season=2,
        title="Targeted Series",
        status=MediaStatus.DOWNLOADING,
        simkl_id=201,
    )
    season = seasons[0]
    season.status = SeasonStatus.DOWNLOADING
    ep1, ep2 = episodes
    ep1.status = EpisodeStatus.DOWNLOADING
    ep2.status = EpisodeStatus.DOWNLOADING

    h_ep1 = DownloadHistory(
        media_item_id=show.id,
        season_id=season.id,
        episode_id=ep1.id,
        nzb_guid="guid-target-1",
        nzb_title="Target.S01E01",
        downloader_transfer_id="tb-target-1",
        dispatched_at=datetime.now(timezone.utc),
        status_detail="downloading",
        progress_pct=30.0,
        is_dismissed=False,
    )
    h_ep2 = DownloadHistory(
        media_item_id=show.id,
        season_id=season.id,
        episode_id=ep2.id,
        nzb_guid="guid-target-2",
        nzb_title="Target.S01E02",
        downloader_transfer_id="tb-target-2",
        dispatched_at=datetime.now(timezone.utc),
        status_detail="downloading",
        progress_pct=60.0,
        is_dismissed=False,
    )
    db_session.add_all([h_ep1, h_ep2])
    await db_session.commit()

    res = await abort_item_transfer(
        db_session,
        item_id=show.id,
        season_id=season.id,
        episode_id=ep1.id,
    )

    assert res["status"] == "ok"
    assert res["deleted_transfer_ids"] == ["tb-target-1"]
    assert deleted_tb_ids == ["tb-target-1"]

    e1 = await db_session.get(Episode, ep1.id)
    assert e1 is not None
    assert e1.status == EpisodeStatus.SEARCHING

    e2 = await db_session.get(Episode, ep2.id)
    assert e2 is not None
    assert e2.status == EpisodeStatus.DOWNLOADING

    he1 = await db_session.get(DownloadHistory, h_ep1.id)
    assert he1 is not None
    assert he1.status_detail == "canceled"
    assert he1.is_dismissed is True

    he2 = await db_session.get(DownloadHistory, h_ep2.id)
    assert he2 is not None
    assert he2.status_detail == "downloading"
    assert he2.is_dismissed is False


@pytest.mark.asyncio
async def test_abort_item_transfer_season_level(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify abort_item_transfer targeting a season aborts active downloads for the season and its episodes."""
    import app.core.transfer_poller as poller_module

    deleted_tb_ids: list[str] = []

    async def fake_delete_purge(
        session: AsyncSession,
        tb_id: str | None,
        tb_item: dict[str, Any] | None = None,
        downloader_type: str | None = "torbox",
    ) -> None:
        if tb_id:
            deleted_tb_ids.append(str(tb_id))

    monkeypatch.setattr(
        poller_module,
        "_delete_and_purge_transfer",
        fake_delete_purge,
    )

    show, seasons, episodes = await make_series(
        db_session,
        seasons=1,
        episodes_per_season=2,
        title="Season Abort Series",
        status=MediaStatus.DOWNLOADING,
        simkl_id=301,
    )
    season = seasons[0]
    season.status = SeasonStatus.DOWNLOADING
    ep1, ep2 = episodes
    ep1.status = EpisodeStatus.DOWNLOADING
    ep2.status = EpisodeStatus.DOWNLOADING

    h_ep1 = DownloadHistory(
        media_item_id=show.id,
        season_id=season.id,
        episode_id=ep1.id,
        nzb_guid="guid-s-1",
        nzb_title="S1.E01",
        downloader_transfer_id="tb-s-1",
        dispatched_at=datetime.now(timezone.utc),
        status_detail="downloading",
        is_dismissed=False,
    )
    h_ep2 = DownloadHistory(
        media_item_id=show.id,
        season_id=season.id,
        episode_id=ep2.id,
        nzb_guid="guid-s-2",
        nzb_title="S1.E02",
        downloader_transfer_id="tb-s-2",
        dispatched_at=datetime.now(timezone.utc),
        status_detail="downloading",
        is_dismissed=False,
    )
    db_session.add_all([h_ep1, h_ep2])
    await db_session.commit()

    res = await abort_item_transfer(
        db_session,
        item_id=show.id,
        season_id=season.id,
    )

    assert res["status"] == "ok"
    assert set(res["deleted_transfer_ids"]) == {"tb-s-1", "tb-s-2"}
    assert set(deleted_tb_ids) == {"tb-s-1", "tb-s-2"}

    s = await db_session.get(Season, season.id)
    assert s is not None
    assert s.status == SeasonStatus.SEARCHING

    e1 = await db_session.get(Episode, ep1.id)
    assert e1 is not None
    assert e1.status == EpisodeStatus.SEARCHING

    e2 = await db_session.get(Episode, ep2.id)
    assert e2 is not None
    assert e2.status == EpisodeStatus.SEARCHING


@pytest.mark.asyncio
async def test_abort_item_transfer_movie(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify abort_item_transfer targeting a movie aborts the movie transfer and reverts to SEARCHING."""
    import app.core.transfer_poller as poller_module

    deleted_tb_ids: list[str] = []

    async def fake_delete_purge(
        session: AsyncSession,
        tb_id: str | None,
        tb_item: dict[str, Any] | None = None,
        downloader_type: str | None = "torbox",
    ) -> None:
        if tb_id:
            deleted_tb_ids.append(str(tb_id))

    monkeypatch.setattr(
        poller_module,
        "_delete_and_purge_transfer",
        fake_delete_purge,
    )

    movie = await make_movie(
        db_session,
        title="Abort Movie",
        status=MediaStatus.DOWNLOADING,
        simkl_id=401,
    )

    h_movie = DownloadHistory(
        media_item_id=movie.id,
        nzb_guid="guid-abort-movie",
        nzb_title="Abort.Movie.2024",
        downloader_transfer_id="tb-abort-movie",
        dispatched_at=datetime.now(timezone.utc),
        status_detail="downloading",
        is_dismissed=False,
    )
    db_session.add(h_movie)
    await db_session.commit()

    res = await abort_item_transfer(
        db_session,
        item_id=movie.id,
    )

    assert res["status"] == "ok"
    assert res["deleted_transfer_ids"] == ["tb-abort-movie"]
    assert deleted_tb_ids == ["tb-abort-movie"]

    m = await db_session.get(MediaItem, movie.id)
    assert m is not None
    assert m.status == MediaStatus.SEARCHING

    hm = await db_session.get(DownloadHistory, h_movie.id)
    assert hm is not None
    assert hm.status_detail == "canceled"
    assert hm.is_dismissed is True


@pytest.mark.asyncio
async def test_cancel_in_flight_push_tasks_item_specific():
    """Verify cancel_in_flight_push_tasks can cancel a specific item's task while leaving others running."""
    item1 = 111
    item2 = 222

    item_lock_manager.try_acquire(item1, owner="manual")
    item_lock_manager.try_acquire(item2, owner="manual")
    transfer_poller.register_in_flight_push(item1, "Item 1")
    transfer_poller.register_in_flight_push(item2, "Item 2")

    task1_cancelled = asyncio.Event()
    task2_cancelled = asyncio.Event()

    async def coro1():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            task1_cancelled.set()
            raise

    async def coro2():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            task2_cancelled.set()
            raise

    t1 = asyncio.create_task(coro1())
    setattr(t1, "item_id", item1)
    _background_push_tasks.add(t1)
    t1.add_done_callback(_background_push_tasks.discard)

    t2 = asyncio.create_task(coro2())
    setattr(t2, "item_id", item2)
    _background_push_tasks.add(t2)
    t2.add_done_callback(_background_push_tasks.discard)

    await asyncio.sleep(0)

    # Cancel only item1
    canceled_count = await cancel_in_flight_push_tasks(item_id=item1)
    assert canceled_count == 1
    assert task1_cancelled.is_set()
    assert not task2_cancelled.is_set()
    assert not item_lock_manager.is_locked(item1)
    assert item_lock_manager.is_locked(item2)
    assert item1 not in transfer_poller.in_flight_pushes
    assert item2 in transfer_poller.in_flight_pushes

    # Clean up t2
    await cancel_in_flight_push_tasks(item_id=item2)
    assert task2_cancelled.is_set()


@pytest.mark.asyncio
async def test_api_abort_all_json_and_htmx(
    async_client,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify POST /api/pushes/abort-all handles both JSON and HTMX requests properly."""
    import app.core.transfer_poller as poller_module

    deleted_tb_ids: list[str] = []

    async def mock_delete_and_purge(session, torbox_id, tb_item=None, **_kw):
        deleted_tb_ids.append(str(torbox_id))
        return True

    monkeypatch.setattr(
        poller_module, "_delete_and_purge_transfer", mock_delete_and_purge
    )

    movie = await make_movie(
        db_session,
        title="Abort API Movie",
        status=MediaStatus.DOWNLOADING,
    )
    hist = DownloadHistory(
        media_item_id=movie.id,
        nzb_title="Abort API Movie 1080p",
        nzb_guid="guid-api-1",
        downloader_transfer_id="tb_api_movie",
        status_detail="downloading",
        is_dismissed=False,
        progress_pct=45.0,
    )
    db_session.add(hist)
    await db_session.commit()

    # 1. JSON Request
    resp = await async_client.post(
        "/api/pushes/abort-all",
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["aborted_count"] == 1
    assert "tb_api_movie" in data["canceled_transfer_ids"]

    # Re-seed for HTMX test
    movie.status = MediaStatus.DOWNLOADING
    hist2 = DownloadHistory(
        media_item_id=movie.id,
        nzb_title="Abort API Movie 1080p v2",
        nzb_guid="guid-api-2",
        downloader_transfer_id="tb_api_movie_2",
        status_detail="rate_limited",
        is_dismissed=False,
    )
    db_session.add(hist2)
    await db_session.commit()

    # 2. HTMX Request
    resp_htmx = await async_client.post(
        "/api/pushes/abort-all",
        headers={"HX-Request": "true"},
    )
    assert resp_htmx.status_code == 200
    assert "refreshDashboard" in resp_htmx.headers.get("HX-Trigger", "")
    assert "smewlet:poller-state" in resp_htmx.headers.get("HX-Trigger", "")


@pytest.mark.asyncio
async def test_api_abort_item_transfer_json_and_htmx(
    async_client,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify POST /api/items/{item_id}/abort-transfer handles single transfers and returns re-rendered modal for HTMX."""
    import app.core.transfer_poller as poller_module

    deleted_tb_ids: list[str] = []

    async def mock_delete_and_purge(session, torbox_id, tb_item=None, **_kw):
        deleted_tb_ids.append(str(torbox_id))
        return True

    monkeypatch.setattr(
        poller_module, "_delete_and_purge_transfer", mock_delete_and_purge
    )

    movie = await make_movie(
        db_session,
        title="Abort Item Movie",
        status=MediaStatus.DOWNLOADING,
    )
    hist = DownloadHistory(
        media_item_id=movie.id,
        nzb_title="Abort Item Movie 1080p",
        nzb_guid="guid-item-mov",
        downloader_transfer_id="tb_item_mov",
        status_detail="downloading",
        is_dismissed=False,
        progress_pct=25.0,
    )
    db_session.add(hist)
    await db_session.commit()

    # 1. HTMX Request
    resp_htmx = await async_client.post(
        f"/api/items/{movie.id}/abort-transfer",
        headers={"HX-Request": "true"},
    )
    assert resp_htmx.status_code == 200
    assert "refreshDashboard" in resp_htmx.headers.get("HX-Trigger", "")
    assert "modal-container" in resp_htmx.text or "Abort Item Movie" in resp_htmx.text

    # Verify movie reverted to SEARCHING
    await db_session.refresh(movie)
    assert movie.status == MediaStatus.SEARCHING

    # 2. Episode abort test
    series, seasons, episodes = await make_series(
        db_session,
        title="Abort Ep Series",
        seasons=1,
        episodes_per_season=2,
    )
    ep1 = episodes[0]
    ep1.status = EpisodeStatus.DOWNLOADING
    seasons[0].status = SeasonStatus.DOWNLOADING
    series.status = MediaStatus.DOWNLOADING
    hist_ep = DownloadHistory(
        media_item_id=series.id,
        season_id=seasons[0].id,
        episode_id=ep1.id,
        nzb_title="Abort Ep Series S01E01",
        nzb_guid="guid-ep-1",
        downloader_transfer_id="tb_ep_1",
        status_detail="downloading",
        is_dismissed=False,
    )
    db_session.add(hist_ep)
    await db_session.commit()

    resp_ep = await async_client.post(
        f"/api/items/{series.id}/abort-transfer?season_id={seasons[0].id}&episode_id={ep1.id}",
        headers={"HX-Request": "true"},
    )
    assert resp_ep.status_code == 200
    assert "refreshDashboard" in resp_ep.headers.get("HX-Trigger", "")

    await db_session.refresh(ep1)
    assert ep1.status == EpisodeStatus.SEARCHING


@pytest.mark.asyncio
async def test_ui_card_5_abort_all_rendering(
    async_client,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify Card 5 header and active_pushes_table.html render [Abort All] button when active transfers exist."""
    from app.services.downloaders import torbox

    async def mock_get_usenet_downloads(*args, **kwargs):
        return [
            {
                "id": "tb-ui-1",
                "download_state": "downloading",
                "progress": 0.2,
                "download_speed": 1000000,
                "eta": 100,
            }
        ]

    monkeypatch.setattr(torbox, "get_usenet_downloads", mock_get_usenet_downloads)

    movie = await make_movie(
        db_session,
        title="UI Active Movie",
        status=MediaStatus.DOWNLOADING,
    )
    hist = DownloadHistory(
        media_item_id=movie.id,
        nzb_title="UI Active Movie 1080p",
        nzb_guid="guid-ui-1",
        downloader_transfer_id="tb-ui-1",
        status_detail="downloading",
        is_dismissed=False,
        progress_pct=20.0,
    )
    db_session.add(hist)
    await db_session.commit()

    # 1. Fetch dashboard HTML and verify Card 5 Abort All button
    dash_resp = await async_client.get("/")
    assert dash_resp.status_code == 200
    html = dash_resp.text
    assert 'hx-post="/api/pushes/abort-all"' in html
    assert 'hx-target="#active-pushes-container"' in html
    assert 'hx-confirm="Abort and delete all active transfers from downloader?"' in html
    assert "border-red-800 text-red-400" in html
    assert "Abort All" in html

    # 2. Fetch active pushes partial with in-flight task to verify in-flight banner is displayed
    transfer_poller.register_in_flight_push(movie.id, "UI Active Movie")
    try:
        table_resp = await async_client.get(
            "/api/pushes/active",
            headers={"HX-Request": "true"},
        )
        assert table_resp.status_code == 200
        table_html = table_resp.text
        assert 'data-in-flight-banner="true"' in table_html
        assert "Searching indexers &amp; dispatching to downloader:" in table_html
        assert "UI Active Movie" in table_html
    finally:
        transfer_poller.unregister_in_flight_push(movie.id)


@pytest.mark.asyncio
async def test_ui_push_modal_inline_abort_buttons(
    async_client,
    db_session: AsyncSession,
):
    """Verify Unified Push Modal renders inline [Abort Transfer] buttons for DOWNLOADING and RATE_LIMITED seasons/episodes/movies."""
    # 1. Series with DOWNLOADING season & RATE_LIMITED episode
    series, seasons, episodes = await make_series(
        db_session,
        title="Modal Series",
        seasons=1,
        episodes_per_season=2,
    )
    season = seasons[0]
    season.status = SeasonStatus.DOWNLOADING
    ep1, ep2 = episodes
    ep1.status = EpisodeStatus.DOWNLOADING
    ep2.status = EpisodeStatus.RATE_LIMITED
    series.status = MediaStatus.DOWNLOADING

    await db_session.commit()

    resp_series = await async_client.get(f"/api/items/{series.id}/push-modal")
    assert resp_series.status_code == 200
    series_html = resp_series.text

    # Verify season abort button
    season_abort_url = f"/api/items/{series.id}/abort-transfer?season_id={season.id}"
    assert f'hx-post="{season_abort_url}"' in series_html
    assert 'hx-target="#modal-container"' in series_html
    assert "Abort Transfer" in series_html

    # Verify episode abort buttons
    ep1_abort_url = f"/api/items/{series.id}/abort-transfer?season_id={season.id}&episode_id={ep1.id}"
    assert f'hx-post="{ep1_abort_url}"' in series_html

    ep2_abort_url = f"/api/items/{series.id}/abort-transfer?season_id={season.id}&episode_id={ep2.id}"
    assert f'hx-post="{ep2_abort_url}"' in series_html

    # 2. Standalone Movie in DOWNLOADING status
    movie = await make_movie(
        db_session,
        title="Modal Movie",
        status=MediaStatus.DOWNLOADING,
    )
    await db_session.commit()

    resp_movie = await async_client.get(f"/api/items/{movie.id}/push-modal")
    assert resp_movie.status_code == 200
    movie_html = resp_movie.text
    movie_abort_url = f"/api/items/{movie.id}/abort-transfer"
    assert f'hx-post="{movie_abort_url}"' in movie_html
    assert 'hx-target="#modal-container"' in movie_html
    assert "Abort Transfer" in movie_html
