"""
Tests for Milestone m23_v3.0.0-legacy-v2-purge-and-db-reset (ADR-089):
1. TorBox 'expired' transfer state handling in TransferPoller, get_active_pushes, and get_push_history_ledger.
2. Automatic DROP TABLE IF EXISTS of all 8 retired v2 tables during init_db().
3. Complete purge of retired v2 modules, ORM models, columns, functions, routes, and UI controls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import app.config as config_mod
import app.core.automation as auto_mod
import app.db.database as db_mod
import app.db.models as models_mod
import app.services.downloaders.torbox as torbox_mod
import app.services.indexers.newznab as tm_mod
import app.services.watchlists.simkl as simkl_mod
from app.core.transfer_poller import (
    get_active_pushes,
    get_push_history_ledger,
    run_transfer_poller_tick,
)
from app.db.models import (
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    MediaType,
    Provider,
    SeasonStatus,
    SystemSettings,
)
from tests.conftest import make_history, make_movie, make_series

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_torbox_expired_transfer_state_transitions_to_deleted_and_completed(
    db_session: AsyncSession,
) -> None:
    """When TorBox reports download_state='expired', TransferPoller marks history 'deleted' (100%)
    and transitions DOWNLOADING targets (Movie, Season + aired Episodes) to COMPLETED."""
    now = datetime.now(timezone.utc)

    movie = await make_movie(
        db_session,
        title="Expired Cloud Movie",
        year=2025,
        simkl_id=91001,
        status=MediaStatus.DOWNLOADING,
    )
    hist_movie = await make_history(
        db_session,
        item=movie,
        nzb_title="Expired.Cloud.Movie.2025.2160p.WEB-DL.DDP5.1.H.265-GRP",
        downloader_transfer_id="tb-exp-movie-1",
        status_detail="downloading",
        progress_pct=45.0,
        download_speed_bytes=12_000_000,
        eta_seconds=300,
        is_dismissed=False,
    )

    series, seasons, episodes = await make_series(
        db_session,
        title="Expired Cloud Series",
        media_type=MediaType.SHOW,
        year=2024,
        seasons=[
            {
                "season_number": 1,
                "watch_order": 1,
                "type_number": 1,
                "entry_type": "season",
                "status": SeasonStatus.DOWNLOADING,
                "monitored": True,
            }
        ],
        episodes_per_season=2,
        episode_statuses=[EpisodeStatus.DOWNLOADING, EpisodeStatus.FUTURE],
    )
    episodes[0].air_date = now - timedelta(days=10)
    episodes[1].air_date = now + timedelta(days=10)
    season1 = seasons[0]

    hist_season = await make_history(
        db_session,
        item=series,
        season=season1,
        nzb_title="Expired.Cloud.Series.S01.1080p.WEB-DL.H.264-GRP",
        downloader_transfer_id="tb-exp-season-1",
        status_detail="downloading",
        progress_pct=80.0,
        download_speed_bytes=8_000_000,
        eta_seconds=120,
        is_dismissed=False,
    )

    # Also seed a historical row whose status_detail was literally stored as "expired"
    hist_raw_expired = await make_history(
        db_session,
        item=movie,
        nzb_title="Old.Expired.Row.2024.1080p.WEB-DL",
        downloader_transfer_id="tb-exp-legacy-row",
        status_detail="expired",
        progress_pct=0.0,
        is_dismissed=False,
    )
    await db_session.commit()

    # Before tick: hist_raw_expired with status_detail="expired" must NOT appear in active pushes
    active_before = await get_active_pushes(db_session)
    active_history_ids_before = {row["history_id"] for row in active_before}
    assert hist_raw_expired.id not in active_history_ids_before
    assert hist_movie.id in active_history_ids_before
    assert hist_season.id in active_history_ids_before

    mock_torbox_list = [
        {
            "id": "tb-exp-movie-1",
            "name": "Expired.Cloud.Movie.2025.2160p.WEB-DL.DDP5.1.H.265-GRP",
            "download_state": "expired",
            "progress": 0.0,
            "download_speed": 0,
            "eta": 0,
            "files": [],
        },
        {
            "id": "tb-exp-season-1",
            "name": "Expired.Cloud.Series.S01.1080p.WEB-DL.H.264-GRP",
            "download_state": "expired (files aged out)",
            "progress": 0.0,
            "download_speed": 0,
            "eta": 0,
            "files": [],
        },
    ]

    with patch(
        "app.services.downloaders.torbox.get_usenet_downloads",
        new=AsyncMock(return_value=mock_torbox_list),
    ):
        await run_transfer_poller_tick(db_session)

    await db_session.refresh(hist_movie)
    await db_session.refresh(hist_season)
    await db_session.refresh(movie)
    await db_session.refresh(season1)
    await db_session.refresh(episodes[0])
    await db_session.refresh(episodes[1])

    assert hist_movie.status_detail == "deleted"
    assert hist_movie.progress_pct == 100.0
    assert hist_movie.download_speed_bytes == 0
    assert hist_movie.eta_seconds == 0
    assert hist_movie.notification_sent is True
    assert movie.status == MediaStatus.COMPLETED

    assert hist_season.status_detail == "deleted"
    assert hist_season.progress_pct == 100.0
    assert hist_season.download_speed_bytes == 0
    assert hist_season.eta_seconds == 0
    assert hist_season.notification_sent is True
    assert season1.status == SeasonStatus.COMPLETED
    assert episodes[0].status == EpisodeStatus.COMPLETED
    assert episodes[1].status == EpisodeStatus.FUTURE

    # After tick: no active pushes remain
    active_after = await get_active_pushes(db_session)
    assert active_after == []

    # Push History Ledger maps both "deleted" and "expired" to DELETED / deleted
    ledger = await get_push_history_ledger(db_session)
    by_id = {entry["id"]: entry for entry in ledger}
    for hid in (hist_movie.id, hist_season.id, hist_raw_expired.id):
        assert by_id[hid]["downloader_status"] == "DELETED"
        assert by_id[hid]["status_key"] == "deleted"
        assert by_id[hid]["can_delete_transfer"] is False


@pytest.mark.asyncio
async def test_init_db_drops_all_legacy_v2_tables(
    tmp_path: Path,
) -> None:
    """init_db() detects a v2 database, creates a .v2_backup file, drops all 8 retired v2 tables,
    resets v2 media/history rows, and preserves providers (with Simkl last_synced_at cleared),
    system_settings, and notification_channels in a clean v3 schema."""
    import json
    import sqlite3

    db_file = tmp_path / "legacy_upgrade_test.db"
    retired_tables = (
        "seen_torbox_downloads",
        "daily_grab_counters",
        "provider_profiles",
        "manga_items",
        "manga_volumes",
        "book_items",
        "magazine_subscriptions",
        "magazine_issues",
    )
    conn = sqlite3.connect(db_file)
    for tbl in retired_tables:
        conn.execute(f"CREATE TABLE {tbl} (id INTEGER PRIMARY KEY)")
        conn.execute(f"INSERT INTO {tbl} (id) VALUES (1)")

    conn.executescript(
        """
        CREATE TABLE system_settings (
            id INTEGER PRIMARY KEY,
            treasure_maps_api_key VARCHAR(200) NOT NULL DEFAULT '',
            torbox_api_key VARCHAR(200) NOT NULL DEFAULT '',
            tmdb_api_key VARCHAR(200) NOT NULL DEFAULT '',
            scan_interval_multiplier INTEGER NOT NULL DEFAULT 1,
            sh_max_retries INTEGER NOT NULL DEFAULT 5,
            sh_max_time_hours FLOAT NOT NULL DEFAULT 12.0,
            sh_auto_retry BOOLEAN NOT NULL DEFAULT 1,
            sh_retry_wait_hours FLOAT NOT NULL DEFAULT 24.0,
            dry_run BOOLEAN NOT NULL DEFAULT 0,
            automation_state VARCHAR(50) NOT NULL DEFAULT 'ACTIVE',
            download_timeout_hours INTEGER NOT NULL DEFAULT 36
        );
        INSERT INTO system_settings (id, sh_max_retries, download_timeout_hours)
        VALUES (1, 5, 36);

        CREATE TABLE providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type VARCHAR(50) NOT NULL,
            name VARCHAR(200) NOT NULL,
            username VARCHAR(200),
            client_id VARCHAR(500),
            access_token VARCHAR(500),
            movie_category_id INTEGER,
            series_category_id INTEGER,
            anime_category_id INTEGER,
            bandwidth_mbit INTEGER,
            category VARCHAR(50) NOT NULL DEFAULT 'watchlist',
            api_key VARCHAR(500),
            api_url VARCHAR(500),
            priority INTEGER NOT NULL DEFAULT 1,
            is_active BOOLEAN NOT NULL DEFAULT 1,
            config_json TEXT NOT NULL DEFAULT '{}'
        );
        INSERT INTO providers (id, type, name, category, client_id, access_token, config_json)
        VALUES (1, 'simkl', 'Simkl', 'watchlist', 'cid-123', 'tok-456', '{"sync_interval_minutes": 60, "last_synced_at": "2026-09-25T10:00:00Z", "refresh_token": "ref-789"}');
        INSERT INTO providers (id, type, name, category, api_key, api_url, priority)
        VALUES (2, 'treasure_maps', 'Legacy Indexer', 'indexer', 'idx-key', 'https://api.treasuremaps.net/api', 1);
        INSERT INTO providers (id, type, name, category, api_key)
        VALUES (3, 'torbox', 'TorBox', 'downloader', 'tb-key-999');
        INSERT INTO providers (id, type, name, category, api_key)
        VALUES (4, 'hardcover', 'Hardcover Books', 'print_media', 'hc-key');

        CREATE TABLE notification_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type VARCHAR(50) NOT NULL,
            name VARCHAR(200) NOT NULL,
            bot_token VARCHAR(500),
            chat_id VARCHAR(200)
        );
        INSERT INTO notification_channels (id, type, name, bot_token, chat_id)
        VALUES (1, 'telegram', 'Main Telegram', 'bot123:abc', '-100999');

        CREATE TABLE media_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL DEFAULT 1,
            simkl_id INTEGER NOT NULL DEFAULT 100,
            title VARCHAR(500) NOT NULL,
            media_type VARCHAR(20) NOT NULL DEFAULT 'MOVIE',
            status VARCHAR(20) NOT NULL DEFAULT 'DOWNLOADING'
        );
        INSERT INTO media_items (id, provider_id, simkl_id, title, media_type, status)
        VALUES (1, 1, 5555, 'Stale v2 Adopted Movie', 'MOVIE', 'DOWNLOADING');

        CREATE TABLE download_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_item_id INTEGER NOT NULL,
            nzb_title VARCHAR(1000) NOT NULL,
            torbox_id VARCHAR(200)
        );
        INSERT INTO download_history (id, media_item_id, nzb_title, torbox_id)
        VALUES (1, 1, 'Stale.v2.Movie.1080p', 'tb-stale-1');
        """
    )
    conn.commit()
    conn.close()

    await db_mod.init_db(f"sqlite+aiosqlite:///{db_file}")
    await db_mod.close_db()

    # 1. Backup file was automatically created
    backup_file = tmp_path / "legacy_upgrade_test.db.v2_backup"
    assert backup_file.is_file()

    conn = sqlite3.connect(db_file)
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('seen_torbox_downloads', 'daily_grab_counters', "
        "'provider_profiles', 'manga_items', 'manga_volumes', "
        "'book_items', 'magazine_subscriptions', 'magazine_issues')"
    )
    existing_after = [row[0] for row in cur.fetchall()]
    assert existing_after == []

    # 2. v2 media_items and download_history were wiped by the soft reset
    assert conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM download_history").fetchone()[0] == 0

    # 3. Preserved providers, settings, notification_channels, and default SearchPreset
    prov_rows = conn.execute(
        "SELECT id, type, category, api_key, api_url, client_id, access_token, config_json "
        "FROM providers ORDER BY id ASC"
    ).fetchall()
    assert len(prov_rows) == 3  # hardcover print provider was stripped
    assert prov_rows[0][1] == "simkl"
    assert prov_rows[0][5] == "cid-123"
    assert prov_rows[0][6] == "tok-456"
    simkl_cfg = json.loads(prov_rows[0][7])
    assert "last_synced_at" not in simkl_cfg
    assert simkl_cfg["refresh_token"] == "ref-789"
    assert prov_rows[1][1] == "newznab"
    assert prov_rows[1][4] == "https://api.treasure-maps.com/api"
    assert prov_rows[2][1] == "torbox"
    assert prov_rows[2][3] == "tb-key-999"

    # Legacy columns on system_settings and providers were cleanly removed
    sys_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(system_settings)").fetchall()
    }
    assert "treasure_maps_api_key" not in sys_cols
    assert "automation_state" not in sys_cols
    sys_row = conn.execute(
        "SELECT sh_max_retries, download_timeout_minutes FROM system_settings WHERE id = 1"
    ).fetchone()
    # 36 hours legacy (> 4h) is migrated to default 60m
    assert sys_row == (5, 60)

    chan_row = conn.execute(
        "SELECT type, name, bot_token, chat_id FROM notification_channels WHERE id = 1"
    ).fetchone()
    assert chan_row == ("telegram", "Main Telegram", "bot123:abc", "-100999")

    preset_row = conn.execute("SELECT name, is_default FROM search_presets").fetchone()
    assert preset_row == ("Default (Best)", 1)

    # Seed a v3 media item and verify a subsequent init_db() restart does NOT wipe v3 data
    conn.execute(
        "INSERT INTO media_items (id, provider_id, simkl_id, title, media_type, is_anime_movie, status, "
        "allow_season_packs, prefer_season_packs, auto_advance_seasons, last_watched_order, "
        "watched_completed, fail_count) "
        "VALUES (10, 1, 7777, 'Fresh v3 Movie', 'MOVIE', 0, 'SEARCHING', 0, 0, 0, 0, 0, 0)"
    )
    conn.commit()
    conn.close()

    await db_mod.init_db(f"sqlite+aiosqlite:///{db_file}")
    await db_mod.close_db()

    conn = sqlite3.connect(db_file)
    assert conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0] == 1
    conn.close()

    # Re-init in-memory DB so subsequent fixtures remain clean
    await db_mod.init_db("sqlite+aiosqlite:///:memory:")


@pytest.mark.asyncio
async def test_v3_legacy_modules_models_columns_functions_and_routes_purged(
    async_client: AsyncClient,
) -> None:
    """Verify 100% removal of legacy v2 modules, models, columns, functions, routes, and UI buttons."""
    # 1. Deleted app modules and obsolete v2 test files
    for rel_path in (
        "app/core/self_healing.py",
        "app/db/grab_tracker.py",
        "app/core/scheduler_utils.py",
        "tests/test_seen_torbox_model.py",
        "tests/test_torbox_cache_engine.py",
        "tests/test_torbox_rescan_endpoint.py",
        "tests/test_video_auto_adoption.py",
        "tests/test_rate_limits.py",
        "tests/test_metadata_refresh.py",
        "tests/test_metadata_refresh_eligibility.py",
        "tests/test_anime_watchlist_guard.py",
    ):
        assert not (PROJECT_ROOT / rel_path).exists(), f"{rel_path} should be deleted"

    # 2. Deleted ORM models
    assert not hasattr(models_mod, "SeenTorboxDownload")
    assert not hasattr(models_mod, "DailyGrabCounter")

    # 3. Deleted SystemSettings & Settings fields
    for col in (
        "treasure_maps_api_key",
        "torbox_api_key",
        "tmdb_api_key",
        "scan_interval_multiplier",
        "sh_max_time_hours",
        "sh_auto_retry",
        "sh_retry_wait_hours",
        "upgrade_threshold",
    ):
        assert not hasattr(SystemSettings, col), (
            f"SystemSettings.{col} should be removed"
        )
        assert not hasattr(config_mod.Settings, col), (
            f"Settings.{col} should be removed"
        )

    # 4. Deleted Provider columns
    for col in (
        "movie_category_id",
        "series_category_id",
        "anime_category_id",
        "bandwidth_mbit",
    ):
        assert not hasattr(Provider, col), f"Provider.{col} should be removed"

    # 5. Deleted MediaItem column & property
    assert not hasattr(MediaItem, "auto_monitor_next_season")
    assert not hasattr(MediaItem, "is_fully_completed")

    # 6. Deleted dead functions in automation, simkl, torbox, newznab_client, fake_detector
    for fn in (
        "_tick_cooling_indexers",
        "reset_tick_cooling_indexers",
        "mark_indexer_cooling",
        "is_indexer_cooling",
        "is_eligible_for_metadata_refresh",
        "sync_simkl_watchlist",
        "_refresh_series_metadata",
        "run_video_metadata_refresh_cycle",
        "_get_anime_aliases",
    ):
        assert not hasattr(auto_mod, fn), f"automation.{fn} should be removed"

    for fn in ("request_pin", "check_pin"):
        assert not hasattr(simkl_mod, fn), f"simkl.{fn} should be removed"

    for fn in ("send_nzb_link", "check_download_status", "download_file_payload"):
        assert not hasattr(torbox_mod, fn), f"torbox.{fn} should be removed"

    assert not hasattr(tm_mod, "get_download_url")

    # 7. Retired routes return 404/405
    for method, url in (
        ("POST", "/api/automation/rescan-torbox"),
        ("GET", "/api/status"),
        ("POST", "/simkl/auth/pin"),
    ):
        resp = await async_client.request(method, url)
        assert resp.status_code in (404, 405), f"{method} {url} should be retired"

    # 8. Settings page and Provider modal clean of legacy v2 elements
    settings_resp = await async_client.get("/settings")
    assert settings_resp.status_code == 200
    settings_html = settings_resp.text
    assert "Sync External TorBox Library" not in settings_html
    assert "/api/automation/rescan-torbox" not in settings_html

    provider_modal_resp = await async_client.get("/settings/provider/new")
    assert provider_modal_resp.status_code == 200
    modal_html = provider_modal_resp.text
    assert "movie_category_id" not in modal_html
    assert "series_category_id" not in modal_html
    assert "anime_category_id" not in modal_html
    assert "/simkl/auth/start" in modal_html
    assert "/simkl/auth/pin" not in modal_html
