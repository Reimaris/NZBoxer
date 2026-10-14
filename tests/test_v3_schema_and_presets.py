from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

import app.db.models as models
from app.db.database import close_db, get_session_factory, init_db
from app.db.models import (
    DownloadHistory,
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    MediaType,
    Provider,
    Season,
    SeasonStatus,
    SystemSettings,
)


def test_failed_status_enums_exist() -> None:
    """Verify FAILED status enum value is present on MediaStatus, SeasonStatus, and EpisodeStatus."""
    assert hasattr(MediaStatus, "FAILED")
    assert MediaStatus.FAILED.value == "failed"

    assert hasattr(SeasonStatus, "FAILED")
    assert SeasonStatus.FAILED.value == "failed"

    assert hasattr(EpisodeStatus, "FAILED")
    assert EpisodeStatus.FAILED.value == "failed"


@pytest.mark.anyio
async def test_v3_models_and_columns_persistence(db_session: AsyncSession) -> None:
    """Verify SearchPreset and all new v3.0.0 columns on MediaItem, Season, DownloadHistory, SystemSettings, and Provider."""
    assert hasattr(models, "SearchPreset"), "SearchPreset model must exist"
    SearchPreset = models.SearchPreset

    custom_cfg = {
        "resolutions": ["2160p", "1080p"],
        "sources": ["remux", "bluray"],
        "video_codecs": ["hevc", "av1"],
        "min_size_gb": 2.5,
        "max_size_gb": 45.0,
        "required_keywords": ["HDR"],
        "excluded_keywords": ["CAM"],
        "audio_formats": ["atmos", "truehd"],
    }
    preset = SearchPreset(
        name="4K Remux Atmos",
        is_default=True,
        primary_language="en",
        fallback_language="de",
        video_quality_mode="custom",
        audio_quality_mode="custom",
        custom_config_json=json.dumps(custom_cfg),
    )
    db_session.add(preset)
    await db_session.flush()

    assert preset.id is not None
    assert preset.custom_config == custom_cfg

    # Provider config_json
    provider = Provider(
        name="Simkl Main",
        type="simkl",
        category="watchlist",
        config_json=json.dumps(
            {
                "sync_interval_minutes": 60,
                "sync_movies": True,
                "sync_series": True,
                "sync_anime": False,
                "last_synced_at": "2026-09-30T12:00:00Z",
            }
        ),
    )
    db_session.add(provider)
    await db_session.flush()

    # MediaItem new sticky & Simkl progress fields
    item = MediaItem(
        title="Frieren: Beyond Journey's End",
        year=2023,
        media_type=MediaType.ANIME,
        status=MediaStatus.FAILED,
        preset_id=preset.id,
        custom_search_config_json=json.dumps({"min_size_gb": 1.0}),
        prefer_season_packs=True,
        auto_advance_seasons=True,
        last_watched_order=2,
        watched_completed=False,
        provider_id=provider.id,
    )
    db_session.add(item)
    await db_session.flush()

    # Season new chronological franchise fields
    season_entry = Season(
        media_item_id=item.id,
        season_number=2,
        title="Frieren Movie",
        status=SeasonStatus.FAILED,
        entry_type="movie",
        watch_order=2,
        type_number=1,
    )
    db_session.add(season_entry)
    await db_session.flush()

    # DownloadHistory new live transfer & push mode fields
    history = DownloadHistory(
        media_item_id=item.id,
        season_id=season_entry.id,
        nzb_title="Frieren.Movie.1080p.BluRay.x265",
        push_mode="manual",
        progress_pct=64.5,
        download_speed_bytes=15_728_640,
        eta_seconds=180,
        status_detail="Downloading from TorBox",
        is_dismissed=False,
        unpack_started_at=None,
    )
    db_session.add(history)

    # SystemSettings new Discord & per-event notification fields
    sys_settings = SystemSettings(
        id=1,
        discord_webhook_url="https://discord.com/api/webhooks/123/abc",
        discord_enabled=True,
        notify_on_push_initiated=True,
        notify_on_completed=True,
        notify_on_failure=True,
        notify_on_auto_advance=False,
    )
    db_session.add(sys_settings)
    await db_session.commit()

    # Reload and verify all attributes
    loaded_item = await db_session.get(MediaItem, item.id)
    assert loaded_item is not None
    assert loaded_item.status == MediaStatus.FAILED
    assert loaded_item.preset_id == preset.id
    assert loaded_item.prefer_season_packs is True
    assert loaded_item.auto_advance_seasons is True
    assert loaded_item.last_watched_order == 2
    assert loaded_item.watched_completed is False
    assert json.loads(loaded_item.custom_search_config_json or "{}") == {
        "min_size_gb": 1.0
    }

    loaded_season = await db_session.get(Season, season_entry.id)
    assert loaded_season is not None
    assert loaded_season.status == SeasonStatus.FAILED
    assert loaded_season.entry_type == "movie"
    assert loaded_season.watch_order == 2
    assert loaded_season.type_number == 1

    loaded_hist = await db_session.get(DownloadHistory, history.id)
    assert loaded_hist is not None
    assert loaded_hist.push_mode == "manual"
    assert loaded_hist.progress_pct == pytest.approx(64.5)
    assert loaded_hist.download_speed_bytes == 15_728_640
    assert loaded_hist.eta_seconds == 180
    assert loaded_hist.status_detail == "Downloading from TorBox"
    assert loaded_hist.is_dismissed is False
    assert loaded_hist.unpack_started_at is None

    loaded_settings = await db_session.get(SystemSettings, 1)
    assert loaded_settings is not None
    assert (
        loaded_settings.discord_webhook_url
        == "https://discord.com/api/webhooks/123/abc"
    )
    assert loaded_settings.discord_enabled is True
    assert loaded_settings.notify_on_push_initiated is True
    assert loaded_settings.notify_on_completed is True
    assert loaded_settings.notify_on_failure is True
    assert loaded_settings.notify_on_auto_advance is False


@pytest.mark.anyio
async def test_init_db_default_preset_seeding_and_legacy_migration(
    tmp_path: Path,
) -> None:
    """Verify init_db() migrates legacy tables without v3 columns and seeds 'Default (Best)' preset."""
    import sqlite3

    db_file = tmp_path / "legacy_v2.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category VARCHAR(50) NOT NULL DEFAULT 'watchlist',
            type VARCHAR(50) NOT NULL,
            name VARCHAR(200) NOT NULL
        );
        INSERT INTO providers (id, category, type, name) VALUES (1, 'watchlist', 'simkl', 'Legacy Simkl');

        CREATE TABLE media_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL,
            simkl_id INTEGER NOT NULL DEFAULT 0,
            title VARCHAR(500) NOT NULL,
            media_type VARCHAR(20) NOT NULL DEFAULT 'show',
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO media_items (id, provider_id, simkl_id, title, media_type, status)
        VALUES (1, 1, 999, 'Legacy Show', 'SHOW', 'MANUAL_GRAB');

        CREATE TABLE seasons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_item_id INTEGER NOT NULL,
            season_number INTEGER NOT NULL,
            monitored BOOLEAN NOT NULL DEFAULT 0,
            status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
            episode_count INTEGER,
            air_date DATETIME,
            empty_search_count INTEGER NOT NULL DEFAULT 0,
            last_searched_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO seasons (id, media_item_id, season_number, monitored, status)
        VALUES (1, 1, 1, 1, 'MANUAL_GRAB'), (2, 1, 2, 0, 'PENDING'), (3, 1, 3, 0, 'manual_grab');

        CREATE TABLE episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            season_id INTEGER NOT NULL,
            episode_number INTEGER NOT NULL,
            monitored BOOLEAN NOT NULL DEFAULT 1,
            status VARCHAR(20) NOT NULL DEFAULT 'MANUAL_GRAB',
            air_date DATETIME,
            last_searched_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO episodes (id, season_id, episode_number, monitored, status)
        VALUES (1, 1, 1, 1, 'MANUAL_GRAB');
        """
    )
    conn.commit()
    conn.close()

    db_url = f"sqlite+aiosqlite:///{db_file}"

    await close_db()
    await init_db(db_url)

    factory = get_session_factory()
    async with factory() as session:
        SearchPreset = models.SearchPreset
        presets = (await session.execute(select(SearchPreset))).scalars().all()
        assert len(presets) == 1
        default_p = presets[0]
        assert default_p.name == "Default (Best)"
        assert default_p.is_default is True
        assert default_p.primary_language == "en"
        assert default_p.fallback_language is None
        assert default_p.video_quality_mode == "best"
        assert default_p.audio_quality_mode == "best"
        assert json.loads(default_p.custom_config_json) == {}

        # Verify columns exist via PRAGMA table_info
        for table_name, expected_cols in [
            (
                "media_items",
                {
                    "preset_id",
                    "custom_search_config_json",
                    "prefer_season_packs",
                    "auto_advance_seasons",
                    "last_watched_order",
                    "watched_completed",
                },
            ),
            ("seasons", {"entry_type", "watch_order", "type_number"}),
            (
                "download_history",
                {
                    "push_mode",
                    "progress_pct",
                    "download_speed_bytes",
                    "eta_seconds",
                    "status_detail",
                    "is_dismissed",
                    "unpack_started_at",
                },
            ),
            (
                "system_settings",
                {
                    "discord_webhook_url",
                    "discord_enabled",
                    "notify_on_push_initiated",
                    "notify_on_completed",
                    "notify_on_failure",
                    "notify_on_auto_advance",
                },
            ),
            ("providers", {"config_json"}),
        ]:
            res = await session.execute(text(f"PRAGMA table_info({table_name});"))
            actual_cols = {row[1] for row in res.fetchall()}
            missing = expected_cols - actual_cols
            assert not missing, f"Table {table_name} missing columns: {missing}"

        # Verify legacy seasons had watch_order and type_number backfilled from season_number
        # and legacy MANUAL_GRAB status rows were migrated to SEARCHING
        item1_status = (
            await session.execute(text("SELECT status FROM media_items WHERE id = 1;"))
        ).scalar_one()
        s1 = await session.get(Season, 1)
        s2 = await session.get(Season, 2)
        s3 = await session.get(Season, 3)
        ep1 = await session.get(models.Episode, 1)
        assert item1_status == "SEARCHING"
        assert s1 is not None and s1.status == SeasonStatus.SEARCHING
        assert s2 is not None and s2.watch_order == 2 and s2.type_number == 2
        assert (
            s3 is not None
            and s3.watch_order == 3
            and s3.type_number == 3
            and s3.status == SeasonStatus.SEARCHING
        )
        assert ep1 is not None and ep1.status == EpisodeStatus.SEARCHING

    # Re-running init_db is idempotent and does not duplicate default preset
    await close_db()
    await init_db(db_url)
    factory = get_session_factory()
    async with factory() as session:
        presets = (await session.execute(select(models.SearchPreset))).scalars().all()
        assert len(presets) == 1


@pytest.mark.anyio
async def test_preset_crud_and_inline_save_api(
    db_session: AsyncSession,
    async_client: AsyncClient,
) -> None:
    """Verify Preset CRUD endpoints, single-default enforcement, inline save, and sticky MediaItem config."""
    provider = Provider(name="Simkl", type="simkl", category="watchlist")
    db_session.add(provider)
    await db_session.flush()

    item = MediaItem(
        provider_id=provider.id,
        simkl_id=12345,
        title="Dune: Part Two",
        year=2024,
        media_type=MediaType.MOVIE,
        status=MediaStatus.SEARCHING,
    )
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)

    client = async_client
    # 1. Create first preset (automatically becomes default if none exist or when is_default=True)
    resp1 = await client.post(
        "/api/presets",
        json={
            "name": "Default (Best)",
            "is_default": True,
            "primary_language": "en",
            "fallback_language": None,
            "video_quality_mode": "best",
            "audio_quality_mode": "best",
            "custom_config_json": "{}",
        },
    )
    assert resp1.status_code in (200, 201)
    p1 = resp1.json()
    assert p1["name"] == "Default (Best)"
    assert p1["is_default"] is True

    # 2. Create second preset with is_default=True -> should unset p1's is_default
    resp2 = await client.post(
        "/api/presets",
        json={
            "name": "Anime Dual-Audio 1080p",
            "is_default": True,
            "primary_language": "ja",
            "fallback_language": "en",
            "video_quality_mode": "custom",
            "audio_quality_mode": "best",
            "custom_config_json": json.dumps(
                {"resolutions": ["1080p"], "max_size_gb": 5.0}
            ),
        },
    )
    assert resp2.status_code in (200, 201)
    p2 = resp2.json()
    assert p2["is_default"] is True

    # List presets and verify single default
    list_resp = await client.get("/api/presets")
    assert list_resp.status_code == 200
    all_presets = list_resp.json()
    assert len(all_presets) == 2
    defaults = [p for p in all_presets if p["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["id"] == p2["id"]

    # 3. Toggle p1 back to default via POST /api/presets/{id}/default
    toggle_resp = await client.post(f"/api/presets/{p1['id']}/default")
    assert toggle_resp.status_code == 200
    list_resp2 = await client.get("/api/presets")
    defaults2 = [p for p in list_resp2.json() if p["is_default"]]
    assert len(defaults2) == 1
    assert defaults2[0]["id"] == p1["id"]

    # 4. Update preset p2 via PUT /api/presets/{id}
    upd_resp = await client.put(
        f"/api/presets/{p2['id']}",
        json={
            "name": "Anime Dual-Audio 1080p HEVC",
            "primary_language": "ja",
            "fallback_language": "en",
            "video_quality_mode": "custom",
            "audio_quality_mode": "custom",
            "custom_config_json": {
                "resolutions": ["1080p"],
                "video_codecs": ["hevc"],
                "audio_formats": ["flac"],
            },
        },
    )
    assert upd_resp.status_code == 200
    assert upd_resp.json()["name"] == "Anime Dual-Audio 1080p HEVC"
    assert upd_resp.json()["custom_config"]["video_codecs"] == ["hevc"]

    # 5. Inline save from Push Modal (POST /api/presets/save-inline) linked to media_item_id
    inline_resp = await client.post(
        "/api/presets/save-inline",
        json={
            "name": "Dune IMAX 4K",
            "media_item_id": item.id,
            "primary_language": "en",
            "fallback_language": "de",
            "video_quality_mode": "custom",
            "audio_quality_mode": "custom",
            "custom_config": {
                "resolutions": ["2160p"],
                "sources": ["remux"],
                "audio_formats": ["atmos"],
                "min_size_gb": 20.0,
                "max_size_gb": 80.0,
            },
            "prefer_season_packs": False,
            "auto_advance_seasons": True,
        },
    )
    assert inline_resp.status_code in (200, 201)
    p_inline = inline_resp.json()
    assert p_inline["name"] == "Dune IMAX 4K"
    assert p_inline["custom_config"]["resolutions"] == ["2160p"]

    # Check MediaItem sticky config was updated with the new preset
    await db_session.refresh(item)
    assert item.preset_id == p_inline["id"]
    assert item.prefer_season_packs is False
    assert item.auto_advance_seasons is True

    # 6. Update MediaItem sticky custom config directly without saving a preset
    sticky_resp = await client.post(
        f"/api/items/{item.id}/search-config",
        json={
            "preset_id": None,
            "custom_search_config_json": {
                "primary_language": "de",
                "video_quality_mode": "custom",
                "resolutions": ["1080p"],
            },
            "allow_season_packs": True,
            "prefer_season_packs": True,
            "auto_advance_seasons": False,
        },
    )
    assert sticky_resp.status_code == 200
    await db_session.refresh(item)
    assert item.preset_id is None
    assert item.allow_season_packs is True
    assert item.prefer_season_packs is True
    assert item.auto_advance_seasons is False
    assert (
        json.loads(item.custom_search_config_json or "{}")["primary_language"] == "de"
    )

    # 7. Delete default preset p1 -> remaining preset becomes default automatically
    del_resp = await client.delete(f"/api/presets/{p1['id']}")
    assert del_resp.status_code == 200
    remaining = (await client.get("/api/presets")).json()
    assert len(remaining) == 2
    assert sum(1 for p in remaining if p["is_default"]) == 1


@pytest.mark.anyio
async def test_unpack_started_at_schema_migration(tmp_path: Path) -> None:
    """Verify init_db() cleanly migrates download_history by adding unpack_started_at TIMESTAMP."""
    import sqlite3
    from datetime import datetime, timezone

    db_file = tmp_path / "unpack_migration.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE download_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_item_id INTEGER,
            season_id INTEGER,
            episode_id INTEGER,
            media_title VARCHAR(500),
            media_year INTEGER,
            media_type_label VARCHAR(30),
            target_label VARCHAR(200),
            poster_url VARCHAR(1000),
            simkl_id INTEGER,
            tmdb_id INTEGER,
            imdb_id VARCHAR(20),
            anilist_id INTEGER,
            nzb_title VARCHAR(1000) NOT NULL,
            nzb_guid VARCHAR(500),
            score FLOAT,
            size_bytes INTEGER,
            resolution VARCHAR(20),
            video_codec VARCHAR(50),
            audio_codec VARCHAR(100),
            source VARCHAR(100),
            release_group VARCHAR(100),
            bitrate_mbps FLOAT,
            torbox_hash VARCHAR(200),
            torbox_id VARCHAR(200),
            torbox_sent_at DATETIME,
            is_fallback BOOLEAN NOT NULL DEFAULT 0,
            grabbed_language VARCHAR(50),
            push_mode VARCHAR(20) NOT NULL DEFAULT 'auto',
            progress_pct FLOAT NOT NULL DEFAULT 0.0,
            download_speed_bytes BIGINT NOT NULL DEFAULT 0,
            eta_seconds INTEGER,
            status_detail VARCHAR(200),
            is_dismissed BOOLEAN NOT NULL DEFAULT 0,
            notification_sent BOOLEAN NOT NULL DEFAULT 0,
            auto_replaced_count INTEGER NOT NULL DEFAULT 0,
            completed_at DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO download_history (id, media_item_id, nzb_title, status_detail)
        VALUES (1, NULL, 'Unpack.Testing.Release', 'downloading');
        """
    )
    conn.commit()
    conn.close()

    db_url = f"sqlite+aiosqlite:///{db_file}"

    await close_db()
    await init_db(db_url)

    factory = get_session_factory()
    async with factory() as session:
        # Check column exists in PRAGMA table_info
        res = await session.execute(text("PRAGMA table_info(download_history);"))
        cols = {row[1] for row in res.fetchall()}
        assert "unpack_started_at" in cols

        # Query existing row and update unpack_started_at
        hist = await session.get(DownloadHistory, 1)
        assert hist is not None
        assert hist.unpack_started_at is None

        now_utc = datetime.now(timezone.utc)
        hist.unpack_started_at = now_utc
        await session.commit()

        # Re-query to verify persistence
        refreshed = await session.get(DownloadHistory, 1)
        assert refreshed is not None
        assert refreshed.unpack_started_at is not None
        assert refreshed.unpack_started_at.replace(tzinfo=timezone.utc) == now_utc

    await close_db()


@pytest.mark.anyio
async def test_last_air_dates_refreshed_at_column_exists(
    db_session: AsyncSession,
) -> None:
    """init_db() (via setup_db fixture) creates media_items.last_air_dates_refreshed_at."""
    res = await db_session.execute(text("PRAGMA table_info(media_items);"))
    assert "last_air_dates_refreshed_at" in {row[1] for row in res.fetchall()}
