import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.transfer_poller import (
    batch_delete_history_entries,
    clear_history_ledger,
)
from app.db.models import DownloadHistory, MediaStatus
from app.main import count_history_by_status, count_history_filter_buckets
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
    assert "selectAllHistory" in html
    assert "deselectAllHistory" in html
    assert "toggleHistoryRowSelection" in html
    assert "batchDeleteHistory" in html
    assert "clearAllHistory" in html
    # Checkbox inputs
    assert 'x-model.number="selectedHistoryIds"' in html
    assert f'value="{hist.id}"' in html


def test_count_history_by_status_counts_pinned_keys_and_ignores_unknown():
    ledger = [
        {"downloader_status": "FAILED"},
        {"downloader_status": "FAILED"},
        {"downloader_status": "READY"},
        {"downloader_status": "weird"},
        {"downloader_status": None},
    ]
    assert count_history_by_status(ledger) == {
        "failed": 2,
        "canceled": 0,
        "replaced": 0,
        "deleted": 0,
        "ready": 1,
    }


@pytest.mark.asyncio
async def test_dashboard_history_status_quick_selects(
    async_client: AsyncClient, db_session: AsyncSession
):
    movie = await make_movie(db_session, title="QS Movie", status=MediaStatus.COMPLETED)
    for i, detail in enumerate(
        ["failed: Disk full", "failed: x", "completed", "replaced", "deleted"]
    ):
        await make_history(
            db_session,
            item=movie,
            downloader_transfer_id=f"tb-qs-{i}",
            nzb_guid=f"guid-qs-{i}",
            status_detail=detail,
        )
    await db_session.commit()

    html = (await async_client.get("/?tab=history")).text

    labels = [
        "Failed (2)",
        "Canceled (0)",
        "Replaced (1)",
        "Deleted (1)",
        "Ready (1)",
    ]
    positions = [html.index(label) for label in labels]
    assert positions == sorted(positions)
    for call in (
        "selectHistoryByStatus('failed')",
        "enterHistorySelection()",
        "exitHistorySelection()",
        "selectAllHistory()",
        "deselectAllHistory()",
    ):
        assert call in html
    assert "Selected: <span" in html
    assert (
        "bg-red-500/15 text-red-400 border border-red-500/30 hover:bg-red-500/25"
        in html
    )
    assert "? 'Deselect All' : 'Select All'" not in html
    assert "toggleHistorySelectAll" not in html

    canceled_btn = html[html.index("selectHistoryByStatus('canceled')") :][:700]
    assert "opacity-50 cursor-not-allowed pointer-events-none" in canceled_btn
    failed_btn = html[html.index("selectHistoryByStatus('failed')") :][:700]
    assert "opacity-50 cursor-not-allowed" not in failed_btn


@pytest.mark.asyncio
async def test_dashboard_history_mobile_footer_layout(
    async_client: AsyncClient, db_session: AsyncSession
):
    movie = await make_movie(
        db_session, title="Mobile Movie", status=MediaStatus.COMPLETED
    )
    ready = await make_history(
        db_session,
        item=movie,
        downloader_transfer_id="tb-m-1",
        nzb_guid="guid-m-1",
        status_detail="completed",
    )
    await make_history(
        db_session,
        item=movie,
        downloader_transfer_id="tb-m-2",
        nzb_guid="guid-m-2",
        status_detail="failed: Disk full",
    )
    await make_history(
        db_session,
        item=movie,
        downloader_transfer_id="tb-m-3",
        nzb_guid="guid-m-3",
        status_detail="completed",
        is_dry_run=True,
    )
    await db_session.commit()

    html = (await async_client.get("/?tab=history")).text

    assert "md:hidden mt-3 space-y-2.5" in html
    assert "flex flex-wrap items-center gap-2 pt-0.5 text-xs" not in html
    assert "border-t border-[#3f3f46]/40" in html
    assert "text-gray-400 font-mono text-[11px] leading-tight" in html
    assert "md:hidden flex flex-col" in html
    footer = html[html.index("md:hidden flex flex-col") :]
    assert "READY ON " in footer
    assert 'title="Disk full"' in footer
    assert "SIMULATED (DRY RUN)" in footer
    assert html.count(f'id="ledger-status-{ready.id}"') == 1


def test_count_history_filter_buckets_dry_run_only_counts_as_simulated():
    ledger = [
        {"downloader_status": "FAILED", "is_dry_run": False},
        {"downloader_status": "FAILED", "is_dry_run": False},
        {"downloader_status": "READY", "is_dry_run": True},
        {"downloader_status": "weird", "is_dry_run": False},
    ]
    assert count_history_filter_buckets(ledger) == {
        "failed": 2,
        "canceled": 0,
        "replaced": 0,
        "deleted": 0,
        "ready": 0,
        "simulated": 1,
    }


LONG_NZB = "Some.Very.Long.Movie.Name.2024.2160p.UHD.BluRay.REMUX.HDR10.HEVC.DTS-HD.MA.7.1-GROUP"
GRAY_PILL = "bg-[#1e1e24] text-gray-300 border border-[#3f3f46]"


@pytest.mark.asyncio
async def test_dashboard_history_filter_bar_and_row_markup(
    async_client: AsyncClient, db_session: AsyncSession
):
    movie = await make_movie(
        db_session, title="Bar Movie", status=MediaStatus.COMPLETED
    )
    for i, detail in enumerate(["failed: Disk full", "completed"]):
        await make_history(
            db_session,
            item=movie,
            downloader_transfer_id=f"tb-bar-{i}",
            nzb_guid=f"guid-bar-{i}",
            nzb_title=LONG_NZB,
            status_detail=detail,
        )
    await db_session.commit()

    html = (await async_client.get("/?tab=history")).text

    assert html.index("toggleHistoryStatusFilter('failed')") < html.index(
        'id="dashboard-history-table-body"'
    )
    bar = html[html.index("toggleHistoryStatusFilter('failed')") :]
    positions = [
        bar.index(label)
        for label in (
            "Failed (1)",
            "Canceled (0)",
            "Replaced (0)",
            "Deleted (0)",
            "Ready (1)",
        )
    ]
    assert positions == sorted(positions)
    assert "toggleHistoryStatusFilter('simulated')" not in html
    assert "toggleHistoryStatusFilter('')" not in html
    assert "ring-2 ring-white/60" in html
    assert (
        "bg-emerald-500/15 text-emerald-400 border border-emerald-500/30 hover:bg-emerald-500/25"
        in html
    )
    zero = html[html.index("toggleHistoryStatusFilter('canceled')") :][:600]
    assert "opacity-50 cursor-not-allowed pointer-events-none" in zero
    assert 'aria-disabled="true"' in zero
    nonzero = html[html.index("toggleHistoryStatusFilter('failed')") :][:600]
    assert "opacity-50" not in nonzero

    assert 'data-filter-key="failed"' in html
    assert "historyRowMatches($el)" in html
    assert ".toLowerCase().includes(historySearch.toLowerCase())" not in html
    assert 'data-empty-history-row="true"' in html
    assert "No history entries matching this filter." in html
    assert (
        "enterHistorySelection() { this.historySearch = ''; this.historyStatusFilter = ''; "
        in html
    )

    # Movie badge de-duplication and unwrapped filenames
    assert html.count(">MOVIE</span>") == 2
    assert GRAY_PILL not in html[html.index('id="dashboard-history-table-body"') :]
    assert f'break-all" title="{LONG_NZB}"' in html
    assert f'truncate" title="{LONG_NZB}"' not in html
    assert "md:max-w-[380px]" not in html


@pytest.mark.asyncio
async def test_dashboard_history_simulated_pill_and_episodic_pill(
    async_client: AsyncClient, db_session: AsyncSession
):
    movie = await make_movie(
        db_session, title="Sim Movie", status=MediaStatus.COMPLETED
    )
    await make_history(
        db_session,
        item=movie,
        downloader_transfer_id="tb-sim-1",
        nzb_guid="guid-sim-1",
        status_detail="completed",
        is_dry_run=True,
    )
    await db_session.commit()

    html = (await async_client.get("/?tab=history")).text

    assert 'data-filter-key="simulated"' in html
    assert html.index("toggleHistoryStatusFilter('ready')") < html.index(
        "toggleHistoryStatusFilter('simulated')"
    )
    badge = html[html.index("border-blue-500/20 inline-flex") :][:900]
    assert badge.index("<svg") < badge.index("MOVIE</span>")
    assert GRAY_PILL not in html[html.index('id="dashboard-history-table-body"') :]


@pytest.mark.asyncio
async def test_dashboard_history_empty_ledger_has_no_filter_empty_row(
    async_client: AsyncClient,
):
    html = (await async_client.get("/?tab=history")).text
    assert "No push history entries recorded yet." in html
    assert 'data-empty-history-row="true"' not in html


@pytest.mark.asyncio
async def test_dashboard_selection_bar_has_simulated_quick_select(
    async_client: AsyncClient, db_session: AsyncSession
):
    movie = await make_movie(db_session, title="QS Sim", status=MediaStatus.COMPLETED)
    await make_history(
        db_session,
        item=movie,
        downloader_transfer_id="tb-qs-sim",
        nzb_guid="guid-qs-sim",
        status_detail="completed",
        is_dry_run=True,
    )
    await db_session.commit()

    html = (await async_client.get("/?tab=history")).text

    assert "'simulated']" in html
    btn = html[html.index("selectHistoryByStatus('simulated')") :][:700]
    assert "Simulated (1)" in btn
    assert "opacity-50" not in btn
    assert "data-filter-key" in html[html.index("selectHistoryByStatus(key)") :][:500]
