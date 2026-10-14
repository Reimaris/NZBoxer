import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.transfer_poller import (
    batch_delete_history_entries,
    clear_history_ledger,
)
from app.db.models import DownloadHistory, MediaStatus
from tests.conftest import make_history, make_movie


@pytest.mark.asyncio
async def test_batch_delete_history_entries_empty_and_nonexistent(
    db_session: AsyncSession,
):
    # Empty list
    res = await batch_delete_history_entries(db_session, [])
    assert res == {"deleted_count": 0, "deleted_ids": []}

    # Non-existent IDs
    res = await batch_delete_history_entries(db_session, [9999, 10000])
    assert res == {"deleted_count": 0, "deleted_ids": []}


@pytest.mark.asyncio
async def test_batch_delete_history_entries_success_and_parent_reconcile(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    # Ensure torbox delete is NEVER called
    torbox_delete_called = False

    async def fake_delete(*args, **kwargs):
        nonlocal torbox_delete_called
        torbox_delete_called = True
        raise AssertionError("TorBox delete should not be called")

    monkeypatch.setattr(
        "app.services.downloaders.torbox.delete_usenet_download", fake_delete
    )
    monkeypatch.setattr(
        "app.services.downloaders.torbox.delete_torrent_download", fake_delete
    )

    movie = await make_movie(
        db_session, title="Batch Movie", status=MediaStatus.COMPLETED
    )
    hist1 = await make_history(
        db_session, item=movie, downloader_transfer_id="tb-1", status_detail="completed"
    )
    hist2 = await make_history(
        db_session, item=movie, downloader_transfer_id="tb-2", status_detail="completed"
    )
    await db_session.commit()

    # Partial delete: delete hist1, hist2 remains -> movie should remain COMPLETED
    res = await batch_delete_history_entries(db_session, [hist1.id, 99999])
    assert res["deleted_count"] == 1
    assert res["deleted_ids"] == [hist1.id]
    assert not torbox_delete_called

    await db_session.refresh(movie)
    assert movie.status == MediaStatus.COMPLETED

    # Delete remaining hist2 -> movie should reconcile to SEARCHING
    res = await batch_delete_history_entries(db_session, [hist2.id])
    assert res["deleted_count"] == 1
    assert res["deleted_ids"] == [hist2.id]
    assert not torbox_delete_called

    await db_session.refresh(movie)
    assert movie.status == MediaStatus.SEARCHING


@pytest.mark.asyncio
async def test_clear_history_ledger(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    torbox_delete_called = False

    async def fake_delete(*args, **kwargs):
        nonlocal torbox_delete_called
        torbox_delete_called = True
        raise AssertionError("TorBox delete should not be called")

    monkeypatch.setattr(
        "app.services.downloaders.torbox.delete_usenet_download", fake_delete
    )
    monkeypatch.setattr(
        "app.services.downloaders.torbox.delete_torrent_download", fake_delete
    )

    movie1 = await make_movie(
        db_session, title="Clear Movie 1", status=MediaStatus.COMPLETED
    )
    movie2 = await make_movie(
        db_session, title="Clear Movie 2", status=MediaStatus.COMPLETED
    )
    await make_history(
        db_session,
        item=movie1,
        downloader_transfer_id="tb-c1",
        status_detail="completed",
    )
    await make_history(
        db_session,
        item=movie2,
        downloader_transfer_id="tb-c2",
        status_detail="completed",
    )
    await db_session.commit()

    res = await clear_history_ledger(db_session)
    assert res["cleared_count"] == 2
    assert not torbox_delete_called

    # DownloadHistory should be empty
    remaining = (await db_session.execute(select(DownloadHistory))).scalars().all()
    assert len(remaining) == 0

    # Both movies should be reconciled to SEARCHING
    await db_session.refresh(movie1)
    await db_session.refresh(movie2)
    assert movie1.status == MediaStatus.SEARCHING
    assert movie2.status == MediaStatus.SEARCHING


@pytest.mark.asyncio
async def test_api_batch_delete_and_clear_endpoints(
    async_client: AsyncClient, db_session: AsyncSession
):
    movie = await make_movie(
        db_session, title="API Movie", status=MediaStatus.COMPLETED
    )
    h1 = await make_history(
        db_session,
        item=movie,
        downloader_transfer_id="tb-a1",
        status_detail="completed",
    )
    h2 = await make_history(
        db_session,
        item=movie,
        downloader_transfer_id="tb-a2",
        status_detail="completed",
    )
    h3 = await make_history(
        db_session,
        item=movie,
        downloader_transfer_id="tb-a3",
        status_detail="completed",
    )
    await db_session.commit()

    # 1. Batch delete via JSON
    resp_json = await async_client.post(
        "/api/history/batch-delete",
        json={"history_ids": [h1.id]},
        headers={"HX-Request": "true"},
    )
    assert resp_json.status_code == 200
    assert resp_json.headers.get("HX-Trigger") == "refreshDashboard"

    # 2. Batch delete via Form data
    resp_form = await async_client.post(
        "/api/history/batch-delete",
        data={"history_ids": [str(h2.id)]},
    )
    assert resp_form.status_code == 200
    assert resp_form.headers.get("HX-Trigger") == "refreshDashboard"
    data = resp_form.json()
    assert data["deleted_count"] == 1
    assert data["deleted_ids"] == [h2.id]

    # Verify only h3 remains
    remaining = (await db_session.execute(select(DownloadHistory))).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].id == h3.id

    # 3. Clear all endpoint
    resp_clear = await async_client.post(
        "/api/history/clear", headers={"HX-Request": "true"}
    )
    assert resp_clear.status_code == 200
    assert resp_clear.headers.get("HX-Trigger") == "refreshDashboard"

    # Verify DB empty
    remaining_after_clear = (
        (await db_session.execute(select(DownloadHistory))).scalars().all()
    )
    assert len(remaining_after_clear) == 0

    await db_session.refresh(movie)
    assert movie.status == MediaStatus.SEARCHING


@pytest.mark.asyncio
async def test_dashboard_renders_card_4_selection_mode_controls(
    async_client: AsyncClient, db_session: AsyncSession
):
    movie = await make_movie(
        db_session, title="Dashboard Movie", status=MediaStatus.COMPLETED
    )
    hist = await make_history(
        db_session,
        item=movie,
        downloader_transfer_id="tb-dash-1",
        status_detail="completed",
    )
    await db_session.commit()

    resp = await async_client.get("/?tab=history")
    assert resp.status_code == 200
    html = resp.text

    # Selection mode toggle button
    assert "Select Entries" in html
    # Selection action bar controls
    assert "Delete Selected" in html
    assert "Clear All History" in html
    assert "Cancel" in html or "Done" in html
    assert "toggleHistorySelectAll" in html
    assert "toggleHistoryRowSelection" in html
    assert "batchDeleteHistory" in html
    assert "clearAllHistory" in html
    # Checkbox inputs
    assert 'x-model.number="selectedHistoryIds"' in html
    assert f'value="{hist.id}"' in html
