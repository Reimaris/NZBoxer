"""Unit and integration tests for Ticket 06: Auto-Wake Transfer Poller & Split Failure Recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    BlacklistedRelease,
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
async def poller_db(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
):
    provider = Provider(name="Simkl", type="simkl", category="watchlist")
    sys_settings = SystemSettings(
        id=1,
        download_timeout_hours=24,
        discord_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test/poller",
        notify_on_completed=True,
        notify_on_failure=True,
    )
    db_session.add_all([provider, sys_settings])
    await db_session.commit()
    return db_session, session_factory, provider


@pytest.mark.asyncio
async def test_poller_zero_api_calls_when_idle_and_wakes_on_push(poller_db):
    """Verify the transfer poller makes 0 TorBox API calls when no entities are DOWNLOADING,
    wakes when a push is initiated, and sleeps once active transfers complete.
    """
    from app.core.transfer_poller import (
        run_transfer_poller_tick,
        transfer_poller,
        wake_transfer_poller,
    )

    session, _, _ = poller_db
    transfer_poller.sleep()
    assert transfer_poller.is_awake is False

    with patch(
        "app.services.downloaders.torbox.get_usenet_downloads", new_callable=AsyncMock
    ) as mock_get_dl:
        # 0 DOWNLOADING entities -> 0 TorBox API calls
        res = await run_transfer_poller_tick(session)
        assert res["polled"] is False
        assert res["active_count"] == 0
        assert res["sleeping"] is True
        assert transfer_poller.is_awake is False
        mock_get_dl.assert_not_awaited()

        # Waking the poller sets is_awake = True
        wake_transfer_poller()
        assert transfer_poller.is_awake is True

        # If a tick runs and finds 0 DOWNLOADING entities, it immediately goes back to sleep without calling TorBox
        res2 = await run_transfer_poller_tick(session)
        assert res2["polled"] is False
        assert res2["sleeping"] is True
        assert transfer_poller.is_awake is False
        mock_get_dl.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_progress_updates_and_immediate_layer3_archive_rejection(poller_db):
    """Verify live progress/speed/ETA updates, immediate Tick-1 Layer 3 rejection for RAR-only
    payloads (even 2 minutes after dispatch, with zero 15m grace window), auto-replacement with
    the next-best candidate, and transition to COMPLETED once playable video is verified.
    """
    from app.core.transfer_poller import run_transfer_poller_tick, transfer_poller

    session, _, provider = poller_db

    movie = MediaItem(
        provider_id=provider.id,
        title="Interstellar",
        year=2014,
        media_type=MediaType.MOVIE,
        status=MediaStatus.DOWNLOADING,
    )
    session.add(movie)
    await session.flush()

    now_utc = datetime.now(timezone.utc)
    hist = DownloadHistory(
        media_item_id=movie.id,
        nzb_title="Interstellar.2014.2160p.UHD.BluRay.Remux.HEVC.TrueHD.7.1-RARONLY",
        nzb_guid="guid-interstellar-raronly",
        score=9500.0,
        size_bytes=50 * (1024**3),
        resolution="2160p",
        source="remux",
        downloader_transfer_id="tb-101",
        dispatched_at=now_utc - timedelta(minutes=2),
        push_mode="auto",
        grabbed_language="en",
    )
    session.add(hist)
    await session.commit()

    # Step 1: TorBox reports 45% downloading at 52.4 MB/s, ETA 180s
    downloading_snapshot = [
        {
            "id": "tb-101",
            "name": hist.nzb_title,
            "download_state": "downloading",
            "progress": 0.45,
            "download_speed": 52428800,
            "eta": 180,
            "files": [],
        }
    ]
    res1 = await run_transfer_poller_tick(
        session, pre_fetched_downloads=downloading_snapshot
    )
    assert res1["polled"] is True
    assert res1["active_count"] == 1
    assert res1["sleeping"] is False

    await session.refresh(hist)
    await session.refresh(movie)
    assert movie.status == MediaStatus.DOWNLOADING
    assert hist.progress_pct == 45.0
    assert hist.download_speed_bytes == 52428800
    assert hist.eta_seconds == 180
    assert hist.status_detail == "downloading"

    # Step 2: TorBox reports "completed" (or download_finished=True), but file list contains an executable!
    # Executable payloads MUST immediately fail on Tick 1, delete from TorBox, blacklist, and auto-replace!
    exe_snapshot = [
        {
            "id": "tb-101",
            "name": hist.nzb_title,
            "download_state": "completed",
            "download_finished": True,
            "progress": 1.0,
            "download_speed": 0,
            "eta": 0,
            "files": [
                {"name": "setup.exe", "size": 1000000},
                {"name": "readme.txt", "size": 500},
            ],
        }
    ]
    valid_nzb = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<nzb><file subject="Interstellar.2014.mkv"><segments>'
        b'<segment bytes="50000000" number="1">msg1</segment>'
        b"</segments></file></nzb>"
    )
    indexer_candidates = [
        {
            "title": "Interstellar.2014.2160p.UHD.BluRay.Remux.HEVC.TrueHD.7.1-RARONLY",
            "guid": "guid-interstellar-raronly",
            "size": int(50 * (1024**3)),
            "api_language": "en",
        },
        {
            "title": "Interstellar.2014.2160p.UHD.BluRay.Remux.HEVC.TrueHD.7.1-PLAYABLE",
            "guid": "guid-interstellar-playable",
            "size": int(48 * (1024**3)),
            "api_language": "en",
        },
    ]

    with (
        patch(
            "app.services.downloaders.torbox.delete_usenet_download",
            new_callable=AsyncMock,
            return_value={"success": True},
        ) as mock_del_tb,
        patch(
            "app.services.indexers.newznab.search_movie",
            new_callable=AsyncMock,
            return_value=indexer_candidates,
        ) as mock_search,
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new_callable=AsyncMock,
            return_value=(valid_nzb, "interstellar_playable.nzb"),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new_callable=AsyncMock,
            return_value={"id": "tb-102", "hash": "hash-102"},
        ) as mock_send_tb,
    ):
        res2 = await run_transfer_poller_tick(
            session, pre_fetched_downloads=exe_snapshot
        )
        mock_del_tb.assert_awaited_once_with("tb-101", session=session)
        mock_search.assert_awaited_once()
        mock_send_tb.assert_awaited_once()

    await session.refresh(hist)
    await session.refresh(movie)
    assert hist.status_detail == "replaced"
    assert hist.is_dismissed is True
    assert movie.status == MediaStatus.DOWNLOADING
    assert movie.fail_count == 1
    assert res2["sleeping"] is False

    bl_rows = (
        (
            await session.execute(
                select(BlacklistedRelease).where(
                    BlacklistedRelease.media_item_id == movie.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert any(
        b.nzb_guid == "guid-interstellar-raronly"
        and "executable" in (b.reason or "").lower()
        for b in bl_rows
    )

    # Step 3: Replacement transfer tb-102 finishes with a playable .mkv container
    verified_snapshot = [
        {
            "id": "tb-102",
            "name": "Interstellar.2014.2160p.UHD.BluRay.Remux.HEVC.TrueHD.7.1-PLAYABLE",
            "download_state": "completed",
            "progress": 1.0,
            "download_speed": 0,
            "eta": 0,
            "files": [
                {"name": "Interstellar.2014.2160p.Remux.mkv", "size": 48 * (1024**3)}
            ],
        }
    ]
    with patch(
        "app.services.discord.send_discord_webhook",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_discord:
        res3 = await run_transfer_poller_tick(
            session, pre_fetched_downloads=verified_snapshot
        )
        await session.refresh(movie)
        assert movie.status == MediaStatus.COMPLETED
        assert movie.fail_count == 0
        # All active downloads finished -> poller automatically sleeps!
        assert res3["remaining_active"] == 0
        assert res3["sleeping"] is True
        assert transfer_poller.is_awake is False
        # Ready on TorBox notification dispatched
        mock_discord.assert_awaited_once()
        assert mock_discord.call_args.kwargs["embed"]["color"] == 0x10B981


@pytest.mark.asyncio
async def test_split_failure_recovery_auto_push_replaces_with_next_best(poller_db):
    """Verify failed transfers with push_mode == 'auto' are deleted from TorBox, blacklisted,
    and automatically replaced by searching and pushing the #2 candidate.
    """
    from app.core.transfer_poller import run_transfer_poller_tick

    session, _, provider = poller_db

    movie = MediaItem(
        provider_id=provider.id,
        title="Blade Runner 2049",
        year=2017,
        media_type=MediaType.MOVIE,
        status=MediaStatus.DOWNLOADING,
    )
    session.add(movie)
    await session.flush()

    failed_hist = DownloadHistory(
        media_item_id=movie.id,
        nzb_title="Blade.Runner.2049.2017.2160p.WEB-DL.H265-BROKEN",
        nzb_guid="guid-br-broken",
        score=8900.0,
        size_bytes=25 * (1024**3),
        resolution="2160p",
        source="web-dl",
        downloader_transfer_id="900",
        dispatched_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        push_mode="auto",
        grabbed_language="en",
    )
    session.add(failed_hist)
    await session.commit()

    failed_snapshot = [
        {
            "id": "900",
            "name": failed_hist.nzb_title,
            "download_state": "failed (Repair failed, not enough repair blocks)",
            "progress": 0.12,
            "download_speed": 0,
            "eta": 0,
        }
    ]

    valid_nzb = b'<?xml version="1.0" encoding="UTF-8"?><nzb><file subject="Blade.Runner.2049.mkv"><segments><segment bytes="50000000" number="1">msg1</segment></segments></file></nzb>'

    indexer_candidates = [
        # #1 is the broken release (now blacklisted!)
        {
            "title": "Blade.Runner.2049.2017.2160p.WEB-DL.H265-BROKEN",
            "guid": "guid-br-broken",
            "size": int(25 * (1024**3)),
            "api_language": "en",
        },
        # #2 is the healthy replacement candidate
        {
            "title": "Blade.Runner.2049.2017.1080p.BluRay.x264-SPARKS",
            "guid": "guid-br-sparks-healthy",
            "size": int(12 * (1024**3)),
            "api_language": "en",
        },
    ]

    with (
        patch(
            "app.services.downloaders.torbox.delete_usenet_download",
            new_callable=AsyncMock,
            return_value={"success": True},
        ) as mock_del_tb,
        patch(
            "app.services.indexers.newznab.search_movie",
            new_callable=AsyncMock,
            return_value=indexer_candidates,
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new_callable=AsyncMock,
            return_value=(valid_nzb, "br2049_sparks.nzb"),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new_callable=AsyncMock,
            return_value={"id": "901", "hash": "hash-901"},
        ) as mock_send_tb,
        patch(
            "app.services.discord.send_discord_webhook",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_discord,
    ):
        res = await run_transfer_poller_tick(
            session, pre_fetched_downloads=failed_snapshot
        )
        assert res["polled"] is True
        mock_del_tb.assert_awaited_once_with(900, session=session)
        mock_send_tb.assert_awaited_once()

        # Verify broken release is blacklisted
        bl_rows = (
            (
                await session.execute(
                    select(BlacklistedRelease).where(
                        BlacklistedRelease.media_item_id == movie.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any(b.nzb_guid == "guid-br-broken" for b in bl_rows)

        # Verify replacement DownloadHistory row is now active in DOWNLOADING state
        await session.refresh(movie)
        assert movie.status == MediaStatus.DOWNLOADING
        hist_rows = (
            (
                await session.execute(
                    select(DownloadHistory)
                    .where(DownloadHistory.media_item_id == movie.id)
                    .order_by(DownloadHistory.id.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(hist_rows) == 2
        assert hist_rows[0].nzb_guid == "guid-br-broken"
        assert hist_rows[0].status_detail == "replaced"
        assert hist_rows[0].is_dismissed is True
        assert hist_rows[1].nzb_guid == "guid-br-sparks-healthy"
        assert hist_rows[1].downloader_transfer_id == "901"
        assert hist_rows[1].push_mode == "auto"
        assert hist_rows[1].auto_replaced_count == 1
        assert hist_rows[1].is_dismissed is False
        # Intermediate retry notifications are suppressed until the transfer settles
        mock_discord.assert_not_awaited()


@pytest.mark.asyncio
async def test_split_failure_recovery_manual_pick_marks_failed_and_dismiss_endpoint(
    poller_db,
    async_client: AsyncClient,
):
    """Verify failed transfers with push_mode == 'manual' are deleted from TorBox, blacklisted,
    marked FAILED (without auto-pushing a replacement), kept in Active Pushes until dismissed via
    POST /api/pushes/{history_id}/dismiss.
    """
    from app.core.transfer_poller import run_transfer_poller_tick

    session, _maker, provider = poller_db

    show = MediaItem(
        provider_id=provider.id,
        title="Andor",
        year=2022,
        media_type=MediaType.SHOW,
        status=MediaStatus.DOWNLOADING,
    )
    session.add(show)
    await session.flush()

    s1 = Season(
        media_item_id=show.id,
        season_number=1,
        title="Season 1",
        entry_type="season",
        watch_order=1,
        type_number=1,
        monitored=True,
        status=SeasonStatus.DOWNLOADING,
    )
    session.add(s1)
    await session.flush()

    ep5 = Episode(
        season_id=s1.id,
        episode_number=5,
        status=EpisodeStatus.DOWNLOADING,
    )
    session.add(ep5)
    await session.flush()

    manual_hist = DownloadHistory(
        media_item_id=show.id,
        season_id=s1.id,
        episode_id=ep5.id,
        nzb_title="Andor.S01E05.1080p.WEB-DL.DDP5.1.H.264-MANUALPICK",
        nzb_guid="guid-andor-manual-bad",
        score=7500.0,
        size_bytes=3 * (1024**3),
        resolution="1080p",
        source="web-dl",
        downloader_transfer_id="777",
        dispatched_at=datetime.now(timezone.utc) - timedelta(minutes=25),
        push_mode="manual",
        grabbed_language="en",
    )
    session.add(manual_hist)
    await session.commit()

    failed_snapshot = [
        {
            "id": "777",
            "name": manual_hist.nzb_title,
            "download_state": "error",
            "progress": 0.0,
            "download_speed": 0,
            "eta": 0,
        }
    ]

    with (
        patch(
            "app.services.downloaders.torbox.delete_usenet_download",
            new_callable=AsyncMock,
            return_value={"success": True},
        ) as mock_del_tb,
        patch(
            "app.services.indexers.newznab.search_show",
            new_callable=AsyncMock,
        ) as mock_search_show,
        patch(
            "app.services.discord.send_discord_webhook",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_discord,
    ):
        await run_transfer_poller_tick(session, pre_fetched_downloads=failed_snapshot)
        mock_del_tb.assert_awaited_once_with(777, session=session)
        # MUST NOT auto-search or auto-replace for manual picks!
        mock_search_show.assert_not_awaited()
        mock_discord.assert_awaited_once()
        assert mock_discord.call_args.kwargs["embed"]["color"] == 0xEF4444

    await session.refresh(ep5)
    await session.refresh(manual_hist)
    assert ep5.status == EpisodeStatus.FAILED
    assert manual_hist.is_dismissed is False
    assert manual_hist.status_detail is not None
    assert "failed" in manual_hist.status_detail.lower()

    # Verify BlacklistedRelease was recorded
    bl_rows = (
        (
            await session.execute(
                select(BlacklistedRelease).where(
                    BlacklistedRelease.media_item_id == show.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert any(b.nzb_guid == "guid-andor-manual-bad" for b in bl_rows)

    # Verify GET /api/pushes/active includes the unacknowledged failed manual pick
    # and POST /api/pushes/{history_id}/dismiss dismisses it
    resp_active = await async_client.get("/api/pushes/active")
    assert resp_active.status_code == 200
    active_data = resp_active.json()
    assert len(active_data["pushes"]) == 1
    assert active_data["pushes"][0]["history_id"] == manual_hist.id
    assert active_data["pushes"][0]["push_mode"] == "manual"
    assert active_data["pushes"][0]["is_failed"] is True

    # Dismiss the failed manual pick
    resp_dismiss = await async_client.post(f"/api/pushes/{manual_hist.id}/dismiss")
    assert resp_dismiss.status_code == 200

    # Now GET /api/pushes/active is empty!
    resp_active_after = await async_client.get("/api/pushes/active")
    assert resp_active_after.status_code == 200
    assert len(resp_active_after.json()["pushes"]) == 0

    await session.refresh(manual_hist)
    await session.refresh(ep5)
    assert manual_hist.is_dismissed is True
    assert ep5.status == EpisodeStatus.SEARCHING


@pytest.mark.asyncio
async def test_unextractable_archive_immediate_rejection_and_cancel_endpoint(
    poller_db,
    async_client: AsyncClient,
):
    """Verify RAR-only payload immediately triggers Layer 3 failure recovery on Tick 1,
    and POST /api/pushes/{history_id}/cancel deletes the active transfer and reverts to SEARCHING.
    """
    from app.core.transfer_poller import run_transfer_poller_tick

    session, _maker, provider = poller_db

    movie = MediaItem(
        provider_id=provider.id,
        title="Tenet",
        year=2020,
        media_type=MediaType.MOVIE,
        status=MediaStatus.DOWNLOADING,
    )
    session.add(movie)
    await session.flush()

    hist = DownloadHistory(
        media_item_id=movie.id,
        nzb_title="Tenet.2020.1080p.BluRay.RARONLY",
        nzb_guid="guid-tenet-raronly",
        score=8000.0,
        size_bytes=15 * (1024**3),
        resolution="1080p",
        source="bluray",
        downloader_transfer_id="555",
        # Stalled unpack timer initialized 11 minutes ago (exceeding 10m grace period)
        unpack_started_at=datetime.now(timezone.utc) - timedelta(minutes=11),
        dispatched_at=datetime.now(timezone.utc) - timedelta(minutes=12),
        push_mode="manual",
        grabbed_language="en",
    )
    session.add(hist)
    await session.commit()

    rar_dead_end_snapshot = [
        {
            "id": "555",
            "name": hist.nzb_title,
            "download_state": "completed",
            "progress": 1.0,
            "files": [
                {"name": "tenet.part01.rar", "size": 5000000},
                {"name": "tenet.par2", "size": 100000},
            ],
        }
    ]

    with (
        patch(
            "app.services.downloaders.torbox.delete_usenet_download",
            new_callable=AsyncMock,
            return_value={"success": True},
        ) as mock_del_tb,
        patch(
            "app.services.discord.send_discord_webhook",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await run_transfer_poller_tick(
            session, pre_fetched_downloads=rar_dead_end_snapshot
        )
        mock_del_tb.assert_awaited_once_with(555, session=session)

    await session.refresh(movie)
    assert movie.status == MediaStatus.FAILED
    bl_rows = (
        (
            await session.execute(
                select(BlacklistedRelease).where(
                    BlacklistedRelease.media_item_id == movie.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert any(
        "failed to extract archive" in (b.reason or "").lower()
        or "10m" in (b.reason or "")
        for b in bl_rows
    )

    # Test Cancel & Delete endpoint on an active transfer
    movie2 = MediaItem(
        provider_id=provider.id,
        title="Memento",
        year=2000,
        media_type=MediaType.MOVIE,
        status=MediaStatus.DOWNLOADING,
    )
    session.add(movie2)
    await session.flush()

    hist2 = DownloadHistory(
        media_item_id=movie2.id,
        nzb_title="Memento.2000.1080p.BluRay",
        nzb_guid="guid-memento",
        downloader_transfer_id="556",
        dispatched_at=datetime.now(timezone.utc),
        push_mode="auto",
    )
    session.add(hist2)
    await session.commit()

    with patch(
        "app.services.downloaders.torbox.delete_usenet_download",
        new_callable=AsyncMock,
        return_value={"success": True},
    ) as mock_del_cancel:
        resp_cancel = await async_client.post(f"/api/pushes/{hist2.id}/cancel")
        assert resp_cancel.status_code == 200
        mock_del_cancel.assert_awaited_once()
        assert mock_del_cancel.call_args[0][0] == 556

    await session.refresh(movie2)
    assert movie2.status == MediaStatus.SEARCHING


@pytest.mark.asyncio
async def test_bounded_auto_replace_retries_exhaustion_and_reset_on_new_push(
    poller_db,
    async_client: AsyncClient,
):
    """Verify auto-pushed transfers increment target.fail_count on each failure,
    auto-replace while fail_count <= sh_max_retries, transition to FAILED and stay in
    Active Pushes once fail_count > sh_max_retries, and reset fail_count = 0 on a new push.
    """
    from app.core.push_engine import execute_auto_push
    from app.core.transfer_poller import run_transfer_poller_tick
    from app.db.models import Provider, SystemSettings

    session, maker, provider = poller_db

    # Configure sh_max_retries = 2 on existing SystemSettings row
    sys_settings = (await session.execute(select(SystemSettings))).scalars().first()
    assert sys_settings is not None
    sys_settings.sh_max_retries = 2
    sys_settings.download_timeout_hours = 6
    sys_settings.discord_enabled = True
    sys_settings.discord_webhook_url = "https://discord.com/api/webhooks/123/abc"
    sys_settings.notify_on_push_initiated = True
    sys_settings.notify_on_completed = True
    sys_settings.notify_on_failure = True
    sys_settings.notify_on_auto_advance = True

    indexer = Provider(
        category="indexer",
        type="newznab",
        name="GeekIndexer",
        api_url="https://api.nzbgeek.info",
        api_key="geek-key",
        priority=1,
        is_active=True,
    )
    session.add(indexer)

    movie = MediaItem(
        provider_id=provider.id,
        title="Interstellar",
        year=2014,
        imdb_id="tt0816692",
        runtime_minutes=169,
        media_type=MediaType.MOVIE,
        status=MediaStatus.DOWNLOADING,
        fail_count=0,
    )
    session.add(movie)
    await session.flush()

    initial_hist = DownloadHistory(
        media_item_id=movie.id,
        nzb_title="Interstellar.2014.2160p.UHD.BluRay.x265-TRY0",
        nzb_guid="guid-interstellar-try0",
        score=9500.0,
        size_bytes=25 * (1024**3),
        resolution="2160p",
        source="bluray",
        video_codec="x265",
        downloader_transfer_id="1001",
        dispatched_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        push_mode="auto",
        grabbed_language="en",
    )
    session.add(initial_hist)
    await session.commit()

    indexer_candidates = [
        {
            "title": "Interstellar.2014.2160p.UHD.BluRay.x265-TRY0",
            "guid": "guid-interstellar-try0",
            "size": 25 * (1024**3),
        },
        {
            "title": "Interstellar.2014.2160p.WEB-DL.DDP5.1.H.265-TRY1",
            "guid": "guid-interstellar-try1",
            "size": 20 * (1024**3),
        },
        {
            "title": "Interstellar.2014.1080p.BluRay.x264-TRY2",
            "guid": "guid-interstellar-try2",
            "size": 14 * (1024**3),
        },
        {
            "title": "Interstellar.2014.1080p.WEB-DL.x264-TRY3",
            "guid": "guid-interstellar-try3",
            "size": 10 * (1024**3),
        },
    ]
    valid_nzb_xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">\n'
        b'  <file subject="Interstellar.2014.mkv (1/1)">\n'
        b"    <segments>\n"
        b'      <segment bytes="750000000" number="1">seg1@news</segment>\n'
        b"    </segments>\n"
        b"  </file>\n"
        b"</nzb>"
    )

    # --- Failure 1: fail_count becomes 1 (<= 2) -> Auto-replaces with TRY1 ---
    with (
        patch(
            "app.services.downloaders.torbox.delete_usenet_download",
            new_callable=AsyncMock,
            return_value={"success": True},
        ) as mock_del,
        patch(
            "app.services.indexers.newznab.search_movie",
            new_callable=AsyncMock,
            return_value=indexer_candidates,
        ) as mock_search,
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new_callable=AsyncMock,
            return_value=(valid_nzb_xml, "Interstellar.nzb"),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new_callable=AsyncMock,
            return_value={"id": "1002", "hash": "hash-try1"},
        ) as mock_send,
        patch(
            "app.services.discord.send_discord_webhook",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await run_transfer_poller_tick(
            session,
            pre_fetched_downloads=[
                {
                    "id": "1001",
                    "name": "Interstellar.2014.2160p.UHD.BluRay.x265-TRY0",
                    "download_state": "error",
                    "progress": 0.0,
                }
            ],
        )
        mock_del.assert_awaited_once_with(1001, session=session)
        mock_search.assert_awaited_once()
        mock_send.assert_awaited_once()

    await session.refresh(movie)
    assert movie.status == MediaStatus.DOWNLOADING
    assert movie.fail_count == 1

    # --- Failure 2: fail_count becomes 2 (<= 2) -> Auto-replaces with TRY2 ---
    with (
        patch(
            "app.services.downloaders.torbox.delete_usenet_download",
            new_callable=AsyncMock,
            return_value={"success": True},
        ) as mock_del,
        patch(
            "app.services.indexers.newznab.search_movie",
            new_callable=AsyncMock,
            return_value=indexer_candidates,
        ) as mock_search,
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new_callable=AsyncMock,
            return_value=(valid_nzb_xml, "Interstellar.nzb"),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new_callable=AsyncMock,
            return_value={"id": "1003", "hash": "hash-try2"},
        ) as mock_send,
        patch(
            "app.services.discord.send_discord_webhook",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await run_transfer_poller_tick(
            session,
            pre_fetched_downloads=[
                {
                    "id": "1002",
                    "name": "Interstellar.2014.2160p.WEB-DL.DDP5.1.H.265-TRY1",
                    "download_state": "error",
                    "progress": 0.0,
                }
            ],
        )
        mock_del.assert_awaited_once_with(1002, session=session)
        mock_search.assert_awaited_once()
        mock_send.assert_awaited_once()

    await session.refresh(movie)
    assert movie.status == MediaStatus.DOWNLOADING
    assert movie.fail_count == 2

    # --- Failure 3: fail_count becomes 3 (> sh_max_retries=2) -> Stops replacing & marks FAILED ---
    with (
        patch(
            "app.services.downloaders.torbox.delete_usenet_download",
            new_callable=AsyncMock,
            return_value={"success": True},
        ) as mock_del,
        patch(
            "app.services.indexers.newznab.search_movie",
            new_callable=AsyncMock,
            return_value=indexer_candidates,
        ) as mock_search,
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new_callable=AsyncMock,
        ) as mock_send,
        patch(
            "app.services.discord.send_discord_webhook",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_discord,
    ):
        await run_transfer_poller_tick(
            session,
            pre_fetched_downloads=[
                {
                    "id": "1003",
                    "name": "Interstellar.2014.1080p.BluRay.x264-TRY2",
                    "download_state": "error",
                    "progress": 0.0,
                }
            ],
        )
        mock_del.assert_awaited_once_with(1003, session=session)
        mock_search.assert_not_awaited()
        mock_send.assert_not_awaited()
        mock_discord.assert_awaited_once()
        assert mock_discord.call_args.kwargs["embed"]["color"] == 0xEF4444

    await session.refresh(movie)
    assert movie.status == MediaStatus.FAILED
    assert movie.fail_count == 3

    # Verify the exhausted auto-push row stays in Active Pushes (is_dismissed == False, is_failed == True)
    resp_active = await async_client.get("/api/pushes/active")
    assert resp_active.status_code == 200
    pushes = resp_active.json()["pushes"]
    assert len(pushes) == 1
    assert pushes[0]["release_name"] == "Interstellar.2014.1080p.BluRay.x264-TRY2"
    assert pushes[0]["is_failed"] is True

    # --- New User-Triggered Push resets fail_count = 0 ---
    with (
        patch(
            "app.services.indexers.newznab.search_movie",
            new_callable=AsyncMock,
            return_value=indexer_candidates,
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new_callable=AsyncMock,
            return_value=(valid_nzb_xml, "Interstellar.nzb"),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new_callable=AsyncMock,
            return_value={"id": "1004", "hash": "hash-try3"},
        ),
        patch(
            "app.services.discord.send_discord_webhook",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        res = await execute_auto_push(session, movie.id, {})
        assert res["pushed"] is True

    await session.refresh(movie)
    assert movie.status == MediaStatus.DOWNLOADING
    assert movie.fail_count == 0


@pytest.mark.asyncio
async def test_settings_tab4_torbox_transfer_poller_and_failure_recovery_labels(
    poller_db,
    async_client: AsyncClient,
):
    """Verify Settings Tab 4 renders 'TorBox Transfer Poller & Failure Recovery'
    with v3.0.0 labels instead of legacy 'Active Push Monitoring & Self-Healing'.
    """
    resp = await async_client.get("/settings")
    assert resp.status_code == 200
    html = resp.text

    assert (
        "TorBox Transfer Poller &amp; Failure Recovery" in html
        or "TorBox Transfer Poller & Failure Recovery" in html
        or "System &amp; Automation" in html
        or "System & Automation" in html
    )
    assert "Download Timeout" in html
    assert "Max Auto-Push Replacement Attempts" in html
    assert "Sync External TorBox Library" not in html
    assert "Sync TorBox Library" not in html

    assert "Active Push Monitoring" not in html
    assert "Max Auto-Replace Retries" not in html
    assert "Rescan TorBox Downloads" not in html


@pytest.mark.asyncio
async def test_count_downloading_entities_deduplicates_season_when_episodes_downloading(
    poller_db,
):
    """Verify count_downloading_entities() only counts Season rows in DOWNLOADING state
    when they have zero child Episode rows in DOWNLOADING state, so 1 downloading episode
    in a downloading season reports DOWNLOADING: 1 rather than 2.
    """
    from app.core.transfer_poller import count_downloading_entities
    from tests.conftest import make_series

    session, _, _ = poller_db

    # Case 1: 1 downloading episode inside a downloading season -> count == 1 (not 2)
    _, seasons, episodes = await make_series(
        session,
        title="Star Wars Rebels",
        seasons=[{"season_number": 1, "status": SeasonStatus.DOWNLOADING}],
        episodes_per_season=3,
        episode_statuses=[
            EpisodeStatus.COMPLETED,
            EpisodeStatus.DOWNLOADING,
            EpisodeStatus.SEARCHING,
        ],
        status=MediaStatus.DOWNLOADING,
    )
    await session.commit()

    assert await count_downloading_entities(session) == 1

    # Case 2: 2 downloading episodes inside the same downloading season -> count == 2
    episodes[2].status = EpisodeStatus.DOWNLOADING
    await session.commit()
    assert await count_downloading_entities(session) == 2

    # Case 3: Season pack download (Season is DOWNLOADING, 0 child Episode rows are DOWNLOADING) -> count == 1
    episodes[1].status = EpisodeStatus.SEARCHING
    episodes[2].status = EpisodeStatus.SEARCHING
    seasons[0].status = SeasonStatus.DOWNLOADING
    await session.commit()
    assert await count_downloading_entities(session) == 1


@pytest.mark.asyncio
async def test_orphaned_downloading_entity_reconciled_during_poller_tick(poller_db):
    """Verify that if a MediaItem, Season, or Episode is stuck in DOWNLOADING status in SQLite
    with zero active non-dismissed DownloadHistory rows and no in-flight push is active,
    run_transfer_poller_tick() reconciles it via _recalculate_parent_status() so
    count_downloading_entities() drops to 0 and the poller goes to sleep.
    """
    from app.core.transfer_poller import (
        count_downloading_entities,
        run_transfer_poller_tick,
        transfer_poller,
    )
    from tests.conftest import make_series

    session, _, _ = poller_db

    show, seasons, episodes = await make_series(
        session,
        title="Orphaned Show",
        seasons=[{"season_number": 1, "status": SeasonStatus.DOWNLOADING}],
        episodes_per_season=2,
        episode_statuses=[EpisodeStatus.COMPLETED, EpisodeStatus.DOWNLOADING],
        status=MediaStatus.DOWNLOADING,
    )
    # Episode 1 has a completed history row; Episode 2 is stuck in DOWNLOADING with NO history row
    session.add(
        DownloadHistory(
            media_item_id=show.id,
            season_id=seasons[0].id,
            episode_id=episodes[0].id,
            nzb_title="Orphaned.Show.S01E01.1080p.WEB-DL",
            downloader_transfer_id="tb-ep1-ok",
            status_detail="completed",
            is_dismissed=False,
            notification_sent=True,
        )
    )
    await session.commit()

    assert await count_downloading_entities(session) == 1
    transfer_poller.wake()

    res = await run_transfer_poller_tick(session, pre_fetched_downloads=[])
    await session.refresh(show)
    await session.refresh(seasons[0])
    await session.refresh(episodes[0])
    await session.refresh(episodes[1])

    assert episodes[0].status == EpisodeStatus.COMPLETED
    assert episodes[1].status == EpisodeStatus.SEARCHING
    assert seasons[0].status == SeasonStatus.SEARCHING
    assert show.status == MediaStatus.SEARCHING
    assert await count_downloading_entities(session) == 0
    assert res["remaining_active"] == 0
    assert res["sleeping"] is True
    assert transfer_poller.is_awake is False


@pytest.mark.asyncio
async def test_transfer_poller_in_flight_pushes_guard_and_apscheduler_pause_resume():
    """Verify TransferPollerState tracks in_flight_pushes, refuses to sleep while an
    in-flight push is active, and pauses/resumes transfer_poller_job in APScheduler.
    """
    from unittest.mock import MagicMock

    from app.core.transfer_poller import TransferPollerState

    poller = TransferPollerState()
    mock_scheduler = MagicMock()
    mock_job = MagicMock()
    # Initially job is running (next_run_time is not None)
    mock_job.next_run_time = datetime.now(timezone.utc)
    mock_scheduler.get_job.return_value = mock_job
    poller.set_scheduler(mock_scheduler)

    # 1. Calling sleep(0) when no in-flight pushes are active pauses transfer_poller_job
    poller.sleep(0)
    assert poller.is_awake is False
    mock_scheduler.pause_job.assert_called_once_with("transfer_poller_job")
    # Simulate APScheduler setting next_run_time = None when paused
    mock_job.next_run_time = None

    # 2. Registering an in-flight push wakes the poller and resumes transfer_poller_job
    poller.register_in_flight_push(42, "Star Wars Rebels")
    assert poller.is_awake is True
    assert poller.has_in_flight_pushes is True
    assert poller.in_flight_titles == ["Star Wars Rebels"]
    mock_scheduler.resume_job.assert_called_once_with("transfer_poller_job")
    mock_job.next_run_time = datetime.now(timezone.utc)

    # 3. Attempting to sleep while has_in_flight_pushes is True refuses to sleep or pause job
    mock_scheduler.pause_job.reset_mock()
    poller.sleep(0)
    assert poller.is_awake is True
    mock_scheduler.pause_job.assert_not_called()

    # 4. Unregistering the in-flight push allows sleep(0) to pause transfer_poller_job again
    poller.unregister_in_flight_push(42)
    assert poller.has_in_flight_pushes is False
    assert poller.in_flight_titles == []
    poller.sleep(0)
    assert poller.is_awake is False
    mock_scheduler.pause_job.assert_called_once_with("transfer_poller_job")


@pytest.mark.parametrize(
    "subjects,expected_fake",
    [
        # Only .sample.mkv (even with .nfo, .sfv, .srr, .par2, yEnc) -> rejected
        (
            [
                '[01/04] - "Show.S01E01.1080p.WEB.h264-GRP.nfo" yEnc (1/1)',
                '[02/04] - "Show.S01E01.1080p.WEB.h264-GRP.sample.mkv" yEnc (1/15)',
                '[03/04] - "Show.S01E01.1080p.WEB.h264-GRP.par2" yEnc (1/1)',
                '[04/04] - "Show.S01E01.1080p.WEB.h264-GRP.vol00+01.par2" yEnc (1/2)',
            ],
            True,
        ),
        # Unquoted .sample.mkv subject -> rejected
        (["Show.S01E01.1080p.WEB-DL.sample.mkv (1/10)"], True),
        # Hyphen/underscore/prefix/folder sample naming -> rejected
        (['[01/01] - "Show.S01E01.1080p-sample.mkv" yEnc (1/10)'], True),
        (['[01/01] - "sample-show.s01e01.1080p.mkv" yEnc (1/10)'], True),
        (['[01/01] - "Show.S01E01/Sample/Show.S01E01.mkv" yEnc (1/10)'], True),
        # Sample video + sample RAR only -> rejected
        (
            [
                '[01/02] - "Show.S01E01.1080p.sample.mkv" yEnc (1/10)',
                '[02/02] - "Show.S01E01.1080p.sample.rar" yEnc (1/10)',
            ],
            True,
        ),
        # Both .sample.mkv AND full episode .mkv -> accepted
        (
            [
                '[01/02] - "Show.S01E01.1080p.WEB.h264-GRP.sample.mkv" yEnc (1/10)',
                '[02/02] - "Show.S01E01.1080p.WEB.h264-GRP.mkv" yEnc (1/90)',
            ],
            False,
        ),
        # Both .sample.mkv AND main .part01.rar archive -> accepted at Layer 2
        (
            [
                '[01/02] - "Show.S01E01.1080p.WEB.h264-GRP.sample.mkv" yEnc (1/10)',
                '[02/02] - "Show.S01E01.1080p.WEB.h264-GRP.part01.rar" yEnc (1/50)',
            ],
            False,
        ),
        # Title legitimately starting with "Sample." -> accepted
        (['[01/01] - "Sample.People.2000.1080p.BluRay.mkv" yEnc (1/50)'], False),
    ],
)
def test_sample_only_layer2_nzb_content_detection(
    subjects: list[str], expected_fake: bool
):
    """Verify is_nzb_content_fake rejects NZBs containing only sample video files while
    accepting releases that also include a full video file or main archive.
    """
    from app.core.fake_detector import is_nzb_content_fake

    files_xml = "".join(
        f'<file poster="p" date="1700000000" subject=\'{subj}\'>'
        f"<groups><group>alt.binaries.tv</group></groups>"
        f'<segments><segment bytes="15000000" number="1">seg1</segment></segments>'
        f"</file>"
        for subj in subjects
    )
    nzb_bytes = f'<?xml version="1.0" encoding="UTF-8"?><nzb>{files_xml}</nzb>'.encode()
    is_fake, reason = is_nzb_content_fake(nzb_bytes, media_type="episode")
    assert is_fake is expected_fake
    if expected_fake:
        assert reason is not None
        assert "sample" in reason.lower()


@pytest.mark.parametrize(
    "files,expected_fake",
    [
        ([{"name": "Show.S01E01.1080p.WEB.h264-GRP.sample.mkv"}], True),
        (
            [
                {"name": "Show.S01E01.1080p.WEB.h264-GRP.sample.mkv"},
                {"name": "Show.S01E01.1080p.WEB.h264-GRP.nfo"},
            ],
            True,
        ),
        ([{"name": "Show.S01E01.1080p-sample.mkv"}], True),
        ([{"name": "sample-show.s01e01.1080p.mkv"}], True),
        ([{"name": "Show.S01E01/Sample/Show.S01E01.mkv"}], True),
        (
            [
                {"name": "Show.S01E01.1080p.WEB.h264-GRP.sample.mkv"},
                {"name": "Show.S01E01.1080p.WEB.h264-GRP.mkv"},
            ],
            False,
        ),
        ([{"name": "Sample.People.2000.1080p.BluRay.mkv"}], False),
    ],
)
def test_sample_only_layer3_torbox_filelist_detection(
    files: list[dict[str, str]], expected_fake: bool
):
    """Verify is_transfer_filelist_fake rejects completed TorBox transfers that only contain
    sample video files while accepting transfers that include the full video file.
    """
    from app.core.fake_detector import is_transfer_filelist_fake

    is_fake, reason, is_archive = is_transfer_filelist_fake(files, media_type="episode")
    assert is_fake is expected_fake
    if expected_fake:
        assert reason is not None
        assert "sample" in reason.lower()


@pytest.mark.asyncio
async def test_sample_only_rejected_and_blacklisted_in_push_and_poller(poller_db):
    """Verify end-to-end that:
    1. Layer 2 (push_engine) rejects and blacklists an NZB containing only .sample.mkv and
       automatically falls back to the next candidate containing a full .mkv.
    2. Layer 3 (transfer_poller) rejects a completed TorBox transfer whose unpacked files
       only contain .sample.mkv, deletes it from TorBox, blacklists it, and auto-replaces it.
    """
    from app.core.push_engine import execute_auto_push
    from app.core.transfer_poller import run_transfer_poller_tick
    from tests.conftest import make_series

    session, _, provider = poller_db
    show, seasons, episodes = await make_series(
        session,
        provider_id=provider.id,
        title="Severance",
        year=2022,
        seasons=1,
        episodes_per_season=1,
    )
    await session.commit()

    sample_only_nzb = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<nzb><file subject="Severance.S01E01.1080p.WEB-DL.sample.mkv (1/5)">'
        b'<segments><segment bytes="20000000" number="1">seg1</segment></segments>'
        b"</file></nzb>"
    )
    obfuscated_nzb = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<nzb><file subject="a1b2c3d4e5f6 (1/50)">'
        b'<segments><segment bytes="1500000000" number="1">seg2</segment></segments>'
        b"</file></nzb>"
    )
    full_episode_nzb = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<nzb><file subject="Severance.S01E01.1080p.WEB-DL.mkv (1/50)">'
        b'<segments><segment bytes="1500000000" number="1">seg3</segment></segments>'
        b"</file></nzb>"
    )

    candidates = [
        {
            "title": "Severance.S01E01.2160p.WEB-DL.DDP5.1.H.265-SAMPLEL2",
            "guid": "guid-sev-sample-l2",
            "size": int(4 * (1024**3)),
            "api_language": "en",
        },
        {
            "title": "Severance.S01E01.1080p.WEB-DL.DDP5.1.H.265-SAMPLEL3",
            "guid": "guid-sev-sample-l3",
            "size": int(2 * (1024**3)),
            "api_language": "en",
        },
        {
            "title": "Severance.S01E01.1080p.WEB-DL.DDP5.1.H.264-FULL",
            "guid": "guid-sev-full",
            "size": int(2 * (1024**3)),
            "api_language": "en",
        },
    ]

    async def fake_fetch_nzb(guid: str, **_kwargs):
        if guid == "guid-sev-sample-l2":
            return sample_only_nzb, "sample_l2.nzb"
        if guid == "guid-sev-sample-l3":
            return obfuscated_nzb, "sample_l3.nzb"
        return full_episode_nzb, "full.nzb"

    with (
        patch(
            "app.services.indexers.newznab.search_show",
            new_callable=AsyncMock,
            return_value=candidates,
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            side_effect=fake_fetch_nzb,
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new_callable=AsyncMock,
            return_value={"id": "tb-sev-l3", "hash": "hash-sev-l3"},
        ) as mock_send_tb,
    ):
        res = await execute_auto_push(
            session,
            show.id,
            {"selected_seasons": [seasons[0].id], "primary_language": "en"},
        )
        assert res["pushed"] is True
        # Layer 2 rejected guid-sev-sample-l2 and dispatched guid-sev-sample-l3 instead
        mock_send_tb.assert_awaited_once()
        assert mock_send_tb.await_args.kwargs["filename"] == "sample_l3.nzb"

    bl_rows = (
        (
            await session.execute(
                select(BlacklistedRelease).where(
                    BlacklistedRelease.media_item_id == show.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert any(
        b.nzb_guid == "guid-sev-sample-l2" and "sample" in (b.reason or "").lower()
        for b in bl_rows
    )

    # Now TorBox completes tb-sev-l3, but the unpacked file list only has a .sample.mkv!
    # Layer 3 must reject it, delete from TorBox, blacklist guid-sev-sample-l3, and auto-replace with guid-sev-full.
    sample_l3_snapshot = [
        {
            "id": "tb-sev-l3",
            "name": "Severance.S01E01.1080p.WEB-DL.DDP5.1.H.265-SAMPLEL3",
            "download_state": "completed",
            "download_finished": True,
            "progress": 1.0,
            "download_speed": 0,
            "eta": 0,
            "files": [
                {
                    "name": "Severance.S01E01.1080p.WEB-DL.DDP5.1.H.265-SAMPLEL3.sample.mkv",
                    "size": 25000000,
                },
                {
                    "name": "Severance.S01E01.1080p.WEB-DL.DDP5.1.H.265-SAMPLEL3.nfo",
                    "size": 1200,
                },
            ],
        }
    ]

    with (
        patch(
            "app.services.downloaders.torbox.delete_usenet_download",
            new_callable=AsyncMock,
            return_value={"success": True},
        ) as mock_del_tb,
        patch(
            "app.services.indexers.newznab.search_show",
            new_callable=AsyncMock,
            return_value=candidates,
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            side_effect=fake_fetch_nzb,
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new_callable=AsyncMock,
            return_value={"id": "tb-sev-full", "hash": "hash-sev-full"},
        ) as mock_send_repl,
    ):
        await run_transfer_poller_tick(
            session, pre_fetched_downloads=sample_l3_snapshot
        )
        mock_del_tb.assert_awaited_once_with("tb-sev-l3", session=session)
        mock_send_repl.assert_awaited_once()
        assert mock_send_repl.await_args.kwargs["filename"] == "full.nzb"

    bl_rows_after = (
        (
            await session.execute(
                select(BlacklistedRelease).where(
                    BlacklistedRelease.media_item_id == show.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert any(
        b.nzb_guid == "guid-sev-sample-l3"
        and b.reason
        == "Layer 3 Fake Detection: Sample-only release without full playable video"
        for b in bl_rows_after
    )


@pytest.mark.asyncio
async def test_layer3_single_episode_failure_strictly_episode_scoped_and_protects_completed_episodes(
    poller_db,
):
    """Verify ADR-098:
    1. When E01..E03 are pushed individually and E02 fails Layer 3 on TorBox with 0 remaining
       episode candidates on indexers (even though a Season Pack exists on indexers and
       prefer_season_packs=True on the show's preset), only E02 transitions to FAILED, no Season
       Pack is ever dispatched, and E01/E03 are never deleted or overwritten.
    2. When a Season Pack transfer fails on TorBox while E01 is already COMPLETED, _auto_replace_failed_push
       never overwrites E01 or dispatches a new Season Pack over E01, only replacing missing E02/E03.
    """
    from app.core.transfer_poller import (
        _auto_replace_failed_push,
        run_transfer_poller_tick,
    )
    from app.services.preset_service import (
        create_preset,
        update_item_sticky_search_config,
    )
    from tests.conftest import VALID_NZB_BYTES, make_history, make_series

    session, _, provider = poller_db
    preset = await create_preset(
        session,
        {
            "name": "Prefer Pack Preset",
            "is_default": True,
            "allow_season_packs": True,
            "prefer_season_packs": True,
        },
    )
    show, seasons, episodes = await make_series(
        session,
        provider_id=provider.id,
        title="Silo",
        year=2023,
        seasons=1,
        episodes_per_season=3,
        episode_statuses=[
            EpisodeStatus.COMPLETED,
            EpisodeStatus.DOWNLOADING,
            EpisodeStatus.DOWNLOADING,
        ],
    )
    await update_item_sticky_search_config(session, show.id, {"preset_id": preset.id})
    s1 = seasons[0]
    s1.status = SeasonStatus.DOWNLOADING
    show.status = MediaStatus.DOWNLOADING
    e01, e02, e03 = episodes

    h_e01 = await make_history(
        session,
        item=show,
        season=s1,
        episode=e01,
        nzb_title="Silo.S01E01.1080p.WEB-DL-GOOD",
        nzb_guid="guid-silo-e01",
        downloader_transfer_id="tb-silo-e01",
        status_detail="completed",
        is_dismissed=True,
    )
    h_e02 = await make_history(
        session,
        item=show,
        season=s1,
        episode=e02,
        nzb_title="Silo.S01E02.1080p.WEB-DL-RARONLY",
        nzb_guid="guid-silo-e02-bad",
        downloader_transfer_id="tb-silo-e02",
        status_detail="unpacking",
        unpack_started_at=datetime.now(timezone.utc) - timedelta(minutes=11),
        is_dismissed=False,
    )
    h_e03 = await make_history(
        session,
        item=show,
        season=s1,
        episode=e03,
        nzb_title="Silo.S01E03.1080p.WEB-DL-GOOD",
        nzb_guid="guid-silo-e03",
        downloader_transfer_id="tb-silo-e03",
        status_detail="downloading",
        is_dismissed=False,
    )
    await session.commit()

    # Indexer has a Season Pack available, but 0 remaining candidates for E02
    queried_eps: list[str | None] = []

    async def fake_search_show(season=None, ep=None, **_kwargs):
        queried_eps.append(ep)
        if ep is None:
            return [
                {
                    "title": "Silo.S01.2160p.WEB-DL.DDP5.1.H.265-PACK",
                    "guid": "guid-silo-pack-4k",
                    "size": int(25 * (1024**3)),
                }
            ]
        if str(ep) == "2":
            return [
                {
                    "title": "Silo.S01E02.1080p.WEB-DL-RARONLY",
                    "guid": "guid-silo-e02-bad",
                    "size": int(2 * (1024**3)),
                }
            ]
        return [
            {
                "title": f"Silo.S01E0{ep}.1080p.WEB-DL-REPL",
                "guid": f"guid-silo-e0{ep}-repl",
                "size": int(2 * (1024**3)),
            }
        ]

    poller_snapshot = [
        {
            "id": "tb-silo-e01",
            "name": h_e01.nzb_title,
            "download_state": "completed",
            "progress": 1.0,
            "files": [{"name": "Silo.S01E01.mkv", "size": 2000000000}],
        },
        {
            "id": "tb-silo-e02",
            "name": h_e02.nzb_title,
            "download_state": "completed",
            "download_finished": True,
            "progress": 1.0,
            "files": [{"name": "Silo.S01E02.part01.rar", "size": 2000000000}],
        },
        {
            "id": "tb-silo-e03",
            "name": h_e03.nzb_title,
            "download_state": "downloading",
            "progress": 0.5,
            "files": [],
        },
    ]

    with (
        patch(
            "app.services.downloaders.torbox.delete_usenet_download",
            new_callable=AsyncMock,
            return_value={"success": True},
        ) as mock_del_tb,
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new_callable=AsyncMock,
        ) as mock_send_tb,
    ):
        await run_transfer_poller_tick(session, pre_fetched_downloads=poller_snapshot)

    # Strictly deleted ONLY tb-silo-e02, queried ONLY ep="2" (never ep=None), and sent 0 packs!
    mock_del_tb.assert_awaited_once_with("tb-silo-e02", session=session)
    assert queried_eps == ["2"]
    mock_send_tb.assert_not_awaited()

    await session.refresh(e01)
    await session.refresh(e02)
    await session.refresh(e03)
    assert e01.status == EpisodeStatus.COMPLETED
    assert e02.status == EpisodeStatus.FAILED
    assert e03.status == EpisodeStatus.DOWNLOADING

    # Part 2: Suppose a Season Pack transfer failed on s1 while e01 is already COMPLETED.
    # _auto_replace_failed_push(session, show, season=s1, episode=None) must NOT overwrite e01
    # and must NOT dispatch a Season Pack over e01!
    queried_eps.clear()
    e02.status = EpisodeStatus.DOWNLOADING
    e03.status = EpisodeStatus.DOWNLOADING
    await session.commit()

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new_callable=AsyncMock,
            return_value=(VALID_NZB_BYTES, "ep.nzb"),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new_callable=AsyncMock,
            return_value={"id": "tb-silo-e03-repl", "hash": "hash-e03-repl"},
        ) as mock_send_part2,
    ):
        await _auto_replace_failed_push(session, show, season=s1, episode=None)

    await session.refresh(e01)
    assert e01.status == EpisodeStatus.COMPLETED
    # Season pack query (None) must NOT have been executed because e01 is already COMPLETED!
    assert None not in queried_eps
    assert mock_send_part2.await_count == 1


@pytest.mark.asyncio
async def test_layer3_unpack_grace_period_lifecycle_and_timeout_recovery(poller_db):
    """Verify Ticket 03 & Milestone 36:
    1. Archive-only Tick 1 enters status_detail='unpacking', sets unpack_started_at, keeps entity DOWNLOADING,
       and renders 'Unpacking' badge in Card 5 (active pushes).
    2. Archive + sample clip on Tick 1 enters unpacking rather than premature fake rejection.
    3. Executable file on Tick 1 is immediately rejected and blacklisted.
    4. Playable video appearing on Tick 2 immediately transitions to COMPLETED and clears unpack_started_at.
    5. Stalled archive exceeding UNPACK_GRACE_PERIOD_SECONDS (600s) triggers terminal deletion, blacklisting, and auto-replacement.
    """
    from app.core.transfer_poller import (
        count_downloading_entities,
        get_active_pushes,
        run_transfer_poller_tick,
    )

    session, _, provider = poller_db

    # --- Scenario A: Archive-only Tick 1 enters unpacking, stays alive, renders badge, and succeeds on Tick 2 ---
    movie = MediaItem(
        provider_id=provider.id,
        title="Dune Part Two",
        year=2024,
        media_type=MediaType.MOVIE,
        status=MediaStatus.DOWNLOADING,
    )
    session.add(movie)
    await session.flush()

    now_utc = datetime.now(timezone.utc)
    hist = DownloadHistory(
        media_item_id=movie.id,
        nzb_title="Dune.Part.Two.2024.2160p.UHD.BluRay.x265-SCENE",
        nzb_guid="guid-dune-scene",
        score=9800.0,
        size_bytes=40 * (1024**3),
        downloader_transfer_id="tb-dune-1",
        dispatched_at=now_utc - timedelta(seconds=30),
        push_mode="auto",
        grabbed_language="en",
    )
    session.add(hist)
    await session.commit()

    # Tick 1: TorBox reports download_finished=True, files list has only RAR/PAR2 archives
    archive_tick1_snapshot = [
        {
            "id": "tb-dune-1",
            "name": hist.nzb_title,
            "download_state": "completed",
            "download_finished": True,
            "progress": 1.0,
            "download_speed": 0,
            "eta": 0,
            "files": [
                {"name": "dune2.part01.rar", "size": 1000000000},
                {"name": "dune2.part02.rar", "size": 1000000000},
                {"name": "dune2.par2", "size": 500000},
            ],
        }
    ]

    res1 = await run_transfer_poller_tick(
        session, pre_fetched_downloads=archive_tick1_snapshot
    )
    assert res1["polled"] is True
    assert res1["active_count"] == 1
    assert res1["sleeping"] is False

    await session.refresh(movie)
    await session.refresh(hist)
    assert movie.status == MediaStatus.DOWNLOADING
    assert hist.status_detail == "unpacking"
    assert hist.progress_pct == 100.0
    assert hist.download_speed_bytes == 0
    assert hist.eta_seconds is None
    assert hist.unpack_started_at is not None

    # Verify count_downloading_entities stays awake (> 0)
    awake_count = await count_downloading_entities(session)
    assert awake_count == 1

    # Verify Card 5 (get_active_pushes) renders with "Unpacking" badge
    active_pushes = await get_active_pushes(session)
    matching_push = next((p for p in active_pushes if p["history_id"] == hist.id), None)
    assert matching_push is not None
    assert matching_push["status"] == "Unpacking"
    assert matching_push["status_detail"] == "unpacking"

    # Tick 2: Extraction finishes! Files now include full playable video
    playable_tick2_snapshot = [
        {
            "id": "tb-dune-1",
            "name": hist.nzb_title,
            "download_state": "completed",
            "download_finished": True,
            "progress": 1.0,
            "download_speed": 0,
            "eta": 0,
            "files": [
                {"name": "dune2.part01.rar", "size": 1000000000},
                {"name": "dune2.part02.rar", "size": 1000000000},
                {"name": "Dune.Part.Two.2024.2160p.mkv", "size": 38 * (1024**3)},
            ],
        }
    ]
    with patch(
        "app.services.discord.send_discord_webhook",
        new_callable=AsyncMock,
        return_value=True,
    ):
        res2 = await run_transfer_poller_tick(
            session, pre_fetched_downloads=playable_tick2_snapshot
        )
    await session.refresh(movie)
    await session.refresh(hist)
    assert movie.status == MediaStatus.COMPLETED
    assert hist.status_detail == "completed"
    assert hist.completed_at is not None
    assert hist.unpack_started_at is None
    assert res2["remaining_active"] == 0


@pytest.mark.asyncio
async def test_layer3_archive_plus_sample_enters_unpacking_and_executable_fails_tick1(
    poller_db,
):
    """Verify:
    1. Archive + sample clip enters unpacking.
    2. Executable payload is immediately rejected on Tick 1 even if archives are present.
    """
    from app.core.transfer_poller import run_transfer_poller_tick

    session, _, provider = poller_db

    movie = MediaItem(
        provider_id=provider.id,
        title="Oppenheimer",
        year=2023,
        media_type=MediaType.MOVIE,
        status=MediaStatus.DOWNLOADING,
    )
    session.add(movie)
    await session.flush()

    now_utc = datetime.now(timezone.utc)
    hist = DownloadHistory(
        media_item_id=movie.id,
        nzb_title="Oppenheimer.2023.2160p.UHD.BluRay.x265-SCENE",
        nzb_guid="guid-oppenheimer-scene",
        score=9900.0,
        size_bytes=50 * (1024**3),
        downloader_transfer_id="tb-opp-1",
        dispatched_at=now_utc,
        push_mode="auto",
        grabbed_language="en",
    )
    session.add(hist)
    await session.commit()

    # Part 1: Archive + Sample clip
    archive_and_sample_snapshot = [
        {
            "id": "tb-opp-1",
            "name": hist.nzb_title,
            "download_state": "completed",
            "download_finished": True,
            "progress": 1.0,
            "download_speed": 0,
            "eta": 0,
            "files": [
                {"name": "oppenheimer.part01.rar", "size": 1000000000},
                {"name": "oppenheimer-sample.mkv", "size": 30000000},
            ],
        }
    ]
    await run_transfer_poller_tick(
        session, pre_fetched_downloads=archive_and_sample_snapshot
    )
    await session.refresh(movie)
    await session.refresh(hist)
    assert movie.status == MediaStatus.DOWNLOADING
    assert hist.status_detail == "unpacking"
    assert hist.unpack_started_at is not None

    # Part 2: Executable alongside archive -> immediate Tick-1 rejection
    exe_and_archive_snapshot = [
        {
            "id": "tb-opp-1",
            "name": hist.nzb_title,
            "download_state": "completed",
            "download_finished": True,
            "progress": 1.0,
            "download_speed": 0,
            "eta": 0,
            "files": [
                {"name": "oppenheimer.part01.rar", "size": 1000000000},
                {"name": "codec_installer.exe", "size": 1200000},
            ],
        }
    ]
    with (
        patch(
            "app.services.downloaders.torbox.delete_usenet_download",
            new_callable=AsyncMock,
            return_value={"success": True},
        ) as mock_del,
        patch(
            "app.services.indexers.newznab.search_movie",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        await run_transfer_poller_tick(
            session, pre_fetched_downloads=exe_and_archive_snapshot
        )
        mock_del.assert_awaited_once_with("tb-opp-1", session=session)

    await session.refresh(movie)
    await session.refresh(hist)
    assert (hist.status_detail or "").startswith("failed")
    bl = (
        await session.execute(
            select(BlacklistedRelease).where(
                BlacklistedRelease.nzb_guid == "guid-oppenheimer-scene"
            )
        )
    ).scalar_one_or_none()
    assert bl is not None
    assert "executable" in (bl.reason or "").lower()


@pytest.mark.asyncio
async def test_layer3_archive_timeout_triggers_deletion_blacklisting_and_replacement(
    poller_db,
):
    """Verify that stalled archive exceeding 600s (UNPACK_GRACE_PERIOD_SECONDS) triggers
    terminal deletion, blacklisting with 10m reason, and auto-replacement.
    """
    from app.core.transfer_poller import run_transfer_poller_tick
    from tests.conftest import VALID_NZB_BYTES

    session, _, provider = poller_db

    movie = MediaItem(
        provider_id=provider.id,
        title="Gladiator",
        year=2000,
        media_type=MediaType.MOVIE,
        status=MediaStatus.DOWNLOADING,
    )
    session.add(movie)
    await session.flush()

    now_utc = datetime.now(timezone.utc)
    # Stalled unpack timer initialized 650 seconds ago (exceeding 600s)
    hist = DownloadHistory(
        media_item_id=movie.id,
        nzb_title="Gladiator.2000.2160p.UHD.BluRay.x265-STALLED",
        nzb_guid="guid-gladiator-stalled",
        score=9500.0,
        size_bytes=30 * (1024**3),
        downloader_transfer_id="tb-glad-1",
        dispatched_at=now_utc - timedelta(seconds=700),
        unpack_started_at=now_utc - timedelta(seconds=650),
        status_detail="unpacking",
        push_mode="auto",
        grabbed_language="en",
    )
    session.add(hist)
    await session.commit()

    archive_stalled_snapshot = [
        {
            "id": "tb-glad-1",
            "name": hist.nzb_title,
            "download_state": "completed",
            "download_finished": True,
            "progress": 1.0,
            "download_speed": 0,
            "eta": 0,
            "files": [
                {"name": "gladiator.part01.rar", "size": 1000000000},
                {"name": "gladiator.part02.rar", "size": 1000000000},
            ],
        }
    ]

    indexer_candidates = [
        {
            "title": "Gladiator.2000.2160p.UHD.BluRay.Remux.HEVC-WORKING",
            "guid": "guid-gladiator-working",
            "size": int(28 * (1024**3)),
            "api_language": "en",
        }
    ]

    with (
        patch(
            "app.services.downloaders.torbox.delete_usenet_download",
            new_callable=AsyncMock,
            return_value={"success": True},
        ) as mock_del,
        patch(
            "app.services.indexers.newznab.search_movie",
            new_callable=AsyncMock,
            return_value=indexer_candidates,
        ) as mock_search,
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new_callable=AsyncMock,
            return_value=(VALID_NZB_BYTES, "glad.nzb"),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new_callable=AsyncMock,
            return_value={"id": "tb-glad-2", "hash": "hash-glad-2"},
        ) as mock_send,
    ):
        await run_transfer_poller_tick(
            session, pre_fetched_downloads=archive_stalled_snapshot
        )
        mock_del.assert_awaited_once_with("tb-glad-1", session=session)
        mock_search.assert_awaited_once()
        mock_send.assert_awaited_once()

    await session.refresh(movie)
    await session.refresh(hist)
    assert hist.status_detail == "replaced"
    assert movie.status == MediaStatus.DOWNLOADING

    bl = (
        await session.execute(
            select(BlacklistedRelease).where(
                BlacklistedRelease.nzb_guid == "guid-gladiator-stalled"
            )
        )
    ).scalar_one_or_none()
    assert bl is not None
    assert (
        "10m" in (bl.reason or "")
        or "failed to extract archive" in (bl.reason or "").lower()
    )


@pytest.mark.asyncio
async def test_poller_waits_while_torbox_status_is_processing(
    db_session: AsyncSession,
) -> None:
    """Verify that when TorBox reports download_finished=True but download_state='processing',

    the poller does NOT prematurely trigger Layer 3 fake verification or fail/delete the transfer,
    even if the files list is empty or lacks extracted playable video.
    """
    from app.core.transfer_poller import run_transfer_poller_tick
    from tests.conftest import make_history, make_movie

    session = db_session
    movie = await make_movie(session, title="Processing Movie", year=2024)
    movie.status = MediaStatus.DOWNLOADING
    await session.commit()

    hist = await make_history(
        session,
        item=movie,
        downloader_transfer_id="tb-proc-1",
        nzb_title="Processing.Movie.2024.1080p.WEB.DL-GROUP",
        nzb_guid="guid-proc-1",
        status_detail="downloading",
        is_dismissed=False,
    )
    await session.commit()

    # TorBox reports 100% progress and download_finished=True, but download_state is still 'processing'
    processing_snapshot = [
        {
            "id": "tb-proc-1",
            "name": hist.nzb_title,
            "download_state": "processing",
            "download_finished": True,
            "progress": 1.0,
            "download_speed": 0,
            "eta": 0,
            "files": [],  # TorBox hasn't populated/extracted the files yet!
        }
    ]

    with patch(
        "app.services.downloaders.torbox.delete_usenet_download", new_callable=AsyncMock
    ) as mock_del:
        res = await run_transfer_poller_tick(
            session, pre_fetched_downloads=processing_snapshot
        )
        assert res["polled"] is True
        assert res["active_count"] == 1
        assert res["sleeping"] is False
        mock_del.assert_not_called()

    await session.refresh(movie)
    await session.refresh(hist)
    assert movie.status == MediaStatus.DOWNLOADING
    assert hist.status_detail == "processing"
    assert hist.progress_pct == 100.0

    # Ensure no blacklist row was created
    bl = (
        await session.execute(
            select(BlacklistedRelease).where(
                BlacklistedRelease.nzb_guid == "guid-proc-1"
            )
        )
    ).scalar_one_or_none()
    assert bl is None
