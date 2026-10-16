"""Unit and integration tests for Ticket 05: Discord Webhooks & Per-Event Notification Dispatcher."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    MediaItem,
    MediaStatus,
    MediaType,
    NotificationChannel,
    Season,
    SeasonStatus,
    SystemSettings,
)


@pytest.mark.asyncio
async def test_build_discord_embed_colors_and_fields():
    """Verify Discord embed formatting, event color coding, poster thumbnail, and structured fields."""
    from app.services.discord import EVENT_COLORS, build_discord_embed

    # 1. Ready on TorBox (Completed) -> Green
    embed_completed = build_discord_embed(
        event_type="completed",
        media_title="Dune: Part Two",
        media_year=2024,
        target_label="Movie",
        release_name="Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-FLUX",
        resolution="2160p",
        source="WEB-DL",
        language="en",
        status_reason="Verified playable video on TorBox",
        poster_url="https://image.tmdb.org/t/p/w500/dune2.jpg",
    )
    assert embed_completed["color"] == EVENT_COLORS["completed"]
    assert embed_completed["color"] == 0x10B981
    assert "Dune: Part Two" in embed_completed["title"]
    assert (
        embed_completed["thumbnail"]["url"]
        == "https://image.tmdb.org/t/p/w500/dune2.jpg"
    )
    field_map = {f["name"]: f["value"] for f in embed_completed["fields"]}
    assert "Target" in field_map
    assert "Movie" in field_map["Target"]
    assert "Release" in field_map
    assert "Dune.Part.Two" in field_map["Release"]
    assert "Quality & Language" in field_map
    assert "2160p" in field_map["Quality & Language"]
    assert "EN" in field_map["Quality & Language"]
    assert "Status" in field_map
    assert "Verified playable video" in field_map["Status"]

    # 2. Failure -> Red, Auto-Replaced -> Amber
    embed_fail = build_discord_embed(
        event_type="failure",
        media_title="Severance",
        target_label="S01E05",
        release_name="Severance.S01E05.1080p.WEB.H264-BAD",
        status_reason="Manual pick failed: Repair failed",
    )
    assert embed_fail["color"] == EVENT_COLORS["failure"]
    assert embed_fail["color"] == 0xEF4444
    assert "thumbnail" not in embed_fail

    embed_replaced = build_discord_embed(
        event_type="auto_replaced",
        media_title="Severance",
        target_label="Season 1",
        release_name="Severance.S01.1080p.WEB-DL-NEXT",
        status_reason="Auto-replaced failed transfer with next-best candidate",
    )
    assert embed_replaced["color"] == EVENT_COLORS["auto_replaced"]
    assert embed_replaced["color"] == 0xF59E0B

    # 3. Auto-Advance -> Purple
    embed_advance = build_discord_embed(
        event_type="auto_advance",
        media_title="Demon Slayer",
        target_label="Movie 1 — Mugen Train",
        release_name="Demon.Slayer.Mugen.Train.2020.1080p.BluRay.x265",
        status_reason="Auto-Advance Season Expansion triggered",
    )
    assert embed_advance["color"] == EVENT_COLORS["auto_advance"]
    assert embed_advance["color"] in (0x9333EA, 0x8B5CF6)

    # 4. Push Initiated -> Pink
    embed_push = build_discord_embed(
        event_type="push_initiated",
        media_title="Attack on Titan",
        target_label="Season 2",
        release_name="Attack.on.Titan.S02.1080p.BluRay",
        status_reason="Auto-Push Best dispatched to TorBox",
    )
    assert embed_push["color"] == EVENT_COLORS["push_initiated"]
    assert embed_push["color"] == 0xD40060


@pytest.mark.asyncio
async def test_send_discord_webhook_success_and_http_errors():
    """Verify send_discord_webhook handles 204/200 responses and gracefully returns False on HTTP/network errors."""
    from app.services.discord import build_discord_embed, send_discord_webhook

    embed = build_discord_embed(
        event_type="completed",
        media_title="Inception",
        target_label="Movie",
        release_name="Inception.2010.1080p.BluRay",
    )

    # Empty URL returns False immediately
    assert await send_discord_webhook("", embed=embed) is False

    # 204 No Content -> True
    mock_resp_ok = MagicMock()
    mock_resp_ok.status_code = 204
    mock_resp_ok.raise_for_status = MagicMock()

    with patch("app.services.discord.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp_ok
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        ok = await send_discord_webhook(
            "https://discord.com/api/webhooks/123/abc", embed=embed
        )
        assert ok is True
        mock_client.post.assert_awaited_once()
        call_kwargs = mock_client.post.call_args
        assert call_kwargs[0][0] == "https://discord.com/api/webhooks/123/abc"
        payload = call_kwargs[1]["json"]
        assert "embeds" in payload
        assert len(payload["embeds"]) == 1
        assert payload["embeds"][0]["color"] == 0x10B981

    # HTTP 400 / 500 error -> False without raising
    with patch("app.services.discord.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.HTTPStatusError(
            "Bad Request",
            request=MagicMock(),
            response=MagicMock(status_code=400, text="Invalid webhook"),
        )
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        ok_err = await send_discord_webhook(
            "https://discord.com/api/webhooks/123/abc", embed=embed
        )
        assert ok_err is False

    # Network RequestError -> False without raising
    with patch("app.services.discord.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.RequestError("Connection failed")
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        ok_net = await send_discord_webhook(
            "https://discord.com/api/webhooks/123/abc", embed=embed
        )
        assert ok_net is False


@pytest.mark.asyncio
async def test_unified_notification_dispatcher_per_event_filtering(
    db_session: AsyncSession,
):
    """Verify dispatch_notification_event checks the 4 per-event flags and fans out to Telegram & Discord."""
    from app.services.discord import dispatch_notification_event

    session = db_session

    # Seed SystemSettings with defaults:
    # notify_on_push_initiated=False, notify_on_completed=True, notify_on_failure=True, notify_on_auto_advance=True
    sys_settings = SystemSettings(
        id=1,
        discord_webhook_url="https://discord.com/api/webhooks/999/xyz",
        discord_enabled=True,
        notify_on_push_initiated=False,
        notify_on_completed=True,
        notify_on_failure=True,
        notify_on_auto_advance=True,
    )
    tg_channel = NotificationChannel(
        name="Main Telegram",
        type="telegram",
        bot_token="123456:ABCDEF",
        chat_id="-100987654",
    )
    session.add_all([sys_settings, tg_channel])
    await session.commit()

    with (
        patch(
            "app.services.discord.send_discord_webhook", new_callable=AsyncMock
        ) as mock_discord,
        patch(
            "app.services.telegram.send_notification", new_callable=AsyncMock
        ) as mock_tg,
    ):
        mock_discord.return_value = True
        mock_tg.return_value = True

        # 1. push_initiated is disabled by default -> neither Telegram nor Discord is called
        res_push = await dispatch_notification_event(
            session=session,
            event_type="push_initiated",
            media_title="The Matrix",
            media_year=1999,
            target_label="Movie",
            release_name="The.Matrix.1999.2160p.Remux",
            resolution="2160p",
            source="Remux",
            language="en",
            status_reason="Push initiated to TorBox",
            poster_url="https://image.tmdb.org/t/p/w500/matrix.jpg",
        )
        assert res_push["dispatched"] is False
        mock_discord.assert_not_awaited()
        mock_tg.assert_not_awaited()

        # 2. completed is enabled -> both Telegram and Discord are called!
        res_comp = await dispatch_notification_event(
            session=session,
            event_type="completed",
            media_title="The Matrix",
            media_year=1999,
            target_label="Movie",
            release_name="The.Matrix.1999.2160p.Remux",
            resolution="2160p",
            source="Remux",
            language="en",
            status_reason="Ready on TorBox (Verified)",
            poster_url="https://image.tmdb.org/t/p/w500/matrix.jpg",
        )
        assert res_comp["dispatched"] is True
        assert res_comp["telegram_sent"] == 1
        assert res_comp["discord_sent"] is True
        mock_discord.assert_awaited_once()
        mock_tg.assert_awaited_once()

        # 3. Disable Discord, enable notify_on_push_initiated, disable notify_on_failure
        sys_settings.discord_enabled = False
        sys_settings.notify_on_push_initiated = True
        sys_settings.notify_on_failure = False
        await session.commit()

        mock_discord.reset_mock()
        mock_tg.reset_mock()

        # Now push_initiated fans out ONLY to Telegram
        res_push_enabled = await dispatch_notification_event(
            session=session,
            event_type="push_initiated",
            media_title="The Matrix",
            target_label="Movie",
            release_name="The.Matrix.1999.2160p.Remux",
        )
        assert res_push_enabled["dispatched"] is True
        assert res_push_enabled["telegram_sent"] == 1
        assert res_push_enabled["discord_sent"] is False
        mock_tg.assert_awaited_once()
        mock_discord.assert_not_awaited()

        # And failure is suppressed because notify_on_failure == False
        mock_tg.reset_mock()
        res_fail_disabled = await dispatch_notification_event(
            session=session,
            event_type="failure",
            media_title="The Matrix",
            target_label="Movie",
            release_name="The.Matrix.1999.BAD",
            status_reason="Repair failed",
        )
        assert res_fail_disabled["dispatched"] is False
        mock_tg.assert_not_awaited()
        mock_discord.assert_not_awaited()


@pytest.mark.asyncio
async def test_push_engine_fires_push_initiated_and_auto_advance_events(
    db_session: AsyncSession,
):
    """Verify execute_auto_push triggers push_initiated and auto_advance notifications when enabled."""
    from app.db.models import Provider

    session = db_session

    provider = Provider(name="Simkl", type="simkl", category="watchlist")
    sys_settings = SystemSettings(
        id=1,
        discord_webhook_url="https://discord.com/api/webhooks/999/xyz",
        discord_enabled=True,
        notify_on_push_initiated=True,
        notify_on_auto_advance=True,
    )
    session.add_all([provider, sys_settings])
    await session.flush()

    item = MediaItem(
        provider_id=provider.id,
        title="Frieren",
        media_type=MediaType.ANIME,
        status=MediaStatus.SEARCHING,
        allow_season_packs=True,
        prefer_season_packs=True,
        auto_advance_seasons=True,
        poster_url="https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/frieren.jpg",
    )
    session.add(item)
    await session.flush()

    from datetime import datetime, timezone

    s1 = Season(
        media_item_id=item.id,
        season_number=1,
        title="Frieren Season 1",
        entry_type="season",
        watch_order=1,
        type_number=1,
        monitored=True,
        status=SeasonStatus.SEARCHING,
        air_date=datetime(2023, 9, 29, tzinfo=timezone.utc),
    )
    s2 = Season(
        media_item_id=item.id,
        season_number=2,
        title="Frieren Season 2",
        entry_type="season",
        watch_order=2,
        type_number=2,
        monitored=False,
        status=SeasonStatus.SEARCHING,
        air_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    session.add_all([s1, s2])
    await session.commit()

    valid_nzb = b'<?xml version="1.0" encoding="UTF-8"?><nzb><file subject="Frieren.mkv"><segments><segment bytes="500000000" number="1">msg1</segment></segments></file></nzb>'

    async def fake_search_show(**kwargs):
        s_num = kwargs.get("season")
        return [
            {
                "title": f"Frieren.S0{s_num}.1080p.WEB-DL.H264-SubsPlease",
                "guid": f"guid-frieren-s{s_num}",
                "size": int(8.0 * (1024**3)),
                "api_language": "en",
            }
        ]

    from app.core.push_engine import execute_auto_push

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new_callable=AsyncMock,
            return_value=(valid_nzb, "frieren.nzb"),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new_callable=AsyncMock,
            return_value={"id": "tb-frieren", "hash": "hash-frieren"},
        ),
        patch(
            "app.services.discord.send_discord_webhook",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_discord,
    ):
        result = await execute_auto_push(
            session,
            item.id,
            {"season_ids": [s1.id], "auto_advance_seasons": True},
        )
        assert result["pushed"] is True
        assert result["dispatched_count"] == 2
        # Both push_initiated (for S1) and auto_advance (for S2) should have been sent to Discord
        assert mock_discord.await_count >= 2
        colors_sent = [
            call.kwargs["embed"]["color"] for call in mock_discord.await_args_list
        ]
        assert 0xD40060 in colors_sent  # Push Initiated (Pink)
        assert 0x9333EA in colors_sent  # Auto-Advance (Purple)


@pytest.mark.asyncio
async def test_notification_test_endpoints(
    db_session: AsyncSession,
    async_client: AsyncClient,
):
    """Verify the Telegram and Discord test notification endpoints in app/main.py."""
    session = db_session

    sys_settings = SystemSettings(
        id=1,
        discord_webhook_url="https://discord.com/api/webhooks/test/hook",
        discord_enabled=True,
    )
    tg_channel = NotificationChannel(
        name="Test TG",
        type="telegram",
        bot_token="999:TESTTOKEN",
        chat_id="12345",
    )
    session.add_all([sys_settings, tg_channel])
    await session.commit()

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
        client = async_client
        if True:
            # 1. Test Discord Webhook endpoint
            resp_discord = await client.post(
                "/api/notifications/test/discord",
                data={
                    "discord_webhook_url": "https://discord.com/api/webhooks/override/hook"
                },
            )
            assert resp_discord.status_code == 200
            data_discord = resp_discord.json()
            assert data_discord["ok"] is True
            mock_discord.assert_awaited_once()
            assert (
                mock_discord.call_args[0][0]
                == "https://discord.com/api/webhooks/override/hook"
            )

            # 1b. Test /api/settings/discord/test via HTMX (returns 200 HTML banner & auto-saves webhook URL)
            mock_discord.reset_mock()
            resp_discord_htmx = await client.post(
                "/api/settings/discord/test",
                data={
                    "discord_webhook_url": "https://discord.com/api/webhooks/htmx/saved"
                },
                headers={"HX-Request": "true"},
            )
            assert resp_discord_htmx.status_code == 200
            assert "Discord test notification sent" in resp_discord_htmx.text
            mock_discord.assert_awaited_once()
            await session.refresh(sys_settings)
            assert (
                sys_settings.discord_webhook_url
                == "https://discord.com/api/webhooks/htmx/saved"
            )
            assert sys_settings.discord_enabled is True

            # 2. Test Telegram endpoint
            resp_tg = await client.post(
                "/api/notifications/test/telegram",
                data={"notification_id": str(tg_channel.id)},
            )
            assert resp_tg.status_code == 200
            data_tg = resp_tg.json()
            assert data_tg["ok"] is True
            mock_tg.assert_awaited_once()

            # 3. Save notification settings endpoint
            resp_save = await client.post(
                "/api/notifications/settings",
                data={
                    "discord_webhook_url": "https://discord.com/api/webhooks/new/hook",
                    "discord_enabled": "true",
                    "notify_on_push_initiated": "true",
                    "notify_on_completed": "true",
                    "notify_on_failure": "false",
                    "notify_on_auto_advance": "true",
                },
            )
            assert resp_save.status_code == 200
            await session.refresh(sys_settings)
            assert (
                sys_settings.discord_webhook_url
                == "https://discord.com/api/webhooks/new/hook"
            )
            assert sys_settings.discord_enabled is True
            assert sys_settings.notify_on_push_initiated is True
            assert sys_settings.notify_on_failure is False


def test_outage_events_map_to_notify_on_outages_flag():
    from app.services.discord import EVENT_FLAG_ATTRS, EVENT_HEADERS

    for event in ("outage_paused", "outage_resumed"):
        assert EVENT_FLAG_ATTRS[event] == "notify_on_outages"
    assert EVENT_HEADERS["outage_paused"] == "⏸️ Service Outage — Pushes Paused"
    assert EVENT_HEADERS["outage_resumed"] == "▶️ Service Recovered — Queue Resumed"
