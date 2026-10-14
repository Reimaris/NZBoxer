from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.orm import selectinload

import app.core.transfer_poller as transfer_poller_module
from app.core.transfer_poller import (
    _run_transfer_poller_tick_with_session,
    cancel_active_push,
    manual_delete_transfer,
    reconcile_completed_history_with_downloader,
)
from app.db.models import (
    DownloadHistory,
    Episode,
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    MediaType,
    Provider,
    Season,
    SeasonStatus,
    SystemSettings,
)


@pytest.fixture
async def ledger_session_factory(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[None, async_sessionmaker[AsyncSession]]:
    db_session.add_all(
        [
            SystemSettings(id=1),
            Provider(
                id=1,
                name="Simkl",
                type="simkl",
                category="watchlist",
                is_active=True,
            ),
        ]
    )
    await db_session.commit()
    return None, session_factory


@pytest.mark.asyncio
async def test_auto_replaced_and_manual_repush_transition_to_replaced(
    ledger_session_factory: tuple[None, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance Criterion 1: Superseded DownloadHistory rows transition to status_detail='replaced' without being deleted."""
    _, factory = ledger_session_factory

    async with factory() as session:
        movie = MediaItem(
            provider_id=1,
            simkl_id=9001,
            title="Blade Runner 2049",
            media_type=MediaType.MOVIE,
            status=MediaStatus.DOWNLOADING,
            year=2017,
            fail_count=0,
        )
        session.add(movie)
        await session.flush()

        hist = DownloadHistory(
            media_item_id=movie.id,
            nzb_guid="guid-failed-release",
            nzb_title="Blade.Runner.2049.2017.1080p.WEB-DL.x264-DEAD",
            resolution="1080p",
            size_bytes=8_000_000_000,
            downloader_transfer_id="tb-old-failed",
            dispatched_at=datetime.now(UTC),
            status_detail="downloading",
            is_dismissed=False,
            notification_sent=False,
        )
        session.add(hist)
        await session.commit()
        movie_id = movie.id
        old_hist_id = hist.id

    deleted_tb_ids: list[str] = []

    async def fake_delete_purge(
        session: AsyncSession,
        tb_id: str | None,
        tb_item: dict[str, Any] | None = None,
        downloader_type: str | None = "torbox",
    ) -> None:
        if tb_id:
            deleted_tb_ids.append(tb_id)

    async def fake_auto_replace(
        session: AsyncSession,
        item: MediaItem,
        season: Season | None = None,
        episode: Episode | None = None,
    ) -> DownloadHistory | None:
        new_hist = DownloadHistory(
            media_item_id=item.id,
            nzb_guid="guid-replacement-release",
            nzb_title="Blade.Runner.2049.2017.1080p.BluRay.x264-SPARKS",
            resolution="1080p",
            size_bytes=9_000_000_000,
            downloader_transfer_id="tb-new-healthy",
            dispatched_at=datetime.now(UTC),
            status_detail="downloading",
            is_dismissed=False,
            notification_sent=False,
            auto_replaced_count=1,
        )
        session.add(new_hist)
        await session.flush()
        return new_hist

    monkeypatch.setattr(
        transfer_poller_module,
        "_delete_and_purge_transfer",
        fake_delete_purge,
    )
    monkeypatch.setattr(
        transfer_poller_module,
        "_auto_replace_failed_push",
        fake_auto_replace,
    )

    async with factory() as session:
        await _run_transfer_poller_tick_with_session(
            session,
            pre_fetched_downloads=[
                {
                    "id": "tb-old-failed",
                    "download_state": "failed",
                    "error": "Download failed: missing articles",
                }
            ],
        )

    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(DownloadHistory)
                    .where(DownloadHistory.media_item_id == movie_id)
                    .order_by(DownloadHistory.id.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        old_row = next(r for r in rows if r.id == old_hist_id)
        new_row = next(r for r in rows if r.id != old_hist_id)
        assert old_row.status_detail == "replaced"
        assert old_row.is_dismissed is True
        assert old_row.notification_sent is True
        assert new_row.status_detail == "downloading"
        assert new_row.is_dismissed is False


@pytest.mark.asyncio
async def test_cancel_active_push_transitions_to_canceled_and_preserves_row(
    ledger_session_factory: tuple[None, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance Criterion 2: Canceled transfers remain in DownloadHistory with status_detail='canceled'."""
    _, factory = ledger_session_factory

    async with factory() as session:
        movie = MediaItem(
            provider_id=1,
            simkl_id=9002,
            title="The Matrix",
            media_type=MediaType.MOVIE,
            status=MediaStatus.DOWNLOADING,
            year=1999,
        )
        session.add(movie)
        await session.flush()

        hist = DownloadHistory(
            media_item_id=movie.id,
            nzb_guid="guid-matrix-1080p",
            nzb_title="The.Matrix.1999.1080p.BluRay.x264",
            resolution="1080p",
            size_bytes=7_500_000_000,
            downloader_transfer_id="tb-matrix-cancel",
            dispatched_at=datetime.now(UTC),
            status_detail="downloading",
            progress_pct=45.5,
            download_speed_bytes=12_500_000,
            eta_seconds=180,
            is_dismissed=False,
            notification_sent=False,
        )
        session.add(hist)
        await session.commit()
        hist_id = hist.id
        movie_id = movie.id

    deleted_tb_ids: list[str] = []

    async def fake_delete_purge(
        session: AsyncSession,
        tb_id: str | None,
        tb_item: dict[str, Any] | None = None,
        downloader_type: str | None = "torbox",
    ) -> None:
        if tb_id:
            deleted_tb_ids.append(tb_id)

    monkeypatch.setattr(
        transfer_poller_module,
        "_delete_and_purge_transfer",
        fake_delete_purge,
    )

    async with factory() as session:
        ok = await cancel_active_push(session, hist_id)
        assert ok is True

    assert deleted_tb_ids == ["tb-matrix-cancel"]

    async with factory() as session:
        preserved = await session.get(DownloadHistory, hist_id)
        assert preserved is not None
        assert preserved.status_detail == "canceled"
        assert preserved.is_dismissed is True
        assert preserved.notification_sent is True
        assert preserved.progress_pct == 0.0
        assert preserved.download_speed_bytes == 0
        assert preserved.eta_seconds is None

        movie_after = await session.get(MediaItem, movie_id)
        assert movie_after is not None
        assert movie_after.status == MediaStatus.SEARCHING


@pytest.mark.asyncio
async def test_passive_torbox_deletion_reconciliation_preserves_completed_media_status(
    ledger_session_factory: tuple[None, async_sessionmaker[AsyncSession]],
) -> None:
    """Acceptance Criterion 3: Passive external TorBox deletion sets status_detail='deleted' while keeping MediaItem/Season/Episode COMPLETED."""
    _, factory = ledger_session_factory

    async with factory() as session:
        show = MediaItem(
            provider_id=1,
            simkl_id=9003,
            title="Severance",
            media_type=MediaType.SHOW,
            status=MediaStatus.COMPLETED,
            year=2022,
        )
        session.add(show)
        await session.flush()

        s1 = Season(
            media_item_id=show.id,
            season_number=1,
            status=SeasonStatus.COMPLETED,
            episode_count=9,
        )
        session.add(s1)
        await session.flush()

        ep1 = Episode(
            season_id=s1.id,
            episode_number=1,
            status=EpisodeStatus.COMPLETED,
        )
        session.add(ep1)
        await session.flush()

        hist_missing = DownloadHistory(
            media_item_id=show.id,
            season_id=s1.id,
            nzb_guid="guid-sev-s01",
            nzb_title="Severance.S01.1080p.ATVP.WEB-DL.DDP5.1.H.264",
            resolution="1080p",
            downloader_transfer_id="tb-sev-missing",
            status_detail="completed",
            is_dismissed=False,
            notification_sent=True,
        )
        hist_still_live = DownloadHistory(
            media_item_id=show.id,
            episode_id=ep1.id,
            nzb_guid="guid-sev-s01e01",
            nzb_title="Severance.S01E01.1080p.WEB-DL",
            resolution="1080p",
            downloader_transfer_id="tb-sev-live",
            status_detail="completed",
            is_dismissed=False,
            notification_sent=True,
        )
        session.add_all([hist_missing, hist_still_live])
        await session.commit()
        missing_id = hist_missing.id
        live_id = hist_still_live.id
        show_id = show.id
        s1_id = s1.id
        ep1_id = ep1.id

    async with factory() as session:
        updated_count = await reconcile_completed_history_with_downloader(
            session, {"tb-sev-live"}
        )
        assert updated_count == 1

    async with factory() as session:
        h_missing = await session.get(DownloadHistory, missing_id)
        h_live = await session.get(DownloadHistory, live_id)
        assert h_missing is not None
        assert h_missing.status_detail == "deleted"
        assert h_live is not None
        assert h_live.status_detail == "completed"

        # Parent entities MUST remain COMPLETED (never re-pushed automatically)
        show_row = await session.get(MediaItem, show_id)
        s1_row = await session.get(Season, s1_id)
        ep1_row = await session.get(Episode, ep1_id)
        assert show_row is not None and show_row.status == MediaStatus.COMPLETED
        assert s1_row is not None and s1_row.status == SeasonStatus.COMPLETED
        assert ep1_row is not None and ep1_row.status == EpisodeStatus.COMPLETED


@pytest.mark.asyncio
async def test_manual_delete_transfer_resets_targets_and_moves_zero_download_item_to_ready_to_push(
    ledger_session_factory: tuple[None, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance Criteria 4 & 5: Manual Delete from TorBox deletes on TorBox, marks history 'deleted', resets Season/Episode/Movie to SEARCHING, and recalculates parent MediaItem back to Section 2 (SEARCHING) when 0 downloaded entries remain."""
    _, factory = ledger_session_factory

    deleted_tb_ids: list[str] = []

    async def fake_delete_purge(
        session: AsyncSession,
        tb_id: str | None,
        tb_item: dict[str, Any] | None = None,
        downloader_type: str | None = "torbox",
    ) -> None:
        if tb_id:
            deleted_tb_ids.append(tb_id)

    monkeypatch.setattr(
        transfer_poller_module,
        "_delete_and_purge_transfer",
        fake_delete_purge,
    )

    async with factory() as session:
        show = MediaItem(
            provider_id=1,
            simkl_id=9004,
            title="Shogun",
            media_type=MediaType.SHOW,
            status=MediaStatus.COMPLETED,
            year=2024,
        )
        session.add(show)
        await session.flush()

        s1 = Season(
            media_item_id=show.id,
            season_number=1,
            status=SeasonStatus.COMPLETED,
            episode_count=2,
            fail_count=1,
            last_error="old err",
        )
        s2 = Season(
            media_item_id=show.id,
            season_number=2,
            status=SeasonStatus.COMPLETED,
            episode_count=1,
        )
        session.add_all([s1, s2])
        await session.flush()

        ep1 = Episode(
            season_id=s1.id,
            episode_number=1,
            status=EpisodeStatus.COMPLETED,
        )
        ep2 = Episode(
            season_id=s1.id,
            episode_number=2,
            status=EpisodeStatus.COMPLETED,
        )
        ep_s2 = Episode(
            season_id=s2.id,
            episode_number=1,
            status=EpisodeStatus.COMPLETED,
        )
        session.add_all([ep1, ep2, ep_s2])
        await session.flush()

        hist_s1 = DownloadHistory(
            media_item_id=show.id,
            season_id=s1.id,
            nzb_guid="guid-shogun-s01",
            nzb_title="Shogun.2024.S01.2160p.WEB-DL",
            resolution="2160p",
            downloader_transfer_id="tb-shogun-s01",
            status_detail="completed",
            is_dismissed=False,
            notification_sent=True,
        )
        hist_s2 = DownloadHistory(
            media_item_id=show.id,
            season_id=s2.id,
            nzb_guid="guid-shogun-s02",
            nzb_title="Shogun.2024.S02.2160p.WEB-DL",
            resolution="2160p",
            downloader_transfer_id="tb-shogun-s02",
            status_detail="completed",
            is_dismissed=False,
            notification_sent=True,
        )
        session.add_all([hist_s1, hist_s2])
        await session.commit()
        show_id = show.id
        s1_id = s1.id
        s2_id = s2.id
        ep1_id = ep1.id
        ep2_id = ep2.id
        hist_s1_id = hist_s1.id
        hist_s2_id = hist_s2.id

    # Step 1: Delete Season 1 via history_id -> Season 1 & its episodes reset to SEARCHING,
    # but Season 2 is still COMPLETED so show remains COMPLETED (in Downloaded Library).
    async with factory() as session:
        res1 = await manual_delete_transfer(session, history_id=hist_s1_id)
        assert res1["status"] == "ok"

    assert "tb-shogun-s01" in deleted_tb_ids

    async with factory() as session:
        h1 = await session.get(DownloadHistory, hist_s1_id)
        assert h1 is not None
        assert h1.status_detail == "deleted"
        assert h1.is_dismissed is True
        assert h1.notification_sent is True

        s1_row = await session.get(Season, s1_id)
        ep1_row = await session.get(Episode, ep1_id)
        ep2_row = await session.get(Episode, ep2_id)
        show_row = (
            (
                await session.execute(
                    select(MediaItem)
                    .where(MediaItem.id == show_id)
                    .options(selectinload(MediaItem.seasons))
                )
            )
            .scalars()
            .first()
        )
        assert s1_row is not None and s1_row.status == SeasonStatus.SEARCHING
        assert s1_row.fail_count == 0
        assert s1_row.last_error is None
        assert ep1_row is not None and ep1_row.status == EpisodeStatus.SEARCHING
        assert ep2_row is not None and ep2_row.status == EpisodeStatus.SEARCHING
        # Because only 1 of 2 seasons is COMPLETED, show is SEARCHING but stays in Section 1 (in_progress)
        assert show_row is not None and show_row.status == MediaStatus.SEARCHING
        assert show_row.status_tier == "in_progress"

    # Step 2: Delete Season 2 via season_id -> Now 0 seasons/episodes are COMPLETED,
    # so parent show transitions to MediaStatus.SEARCHING (Section 2: Ready to Push)!
    async with factory() as session:
        res2 = await manual_delete_transfer(session, item_id=show_id, season_id=s2_id)
        assert res2["status"] == "ok"

    assert "tb-shogun-s02" in deleted_tb_ids

    async with factory() as session:
        h2 = await session.get(DownloadHistory, hist_s2_id)
        assert h2 is not None and h2.status_detail == "deleted"
        show_row = await session.get(MediaItem, show_id)
        assert show_row is not None and show_row.status == MediaStatus.SEARCHING


@pytest.mark.asyncio
async def test_push_history_ledger_ui_and_push_modal_delete_buttons_and_api_endpoints(
    ledger_session_factory: tuple[None, async_sessionmaker[AsyncSession]],
    async_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance Criteria 6 & 7: Card 4 renders 6-column Push History Ledger with all status badges & actions, Push Modal renders inline [Delete from TorBox] buttons, and API endpoints work end-to-end."""
    _, factory = ledger_session_factory

    deleted_tb_ids: list[str] = []

    async def fake_delete_purge(
        session: AsyncSession,
        tb_id: str | None,
        tb_item: dict[str, Any] | None = None,
        downloader_type: str | None = "torbox",
    ) -> None:
        if tb_id:
            deleted_tb_ids.append(tb_id)

    monkeypatch.setattr(
        transfer_poller_module,
        "_delete_and_purge_transfer",
        fake_delete_purge,
    )

    async with factory() as session:
        movie = MediaItem(
            provider_id=1,
            simkl_id=9005,
            title="Dune: Part Two",
            media_type=MediaType.MOVIE,
            status=MediaStatus.COMPLETED,
            year=2024,
            poster_url="https://image.tmdb.org/t/p/w500/dune2.jpg",
        )
        session.add(movie)
        await session.flush()

        h_completed = DownloadHistory(
            media_item_id=movie.id,
            nzb_guid="guid-dune2-2160p",
            nzb_title="Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-FLUX",
            resolution="2160p",
            grabbed_language="en",
            size_bytes=24_500_000_000,
            downloader_transfer_id="tb-dune2-live",
            dispatched_at=datetime.now(UTC),
            status_detail="completed",
            is_dismissed=False,
            notification_sent=True,
        )
        h_deleted = DownloadHistory(
            media_item_id=movie.id,
            nzb_guid="guid-dune2-deleted",
            nzb_title="Dune.Part.Two.2024.1080p.WEB-DL-OLD",
            resolution="1080p",
            size_bytes=8_500_000_000,
            downloader_transfer_id="tb-dune2-del",
            dispatched_at=datetime.now(UTC),
            status_detail="deleted",
            is_dismissed=True,
            notification_sent=True,
        )
        h_replaced = DownloadHistory(
            media_item_id=movie.id,
            nzb_guid="guid-dune2-replaced",
            nzb_title="Dune.Part.Two.2024.1080p.WEBRip-REPLACED",
            resolution="1080p",
            size_bytes=6_000_000_000,
            downloader_transfer_id="tb-dune2-rep",
            dispatched_at=datetime.now(UTC),
            status_detail="replaced",
            is_dismissed=True,
            notification_sent=True,
        )
        h_failed = DownloadHistory(
            media_item_id=movie.id,
            nzb_guid="guid-dune2-failed",
            nzb_title="Dune.Part.Two.2024.1080p.BluRay-BROKEN",
            resolution="1080p",
            size_bytes=11_000_000_000,
            downloader_transfer_id="tb-dune2-fail",
            dispatched_at=datetime.now(UTC),
            status_detail="failed: Download failed: missing articles",
            is_dismissed=False,
            notification_sent=True,
        )
        h_canceled = DownloadHistory(
            media_item_id=movie.id,
            nzb_guid="guid-dune2-canceled",
            nzb_title="Dune.Part.Two.2024.720p.WEB-DL-CANCELED",
            resolution="720p",
            size_bytes=4_000_000_000,
            downloader_transfer_id="tb-dune2-canc",
            dispatched_at=datetime.now(UTC),
            status_detail="canceled",
            is_dismissed=True,
            notification_sent=True,
        )
        session.add_all([h_completed, h_deleted, h_replaced, h_failed, h_canceled])
        await session.commit()
        movie_id = movie.id
        h_completed_id = h_completed.id
        h_canceled_id = h_canceled.id

    client = async_client
    if True:
        # 1. Verify Dashboard Card 4 (Push History Ledger) 6-column headers & badges
        resp = await client.get("/")
        assert resp.status_code == 200
        html = resp.text
        for col_header in (
            "Title &amp; Target",
            "Type",
            "Release &amp; Quality",
            "Pushed At",
            "Downloader Status",
            "Actions",
        ):
            assert col_header in html

        for badge_label in (
            "READY ON TORBOX",
            "DELETED",
            "REPLACED",
            "FAILED",
            "CANCELED",
        ):
            assert badge_label in html

        assert "Delete from TorBox" in html
        assert "Re-Push" in html
        assert "Delete Entry" in html

        # 2. Verify Push Modal renders inline [Delete from TorBox] for downloaded Movie
        modal_resp = await client.get(f"/api/items/{movie_id}/push-modal")
        assert modal_resp.status_code == 200
        assert "Delete from TorBox" in modal_resp.text
        assert f"/api/items/{movie_id}/delete-transfer" in modal_resp.text

        # 3. Verify POST /api/history/{history_id}/delete-transfer
        del_tb_resp = await client.post(
            f"/api/history/{h_completed_id}/delete-transfer"
        )
        assert del_tb_resp.status_code == 200
        assert "tb-dune2-live" in deleted_tb_ids

        async with factory() as session:
            h_comp_after = await session.get(DownloadHistory, h_completed_id)
            movie_after = await session.get(MediaItem, movie_id)
            assert h_comp_after is not None
            assert h_comp_after.status_detail == "deleted"
            assert movie_after is not None
            assert movie_after.status == MediaStatus.SEARCHING

        # 4. Verify DELETE /api/history/{history_id} removes the ledger entry from DB
        del_entry_resp = await client.delete(f"/api/history/{h_canceled_id}")
        assert del_entry_resp.status_code == 200
        async with factory() as session:
            assert await session.get(DownloadHistory, h_canceled_id) is None


@pytest.mark.asyncio
async def test_push_history_ledger_local_timestamp_formatting(
    ledger_session_factory: tuple[None, async_sessionmaker[AsyncSession]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify get_push_history_ledger converts pushed_dt to server local time for pushed_at_str."""
    import time

    from app.core.transfer_poller import get_push_history_ledger

    # Force TZ to UTC+3 (Etc/GMT-3)
    monkeypatch.setenv("TZ", "Etc/GMT-3")
    time.tzset()

    _, factory = ledger_session_factory
    async with factory() as session:
        movie = MediaItem(
            provider_id=1,
            simkl_id=9876,
            title="TZ Test Movie",
            media_type=MediaType.MOVIE,
            status=MediaStatus.COMPLETED,
            year=2026,
        )
        session.add(movie)
        await session.flush()

        # UTC naive datetime at 08:30:00 -> in GMT-3 (UTC+3) is 11:30:00
        sent_utc = datetime(2026, 10, 10, 8, 30, 0)
        h = DownloadHistory(
            media_item_id=movie.id,
            nzb_guid="guid-tz-ledger-1",
            nzb_title="TZ.Test.Movie.2026.1080p",
            status_detail="completed",
            dispatched_at=sent_utc,
            is_dismissed=True,
        )
        session.add(h)
        await session.commit()

        ledger = await get_push_history_ledger(session)
        assert len(ledger) == 1
        entry = ledger[0]
        assert entry["pushed_at_str"] == "2026-10-10 11:30"
        assert entry["pushed_at_ts"] == pytest.approx(
            sent_utc.replace(tzinfo=UTC).timestamp()
        )
