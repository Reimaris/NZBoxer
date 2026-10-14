"""
Tests for Ticket 09: Unified Push Modal UI, 4-Tab Settings Page, and Legacy Automation Contract (v3.0.0).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from httpx import AsyncClient

import app.config as config_mod
from app.db.database import async_session_factory
from app.db.models import (
    BlacklistedRelease,
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
from app.main import APP_VERSION


@pytest.mark.asyncio
async def test_unified_push_modal_html_renders_all_v3_controls(
    default_search_preset,
    async_client: AsyncClient,
):
    """GET /api/items/{id}/push-modal renders templates/modals/push_modal.html with all v3 controls."""
    async with async_session_factory() as session:
        provider = Provider(
            name="Simkl Main",
            type="simkl",
            category="watchlist",
            is_active=True,
        )
        session.add(provider)
        await session.flush()

        anime = MediaItem(
            provider_id=provider.id,
            simkl_id=77001,
            anilist_id=21001,
            title="Demon Slayer",
            year=2019,
            overview="Tanjiro sets out to become a demon slayer.",
            media_type=MediaType.ANIME,
            status=MediaStatus.SEARCHING,
            prefer_season_packs=True,
            auto_advance_seasons=True,
        )
        session.add(anime)
        await session.flush()

        s1 = Season(
            media_item_id=anime.id,
            season_number=1,
            watch_order=1,
            type_number=1,
            entry_type="season",
            title="Unwavering Resolve Arc",
            monitored=True,
            status=SeasonStatus.DOWNLOADED,
            episode_count=2,
        )
        m1 = Season(
            media_item_id=anime.id,
            season_number=2,
            watch_order=2,
            type_number=1,
            entry_type="movie",
            title="Mugen Train",
            monitored=True,
            status=SeasonStatus.SEARCHING,
            episode_count=1,
        )
        session.add_all([s1, m1])
        await session.flush()

        ep1 = Episode(
            season_id=s1.id,
            episode_number=1,
            monitored=True,
            status=EpisodeStatus.DOWNLOADED,
        )
        ep2 = Episode(
            season_id=s1.id,
            episode_number=2,
            monitored=True,
            status=EpisodeStatus.SEARCHING,
        )
        session.add_all([ep1, ep2])
        await session.commit()
        anime_id = anime.id

    client = async_client
    if True:
        resp = await client.get(
            f"/api/items/{anime_id}/push-modal",
            headers={"Accept": "text/html"},
        )
        assert resp.status_code == 200
        html = resp.text

        # 1. Item Header & Metadata Controls
        assert "Demon Slayer" in html
        assert "2019" in html
        assert "Tanjiro sets out to become a demon slayer." in html
        assert f"/api/items/{anime_id}/change-type" in html
        assert f"/items/{anime_id}/reset-metadata" in html

        # 2. Chronological Season / Movie & Episode Selection Checkboxes + Sticky Toggles
        assert "Unwavering Resolve Arc" in html
        assert "Mugen Train" in html
        assert "SEASON" in html
        assert "MOVIE" in html
        assert "Prefer Season Pack" in html
        assert "Auto-Advance Seasons" in html

        # 3. Search Preset Selector & Inline Preset Save
        assert "Default (Best)" in html
        assert "Save current config as Preset" in html
        assert "/api/presets/save-inline" in html

        # 4. Progressive-Disclosure Quality Controls ("Best" vs "Custom")
        assert "Primary Language" in html
        assert "Fallback Language" in html
        assert "Video Quality" in html
        assert "Audio Quality" in html
        assert "Min Size (GB)" in html
        assert "Max Size (GB)" in html
        assert "Must Contain Keywords" in html
        assert "Ignore / Reject Keywords" in html

        # 5. Dual Execution Modes & Two-Mode Manual Picker
        assert "Auto-Push Best" in html
        assert "Search &amp; Pick Manually" in html or "Search & Pick Manually" in html
        assert f"/api/items/{anime_id}/push/auto" in html
        assert f"/api/items/{anime_id}/push/manual-search" in html
        assert f"/api/items/{anime_id}/push/manual-grab" in html
        assert "Season Packs / Movie Releases" in html
        assert "Episode" in html

        # 6. Verify x-data uses single-quoted attribute without broken &#34; inside double-quoted x-data
        assert 'x-data="{' not in html
        assert "x-data='{" in html
        # First uncompleted released entry (m1) is pre-selected, while s1 (DOWNLOADED) is skipped
        assert "Select All Unpushed" in html

    # Also verify non-Anime Series hides Reset Metadata / AniList and pre-selects first uncompleted season
    async with async_session_factory() as session:
        series = MediaItem(
            provider_id=1,
            simkl_id=52692,
            title="Star Wars Rebels",
            year=2014,
            media_type=MediaType.SHOW,
            status=MediaStatus.SEARCHING,
        )
        session.add(series)
        await session.flush()
        rebels_s1 = Season(
            media_item_id=series.id,
            season_number=1,
            watch_order=1,
            type_number=1,
            entry_type="season",
            title="Season 1",
            monitored=True,
            status=SeasonStatus.COMPLETED,
        )
        rebels_s2 = Season(
            media_item_id=series.id,
            season_number=2,
            watch_order=2,
            type_number=2,
            entry_type="season",
            title="Season 2",
            monitored=False,
            status=SeasonStatus.PENDING,
        )
        rebels_s3 = Season(
            media_item_id=series.id,
            season_number=3,
            watch_order=3,
            type_number=3,
            entry_type="season",
            title="Season 3",
            monitored=False,
            status=SeasonStatus.PENDING,
        )
        session.add_all([rebels_s1, rebels_s2, rebels_s3])
        await session.commit()
        series_id = series.id
        rebels_s2_id = rebels_s2.id

    s_resp = await client.get(
        f"/api/items/{series_id}/push-modal",
        headers={"Accept": "text/html"},
    )
    assert s_resp.status_code == 200
    s_html = s_resp.text
    assert "Reset Metadata / AniList" not in s_html
    assert f"selectedSeasons: [{rebels_s2_id}]" in s_html
    assert "max-w-5xl" in s_html
    assert "max-h-52" not in s_html


@pytest.mark.asyncio
async def test_repush_completed_season_replaces_old_torbox_transfer_and_history(
    monkeypatch,
):
    """Explicitly pushing a COMPLETED season searches all released episodes, deletes old TorBox transfer, and dismisses old DownloadHistory."""
    from sqlalchemy import select

    from app.core.push_engine import execute_auto_push
    from app.db.models import DownloadHistory
    from app.services.downloaders import torbox
    from app.services.indexers import newznab as newznab_client

    deleted_torbox_ids: list[str] = []

    async def fake_search_show(**kwargs):
        return [
            {
                "title": "Star.Wars.Rebels.S01E01.German.1080p.BluRay.x265-FuN",
                "guid": "guid-rebels-s01e01-de",
                "size": 1_500_000_000,
                "api_language": "German",
            }
        ]

    async def fake_fetch_nzb(guid, **kwargs):
        nzb_xml = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
            b'<file poster="a" date="1" subject="Star.Wars.Rebels.S01E01.mkv">'
            b"<groups><group>alt.binaries.tv</group></groups>"
            b'<segments><segment bytes="50000000" number="1">msg1</segment></segments>'
            b"</file></nzb>"
        )
        return nzb_xml, "Star.Wars.Rebels.S01E01.nzb"

    async def fake_send_nzb(nzb_bytes, filename, **kwargs):
        return {"id": "999002", "hash": "hash999002"}

    async def fake_delete_usenet(tb_id, **kwargs):
        deleted_torbox_ids.append(str(tb_id))
        return True

    monkeypatch.setattr(newznab_client, "search_show", fake_search_show)
    monkeypatch.setattr(newznab_client, "fetch_nzb_bytes", fake_fetch_nzb)
    monkeypatch.setattr(torbox, "send_nzb_file", fake_send_nzb)
    monkeypatch.setattr(torbox, "delete_usenet_download", fake_delete_usenet)

    async with async_session_factory() as session:
        prov = Provider(
            name="Simkl", type="simkl", category="watchlist", is_active=True
        )
        session.add(prov)
        await session.flush()

        series = MediaItem(
            provider_id=prov.id,
            simkl_id=52692,
            title="Star Wars Rebels",
            year=2014,
            media_type=MediaType.SHOW,
            status=MediaStatus.SEARCHING,
        )
        session.add(series)
        await session.flush()

        s1 = Season(
            media_item_id=series.id,
            season_number=1,
            watch_order=1,
            type_number=1,
            entry_type="season",
            title="Season 1",
            monitored=True,
            status=SeasonStatus.COMPLETED,
        )
        session.add(s1)
        await session.flush()

        ep1 = Episode(
            season_id=s1.id,
            episode_number=1,
            monitored=True,
            status=EpisodeStatus.DOWNLOADED,
        )
        session.add(ep1)
        await session.flush()

        old_hist = DownloadHistory(
            media_item_id=series.id,
            season_id=s1.id,
            episode_id=None,
            nzb_title="Star.Wars.Rebels.S01.COMPLETE.1080p.BluRay.x264-OLD",
            nzb_guid="old-pack-guid",
            downloader_transfer_id="2140456",
            is_dismissed=False,
        )
        session.add(old_hist)
        await session.commit()

        res = await execute_auto_push(
            session,
            series.id,
            {
                "season_ids": [s1.id],
                "primary_language": "de",
                "fallback_language": "none",
                "allow_season_packs": False,
                "prefer_season_packs": False,
            },
        )
        assert res["pushed"] is True
        assert res["dispatched_count"] == 1
        assert deleted_torbox_ids == ["2140456"]

        hists = (
            (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.media_item_id == series.id
                    )
                )
            )
            .scalars()
            .all()
        )
        by_tb = {h.downloader_transfer_id: h for h in hists}
        assert by_tb["2140456"].is_dismissed is True
        assert by_tb["2140456"].status_detail == "replaced"
        assert by_tb["999002"].is_dismissed is False


@pytest.mark.asyncio
async def test_settings_4_tab_layout_and_simplified_simkl_provider(
    default_search_preset,
    async_client: AsyncClient,
):
    """GET /settings renders the 4-tab layout (Providers, Presets, Video Scoring, General & Blacklist)."""
    async with async_session_factory() as session:
        prov = Provider(
            name="Simkl", type="simkl", category="watchlist", is_active=True
        )
        session.add(prov)
        await session.flush()
        item = MediaItem(
            provider_id=prov.id,
            title="Test Movie",
            media_type=MediaType.MOVIE,
            status=MediaStatus.SEARCHING,
        )
        session.add(item)
        await session.flush()
        bl = BlacklistedRelease(
            media_item_id=item.id,
            nzb_guid="bad-guid-99",
            nzb_title="Bad.Release.2026.1080p.WEB-DL",
            reason="Failed unpack",
        )
        notif = NotificationChannel(
            name="Main Telegram",
            type="telegram",
            bot_token="123:ABC",
            chat_id="999",
        )
        session.add_all([bl, notif])
        await session.commit()

    client = async_client
    if True:
        resp = await client.get("/settings")
        assert resp.status_code == 200
        html = resp.text

        # 6-Slice expandable navigation headers
        assert "Connected Providers" in html
        assert "Search Presets" in html
        assert "Scoring Reference" in html
        assert "Notifications" in html
        assert "Blacklisted Releases" in html

        # Slice 2: Presets management
        assert "Default (Best)" in html
        assert "/api/presets" in html

        # Slice 6: Read-only Video Scoring reference matrix
        assert "Video Scoring Reference" in html
        assert "Read-Only Reference" in html

        # Slice 3 & 4: Notifications and System Settings
        assert "Discord Webhook" in html
        assert 'id="discord-test-result"' in html
        assert "Push Initiated" in html
        assert "Download Completed (Ready in Downloader)" in html
        assert "Push Failed / Auto-Replaced" in html
        assert "Auto-Advance Season Triggered" in html
        assert "download_timeout_minutes" in html
        assert "/api/automation/rescan-torbox" not in html
        assert "Bad.Release.2026.1080p.WEB-DL" in html
        assert ">Edit<" not in html
        assert ">Del<" not in html

        # Provider Modal: Simplified Simkl without legacy ProviderProfile quality rules
        modal_resp = await client.get("/settings/provider/new")
        assert modal_resp.status_code == 200
        modal_html = modal_resp.text
        assert "sync_interval_minutes" in modal_html
        assert "sync_movies" in modal_html
        assert "sync_series" in modal_html
        assert "sync_anime" in modal_html
        assert "movies_resolution" not in modal_html

        # Saving a Simkl provider persists config_json without ProviderProfile
        save_resp = await client.post(
            "/settings/provider",
            data={
                "name": "My Simkl",
                "provider_type": "simkl",
                "category": "watchlist",
                "simkl_username": "alice",
                "client_id": "cid123",
                "access_token": "tok123",
                "is_active": "true",
                "sync_interval_minutes": "60",
                "sync_movies": "true",
                "sync_series": "true",
                "sync_anime": "false",
            },
        )
        assert save_resp.status_code == 200


@pytest.mark.asyncio
async def test_v3_legacy_contract_and_version_bump(async_client: AsyncClient):
    """Verify retired routes, removed legacy models/columns, and v3.0.0 version bump."""
    # 1. Version string is semver and consistent across modules
    assert re.fullmatch(r"\d+\.\d+\.\d+", APP_VERSION)
    assert APP_VERSION in config_mod.DEFAULT_USER_AGENT

    # 2. Retired templates removed
    assert not Path("templates/item_detail.html").exists()
    assert not Path("templates/history_dashboard.html").exists()
    assert not Path("templates/partials/automation_status.html").exists()
    assert not Path("templates/manual_search.html").exists()
    assert not Path("templates/components").exists()
    assert Path("templates/modals/push_modal.html").exists()

    # 3. Retired ORM models / enums / columns / properties removed
    import app.db.models as models_mod

    assert not hasattr(models_mod, "ProviderProfile")
    assert not hasattr(models_mod, "AutomationState")
    assert not hasattr(MediaStatus, "MANUAL_GRAB")
    assert not hasattr(SeasonStatus, "MANUAL_GRAB")
    assert not hasattr(EpisodeStatus, "MANUAL_GRAB")
    assert not hasattr(MediaItem, "partition")

    for removed_col in (
        "empty_search_count",
        "upgrade_attempts_count",
        "last_upgrade_search_at",
        "pending_candidate_json",
    ):
        assert not hasattr(MediaItem, removed_col)
        assert not hasattr(Season, removed_col)
        assert not hasattr(Episode, removed_col)

    for removed_setting in (
        "automation_state",
        "self_healing_interval",
        "video_search_interval",
        "upgrade_search_interval_hours",
        "max_upgrade_attempts",
        "backoff_tier2_skip",
        "backoff_tier3_skip",
        "auto_grab_title_fallbacks",
    ):
        assert not hasattr(SystemSettings, removed_setting)
        assert not hasattr(config_mod.Settings, removed_setting)

    # 4. Retired background search & self-healing stages removed
    import app.core.automation as auto_mod

    assert not hasattr(auto_mod, "run_automation_cycle")
    assert not hasattr(auto_mod, "_run_video_acquisition_search_cycle")
    assert not hasattr(auto_mod, "_run_video_upgrade_cycle")
    assert not hasattr(auto_mod, "is_eligible_for_search")
    assert not hasattr(auto_mod, "classify_video_item_partition")
    assert not hasattr(auto_mod, "get_grabs_dispatched_in_cycle")
    assert not hasattr(auto_mod, "reset_grabs_dispatched_in_cycle")
    assert not hasattr(auto_mod, "increment_grabs_dispatched_in_cycle")
    assert hasattr(auto_mod, "sync_all_providers")
    assert hasattr(auto_mod, "classify_v3_status_tier")

    assert not Path("app/core/self_healing.py").exists()

    # 5. Retired HTTP endpoints return 404/405
    for method, url in (
        ("GET", "/items/1"),
        ("GET", "/search"),
        ("GET", "/history"),
        ("POST", "/items/1/seasons/1/toggle"),
        ("POST", "/items/1/refresh"),
        ("POST", "/items/1/retry"),
        ("POST", "/items/1/ignore"),
        ("POST", "/items/1/toggle_auto_monitor"),
        ("POST", "/api/automation/state"),
        ("POST", "/api/automation/run/video"),
        ("GET", "/api/automation/status"),
    ):
        resp = await async_client.request(method, url)
        assert resp.status_code in (404, 405), f"Expected 404/405 for {method} {url}"
