"""Automated tests for Ticket 01: Poller Mutex, Short-Lived SQLite Transactions & Show-Level Batch Notifications."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.database import close_db, get_session_factory, init_db
from app.db.models import (
    DownloadHistory,
    Episode,
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    MediaType,
    NotificationChannel,
    Provider,
    Season,
    SeasonStatus,
    SystemSettings,
)


@pytest.fixture
async def poller_batch_db(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
):
    provider = Provider(name="Simkl", type="simkl", category="watchlist")
    sys_settings = SystemSettings(
        id=1,
        download_timeout_hours=24,
        sh_max_retries=3,
        discord_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test/batch",
        notify_on_push_initiated=True,
        notify_on_completed=True,
        notify_on_failure=True,
        notify_on_auto_advance=True,
    )
    tg_channel = NotificationChannel(
        name="Main TG",
        type="telegram",
        bot_token="123:TOKEN",
        chat_id="999",
    )
    db_session.add_all([provider, sys_settings, tg_channel])
    await db_session.commit()
    return db_session, session_factory, provider


@pytest.mark.asyncio
async def test_init_db_migrates_notification_sent_and_auto_replaced_count(tmp_path):
    """Verify init_db() idempotently adds notification_sent and auto_replaced_count
    and backfills existing terminal DownloadHistory rows to notification_sent = True.
    """
    db_file = tmp_path / "legacy_m21_01.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    # Create a legacy download_history table without notification_sent or auto_replaced_count
    raw_engine = create_async_engine(db_url)
    async with raw_engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE providers (id INTEGER PRIMARY KEY, type VARCHAR(50), name VARCHAR(100));"
            )
        )
        await conn.execute(
            text("INSERT INTO providers (id, type, name) VALUES (1, 'simkl', 'Simkl');")
        )
        await conn.execute(
            text(
                "CREATE TABLE media_items ("
                "id INTEGER PRIMARY KEY, provider_id INTEGER NOT NULL, simkl_id INTEGER NOT NULL DEFAULT 0, "
                "title VARCHAR(500) NOT NULL, media_type VARCHAR(20) NOT NULL, status VARCHAR(20) NOT NULL"
                ");"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO media_items (id, provider_id, simkl_id, title, media_type, status) "
                "VALUES (1, 1, 100, 'Legacy Movie', 'MOVIE', 'COMPLETED');"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE download_history ("
                "id INTEGER PRIMARY KEY, media_item_id INTEGER NOT NULL, season_id INTEGER, episode_id INTEGER, "
                "nzb_title VARCHAR(1000) NOT NULL, nzb_guid VARCHAR(500), score FLOAT NOT NULL DEFAULT 0.0, "
                "size_bytes INTEGER NOT NULL DEFAULT 0, resolution VARCHAR(50), video_codec VARCHAR(50), "
                "audio_codec VARCHAR(50), source VARCHAR(50), release_group VARCHAR(100), bitrate_mbps FLOAT, "
                "torbox_id VARCHAR(100), torbox_hash VARCHAR(200), torbox_sent_at DATETIME, "
                "is_fallback BOOLEAN NOT NULL DEFAULT 0, grabbed_language VARCHAR(20), "
                "status_detail VARCHAR(200), created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                ");"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO download_history (id, media_item_id, nzb_title, status_detail) VALUES "
                "(1, 1, 'Legacy.Completed.1080p', 'completed'), "
                "(2, 1, 'Legacy.Downloading.1080p', 'downloading'), "
                "(3, 1, 'Legacy.Failed.1080p', 'failed: Repair error');"
            )
        )
    await raw_engine.dispose()

    await init_db(db_url)
    try:
        factory = get_session_factory()
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(DownloadHistory).order_by(DownloadHistory.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 3
            # Terminal rows (completed & failed) backfilled to notification_sent = True
            assert rows[0].notification_sent is True
            assert rows[0].auto_replaced_count == 0
            # Active downloading row stays notification_sent = False
            assert rows[1].notification_sent is False
            assert rows[1].auto_replaced_count == 0
            assert rows[2].notification_sent is True
    finally:
        await close_db()


@pytest.mark.asyncio
async def test_format_batch_target_summary_contiguous_and_multi_season(poller_batch_db):
    """Verify compact target range formatting for single items, contiguous episodes,
    non-contiguous episodes, and multi-season batches.
    """
    from app.services.notifications import format_batch_target_summary

    session, _, provider = poller_batch_db
    show = MediaItem(
        provider_id=provider.id,
        title="Bleach",
        media_type=MediaType.ANIME,
        status=MediaStatus.DOWNLOADING,
    )
    session.add(show)
    await session.flush()

    s1 = Season(
        media_item_id=show.id,
        season_number=1,
        type_number=1,
        watch_order=1,
        status=SeasonStatus.DOWNLOADING,
    )
    s2 = Season(
        media_item_id=show.id,
        season_number=2,
        type_number=2,
        watch_order=2,
        status=SeasonStatus.DOWNLOADING,
    )
    session.add_all([s1, s2])
    await session.flush()

    s1_eps = [Episode(season_id=s1.id, episode_number=i) for i in range(1, 14)]
    s2_eps = [Episode(season_id=s2.id, episode_number=i) for i in range(1, 6)]
    session.add_all(s1_eps + s2_eps)
    await session.flush()

    s1_hists = [
        DownloadHistory(
            media_item_id=show.id,
            season_id=s1.id,
            episode_id=ep.id,
            nzb_title=f"Bleach.S01E{ep.episode_number:02d}.1080p",
            season=s1,
            episode=ep,
        )
        for ep in s1_eps
    ]

    # Single episode
    assert format_batch_target_summary([s1_hists[4]], item=show) == "S01E05"

    # Contiguous 13-episode season
    assert (
        format_batch_target_summary(s1_hists, item=show)
        == "S01E01–S01E13 (13 Episodes)"
    )

    # Non-contiguous episodes in S01 (E01..E03, E07)
    non_contig = s1_hists[:3] + [s1_hists[6]]
    summary_nc = format_batch_target_summary(non_contig, item=show)
    assert "S01E01–S01E03" in summary_nc or "S01E01–E03" in summary_nc
    assert "E07" in summary_nc
    assert "(4 Episodes)" in summary_nc

    # Multi-season batch (S01E01..E13 + S02E01..E05 = 18 Episodes)
    s2_hists = [
        DownloadHistory(
            media_item_id=show.id,
            season_id=s2.id,
            episode_id=ep.id,
            nzb_title=f"Bleach.S02E{ep.episode_number:02d}.1080p",
            season=s2,
            episode=ep,
        )
        for ep in s2_eps
    ]
    multi_summary = format_batch_target_summary(s1_hists + s2_hists, item=show)
    assert "S01E01–S01E13" in multi_summary
    assert "S02E01–S02E05" in multi_summary
    assert "(18 Episodes)" in multi_summary


@pytest.mark.asyncio
async def test_multi_episode_auto_push_emits_single_consolidated_notification(
    poller_batch_db,
):
    """Verify pushing a 10-episode season with allow_season_packs=False sends exactly 1
    consolidated push_initiated notification instead of 10 separate webhook calls.
    """
    from app.core.push_engine import execute_auto_push

    session, _, provider = poller_batch_db
    show = MediaItem(
        provider_id=provider.id,
        title="Mob Psycho 100",
        year=2016,
        media_type=MediaType.ANIME,
        status=MediaStatus.SEARCHING,
        allow_season_packs=False,
        prefer_season_packs=False,
    )
    session.add(show)
    await session.flush()

    s1 = Season(
        media_item_id=show.id,
        season_number=1,
        type_number=1,
        watch_order=1,
        monitored=True,
        status=SeasonStatus.SEARCHING,
        air_date=datetime(2016, 7, 11, tzinfo=timezone.utc),
    )
    session.add(s1)
    await session.flush()

    for ep_num in range(1, 11):
        session.add(
            Episode(
                season_id=s1.id,
                episode_number=ep_num,
                monitored=True,
                status=EpisodeStatus.SEARCHING,
                air_date=datetime(2016, 7, 11, tzinfo=timezone.utc),
            )
        )
    await session.commit()

    valid_nzb = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<nzb><file subject="Mob.mkv"><segments>'
        b'<segment bytes="50000000" number="1">m1</segment>'
        b"</segments></file></nzb>"
    )

    async def fake_search_show(**kwargs):
        ep = kwargs.get("ep")
        return [
            {
                "title": f"Mob.Psycho.100.S01E{int(ep):02d}.1080p.BluRay.x265-GROUP",
                "guid": f"guid-mob-e{ep}",
                "size": 1024**3,
                "api_language": "en",
            }
        ]

    tb_counter = 0

    async def fake_send_nzb(*args, **kwargs):
        nonlocal tb_counter
        tb_counter += 1
        return {"id": f"tb-mob-{tb_counter}", "hash": f"hash-mob-{tb_counter}"}

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new_callable=AsyncMock,
            return_value=(valid_nzb, "mob.nzb"),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file", side_effect=fake_send_nzb
        ),
        patch(
            "app.services.discord.send_discord_webhook",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_discord,
        patch(
            "app.services.telegram.send_notification",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_tg,
    ):
        res = await execute_auto_push(session, show.id, {"season_ids": [s1.id]})
        assert res["pushed"] is True
        assert res["dispatched_count"] == 10
        # Strictly 1 Discord webhook and 1 Telegram notification for the entire 10-episode push!
        mock_discord.assert_awaited_once()
        mock_tg.assert_awaited_once()

        embed = mock_discord.call_args.kwargs["embed"]
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["Target"] == "S01E01–S01E10 (10 Episodes)"
        assert "10 releases" in fields["Release"]


@pytest.mark.asyncio
async def test_poller_holds_notifications_until_all_show_downloads_settle_and_suppresses_retry_spam(
    poller_batch_db,
):
    """Verify:
    1. When 5 episodes of a show are downloading, completing E01..E03 on Tick 1 fires 0 notifications.
    2. When E04 fails on Tick 1 and is auto-replaced (`push_mode == 'auto'`), 0 intermediate `auto_replaced`
       notifications fire and `auto_replaced_count` is incremented to 1 on the replacement row.
    3. When E04 (replacement) and E05 finish on Tick 2 (`remaining_downloading == 0`), 1 consolidated
       `Ready on TorBox` notification fires for `S01E01–S01E05 (5 Episodes)` noting the 1 auto-replacement.
    """
    from app.core.transfer_poller import run_transfer_poller_tick

    session, _, provider = poller_batch_db
    show = MediaItem(
        provider_id=provider.id,
        title="Chainsaw Man",
        year=2022,
        media_type=MediaType.ANIME,
        status=MediaStatus.DOWNLOADING,
    )
    session.add(show)
    await session.flush()

    s1 = Season(
        media_item_id=show.id,
        season_number=1,
        type_number=1,
        watch_order=1,
        monitored=True,
        status=SeasonStatus.DOWNLOADING,
    )
    session.add(s1)
    await session.flush()

    now_utc = datetime.now(timezone.utc)
    eps: list[Episode] = []
    for ep_num in range(1, 6):
        ep = Episode(
            season_id=s1.id,
            episode_number=ep_num,
            monitored=True,
            status=EpisodeStatus.DOWNLOADING,
        )
        session.add(ep)
        eps.append(ep)
    await session.flush()

    for ep in eps:
        session.add(
            DownloadHistory(
                media_item_id=show.id,
                season_id=s1.id,
                episode_id=ep.id,
                nzb_title=f"Chainsaw.Man.S01E{ep.episode_number:02d}.1080p.WEB-DL-INITIAL",
                nzb_guid=f"guid-csm-e{ep.episode_number}-init",
                score=8000.0,
                size_bytes=1024**3,
                resolution="1080p",
                source="web-dl",
                downloader_transfer_id=f"tb-csm-{ep.episode_number}",
                dispatched_at=now_utc - timedelta(minutes=20),
                push_mode="auto",
                grabbed_language="en",
            )
        )
    await session.commit()

    valid_nzb = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<nzb><file subject="CSM.E04.mkv"><segments>'
        b'<segment bytes="50000000" number="1">m1</segment>'
        b"</segments></file></nzb>"
    )

    # Tick 1: E01..E03 completed, E04 failed (will be auto-replaced with tb-csm-4-repl), E05 still downloading
    tick1_snapshot = [
        {
            "id": f"tb-csm-{i}",
            "name": f"Chainsaw.Man.S01E0{i}.1080p.WEB-DL-INITIAL",
            "download_state": "completed",
            "progress": 1.0,
            "files": [{"name": f"CSM.S01E0{i}.mkv", "size": 1024**3}],
        }
        for i in (1, 2, 3)
    ] + [
        {
            "id": "tb-csm-4",
            "name": "Chainsaw.Man.S01E04.1080p.WEB-DL-INITIAL",
            "download_state": "failed (Repair failed)",
            "progress": 0.2,
            "files": [],
        },
        {
            "id": "tb-csm-5",
            "name": "Chainsaw.Man.S01E05.1080p.WEB-DL-INITIAL",
            "download_state": "downloading",
            "progress": 0.65,
            "download_speed": 10485760,
            "eta": 45,
            "files": [],
        },
    ]

    e04_candidates = [
        {
            "title": "Chainsaw.Man.S01E04.1080p.WEB-DL-INITIAL",
            "guid": "guid-csm-e4-init",
            "size": 1024**3,
            "api_language": "en",
        },
        {
            "title": "Chainsaw.Man.S01E04.1080p.BluRay.x265-REPLACEMENT",
            "guid": "guid-csm-e4-repl",
            "size": 1024**3,
            "api_language": "en",
        },
    ]

    with (
        patch(
            "app.services.downloaders.torbox.delete_usenet_download",
            new_callable=AsyncMock,
            return_value={"success": True},
        ),
        patch(
            "app.services.indexers.newznab.search_show",
            new_callable=AsyncMock,
            return_value=e04_candidates,
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new_callable=AsyncMock,
            return_value=(valid_nzb, "csm_e04_repl.nzb"),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new_callable=AsyncMock,
            return_value={"id": "tb-csm-4-repl", "hash": "hash-csm-4-repl"},
        ),
        patch(
            "app.services.discord.send_discord_webhook",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_discord,
        patch(
            "app.services.telegram.send_notification",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_tg,
    ):
        await run_transfer_poller_tick(session, pre_fetched_downloads=tick1_snapshot)
        # ZERO notifications during Tick 1 because E04 (replacement) and E05 are still downloading!
        mock_discord.assert_not_awaited()
        mock_tg.assert_not_awaited()

    # Check that E04's replacement row has auto_replaced_count == 1
    e04_repl = (
        (
            await session.execute(
                select(DownloadHistory).where(
                    DownloadHistory.episode_id == eps[3].id,
                    DownloadHistory.is_dismissed.is_(False),
                )
            )
        )
        .scalars()
        .first()
    )
    assert e04_repl is not None
    assert e04_repl.downloader_transfer_id == "tb-csm-4-repl"
    assert e04_repl.auto_replaced_count == 1

    # Tick 2: E01..E03 remain on TorBox while E04 replacement and E05 both complete!
    tick2_snapshot = tick1_snapshot[:3] + [
        {
            "id": "tb-csm-4-repl",
            "name": "Chainsaw.Man.S01E04.1080p.BluRay.x265-REPLACEMENT",
            "download_state": "completed",
            "progress": 1.0,
            "files": [{"name": "CSM.S01E04.mkv", "size": 1024**3}],
        },
        {
            "id": "tb-csm-5",
            "name": "Chainsaw.Man.S01E05.1080p.WEB-DL-INITIAL",
            "download_state": "completed",
            "progress": 1.0,
            "files": [{"name": "CSM.S01E05.mkv", "size": 1024**3}],
        },
    ]

    with (
        patch(
            "app.services.discord.send_discord_webhook",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_discord,
        patch(
            "app.services.telegram.send_notification",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_tg,
    ):
        await run_transfer_poller_tick(session, pre_fetched_downloads=tick2_snapshot)
        # Exactly 1 consolidated Ready on TorBox notification for all 5 episodes!
        mock_discord.assert_awaited_once()
        mock_tg.assert_awaited_once()

        embed = mock_discord.call_args.kwargs["embed"]
        assert embed["color"] == 0x10B981
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        assert fields["Target"] == "S01E01–S01E05 (5 Episodes)"
        assert "Auto-replaced 1 failed transfer" in fields["Status"]

        # Running another tick does not re-notify because notification_sent is now True
        mock_discord.reset_mock()
        await run_transfer_poller_tick(session, pre_fetched_downloads=tick2_snapshot)
        mock_discord.assert_not_awaited()


@pytest.mark.asyncio
async def test_poller_mutex_serializes_concurrent_poller_ticks(poller_batch_db):
    """Verify _poller_lock serializes concurrent run_transfer_poller_tick() invocations
    so they never overlap in execution.
    """
    from app.core.transfer_poller import _poller_lock, run_transfer_poller_tick

    session, maker, provider = poller_batch_db
    movie = MediaItem(
        provider_id=provider.id,
        title="Arrival",
        year=2016,
        media_type=MediaType.MOVIE,
        status=MediaStatus.DOWNLOADING,
    )
    session.add(movie)
    await session.flush()
    session.add(
        DownloadHistory(
            media_item_id=movie.id,
            nzb_title="Arrival.2016.1080p.BluRay",
            downloader_transfer_id="tb-arr-1",
            dispatched_at=datetime.now(timezone.utc),
            push_mode="auto",
        )
    )
    await session.commit()

    assert isinstance(_poller_lock, asyncio.Lock)
    active_inside = 0
    max_concurrent = 0

    async def slow_get_downloads(*args, **kwargs):
        nonlocal active_inside, max_concurrent
        active_inside += 1
        max_concurrent = max(max_concurrent, active_inside)
        await asyncio.sleep(0.05)
        active_inside -= 1
        return [
            {
                "id": "tb-arr-1",
                "name": "Arrival.2016.1080p.BluRay",
                "download_state": "downloading",
                "progress": 0.5,
                "download_speed": 1000,
                "eta": 60,
                "files": [],
            }
        ]

    with (
        patch("app.core.transfer_poller.async_session_factory", maker),
        patch(
            "app.services.downloaders.torbox.get_usenet_downloads",
            side_effect=slow_get_downloads,
        ),
    ):
        await asyncio.gather(
            run_transfer_poller_tick(),
            run_transfer_poller_tick(),
        )

    assert max_concurrent == 1


@pytest.mark.asyncio
async def test_send_discord_webhook_retries_on_http_429_with_retry_after():
    """Verify send_discord_webhook sleeps for retry_after on HTTP 429 and succeeds on retry."""
    from app.services.discord import build_discord_embed, send_discord_webhook

    embed = build_discord_embed(
        event_type="completed",
        media_title="Arrival",
        target_label="Movie",
        release_name="Arrival.2016.1080p",
    )

    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.json.return_value = {
        "message": "You are being rate limited.",
        "retry_after": 0.25,
    }
    resp_429.headers = {}

    resp_204 = MagicMock()
    resp_204.status_code = 204
    resp_204.raise_for_status = MagicMock()

    with (
        patch("app.services.discord.httpx.AsyncClient") as mock_client_cls,
        patch(
            "app.services.discord.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep,
    ):
        mock_client = AsyncMock()
        mock_client.post.side_effect = [resp_429, resp_204]
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        ok = await send_discord_webhook(
            "https://discord.com/api/webhooks/123/abc", embed=embed
        )
        assert ok is True
        assert mock_client.post.await_count == 2
        mock_sleep.assert_awaited_once_with(0.25)
