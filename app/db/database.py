"""
NZBoxer Database Engine & Session Factory
==========================================
Configures the async SQLAlchemy engine, session factory, and provides
a dependency-injection helper for FastAPI route handlers.

Usage (in route handlers)::

    from app.db.database import get_session
    from sqlalchemy.ext.asyncio import AsyncSession

    @router.get("/items")
    async def list_items(session: AsyncSession = Depends(get_session)):
        ...

Usage (in background jobs / non-FastAPI contexts)::

    from app.db.database import async_session_factory

    async with async_session_factory() as session:
        ...
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.models import Base

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine — created lazily; initialized on app startup.
# ---------------------------------------------------------------------------

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the application-wide async SQLAlchemy engine.

    Raises:
        RuntimeError: If the engine has not been initialized yet.
    """
    if _engine is None:
        raise RuntimeError(
            "Database engine is not initialized. "
            "Call `init_db(database_url)` during application startup."
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the application-wide async session factory."""
    if _session_factory is None:
        raise RuntimeError(
            "Session factory is not initialized. "
            "Call `init_db(database_url)` during application startup."
        )
    return _session_factory


async def init_db(database_url: str) -> None:
    """Initialize the async engine, create all tables, and configure WAL mode.

    Args:
        database_url: SQLAlchemy async database URL.
                      Example: 'sqlite+aiosqlite:///./nzboxer.db'
    """
    global _engine, _session_factory

    logger.info("Initializing database: %s", database_url)

    from typing import Any

    from sqlalchemy import event
    from sqlalchemy.pool import StaticPool

    connect_args: dict[str, Any] = {"check_same_thread": False}
    if database_url.startswith("sqlite"):
        connect_args["timeout"] = 60.0

    kwargs = {
        "echo": False,
        "future": True,
        "connect_args": connect_args,
    }

    if ":memory:" in database_url or "mode=memory" in database_url:
        kwargs["poolclass"] = StaticPool

    _engine = create_async_engine(database_url, **kwargs)

    if database_url.startswith("sqlite"):

        @event.listens_for(_engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=60000")
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,  # Prevent lazy-loading errors after commit
        autoflush=False,
        autocommit=False,
    )

    # Detect legacy v2 database before creating v3 tables (ADR-089)
    retired_v2_tables = (
        "manga_volumes",
        "manga_items",
        "magazine_issues",
        "magazine_subscriptions",
        "provider_profiles",
        "seen_torbox_downloads",
        "daily_grab_counters",
        "book_items",
    )
    is_v2_upgrade = False
    saved_providers: list[dict[str, Any]] = []
    saved_settings: list[dict[str, Any]] = []
    saved_channels: list[dict[str, Any]] = []
    saved_presets: list[dict[str, Any]] = []

    async with _engine.begin() as conn:
        if database_url.startswith("sqlite"):
            import json
            import shutil
            from pathlib import Path

            from sqlalchemy import text as sa_text
            from sqlalchemy.engine import make_url

            existing_tables = {
                row[0]
                for row in (
                    await conn.execute(
                        sa_text("SELECT name FROM sqlite_master WHERE type='table'")
                    )
                ).fetchall()
            }
            has_legacy_settings_cols = False
            if "system_settings" in existing_tables:
                settings_cols = {
                    row[1]
                    for row in (
                        await conn.execute(
                            sa_text("PRAGMA table_info(system_settings)")
                        )
                    ).fetchall()
                }
                if settings_cols & {
                    "treasure_maps_api_key",
                    "torbox_api_key",
                    "tmdb_api_key",
                    "automation_state",
                    "dry_run",
                }:
                    has_legacy_settings_cols = True

            is_v2_upgrade = (
                bool(existing_tables & set(retired_v2_tables))
                or has_legacy_settings_cols
            )

            if is_v2_upgrade:
                logger.info(
                    "Detected legacy v2 database — executing one-time v3 soft reset "
                    "(preserving settings, providers, and notification channels)..."
                )
                # 1. Auto-backup file-based SQLite DB before modifying
                try:
                    db_file_str = make_url(database_url).database
                    if (
                        db_file_str
                        and db_file_str != ":memory:"
                        and "mode=memory" not in database_url
                    ):
                        db_path = Path(db_file_str)
                        if db_path.is_file():
                            backup_path = db_path.with_name(f"{db_path.name}.v2_backup")
                            if not backup_path.exists():
                                shutil.copy2(db_path, backup_path)
                                logger.info(
                                    "Backed up legacy v2 database to %s", backup_path
                                )
                except Exception as exc:
                    logger.warning("Could not create v2 database backup: %s", exc)

                # 2. Snapshot user configuration tables (providers, system_settings, notification_channels, search_presets)
                if "providers" in existing_tables:
                    rows = (
                        (await conn.execute(sa_text("SELECT * FROM providers")))
                        .mappings()
                        .all()
                    )
                    for r in rows:
                        p_type = str(r.get("type") or "").strip()
                        p_cat = str(r.get("category") or "watchlist").strip()
                        if (
                            p_type in ("hardcover", "openlibrary")
                            or p_cat == "print_media"
                        ):
                            continue
                        if p_type in ("anilist", "tmdb"):
                            p_cat = "metadata"
                        elif p_type == "torbox":
                            p_cat = "downloader"
                        elif p_type == "treasure_maps":
                            p_cat = "indexer"
                        elif p_type == "simkl":
                            p_cat = "watchlist"

                        api_url = r.get("api_url")
                        if isinstance(api_url, str) and "treasuremaps.net" in api_url:
                            api_url = api_url.replace(
                                "treasuremaps.net", "treasure-maps.com"
                            )

                        raw_cfg = r.get("config_json") or "{}"
                        try:
                            cfg_dict = (
                                json.loads(raw_cfg)
                                if isinstance(raw_cfg, str)
                                else dict(raw_cfg)
                            )
                            if not isinstance(cfg_dict, dict):
                                cfg_dict = {}
                        except Exception:
                            cfg_dict = {}
                        if p_type == "simkl":
                            cfg_dict.pop("last_synced_at", None)

                        raw_prio = r.get("priority")
                        raw_active = r.get("is_active")
                        saved_providers.append(
                            {
                                "id": r.get("id"),
                                "category": p_cat,
                                "type": p_type,
                                "name": str(r.get("name") or p_type or "Provider"),
                                "api_key": r.get("api_key"),
                                "api_url": api_url,
                                "priority": int(raw_prio)
                                if raw_prio is not None
                                else 1,
                                "is_active": bool(raw_active)
                                if raw_active is not None
                                else True,
                                "username": r.get("username"),
                                "client_id": r.get("client_id"),
                                "access_token": r.get("access_token"),
                                "config_json": json.dumps(cfg_dict),
                            }
                        )

                if "system_settings" in existing_tables:
                    rows = (
                        (await conn.execute(sa_text("SELECT * FROM system_settings")))
                        .mappings()
                        .all()
                    )
                    for r in rows:
                        raw_retries = r.get("sh_max_retries")
                        raw_timeout = r.get("download_timeout_hours")
                        raw_disc_en = r.get("discord_enabled")
                        raw_not_push = r.get("notify_on_push_initiated")
                        raw_not_comp = r.get("notify_on_completed")
                        raw_not_fail = r.get("notify_on_failure")
                        raw_not_adv = r.get("notify_on_auto_advance")
                        saved_settings.append(
                            {
                                "id": int(r.get("id") or 1),
                                "sh_max_retries": int(raw_retries)
                                if raw_retries is not None
                                else 3,
                                "download_timeout_hours": int(raw_timeout)
                                if raw_timeout is not None
                                else 24,
                                "discord_webhook_url": r.get("discord_webhook_url"),
                                "discord_enabled": bool(raw_disc_en)
                                if raw_disc_en is not None
                                else False,
                                "notify_on_push_initiated": bool(raw_not_push)
                                if raw_not_push is not None
                                else False,
                                "notify_on_completed": bool(raw_not_comp)
                                if raw_not_comp is not None
                                else True,
                                "notify_on_failure": bool(raw_not_fail)
                                if raw_not_fail is not None
                                else True,
                                "notify_on_auto_advance": bool(raw_not_adv)
                                if raw_not_adv is not None
                                else True,
                            }
                        )

                if "notification_channels" in existing_tables:
                    rows = (
                        (
                            await conn.execute(
                                sa_text("SELECT * FROM notification_channels")
                            )
                        )
                        .mappings()
                        .all()
                    )
                    for r in rows:
                        saved_channels.append(
                            {
                                "id": r.get("id"),
                                "type": str(r.get("type") or "telegram"),
                                "name": str(r.get("name") or "Telegram"),
                                "bot_token": r.get("bot_token"),
                                "chat_id": r.get("chat_id"),
                            }
                        )

                if "search_presets" in existing_tables:
                    rows = (
                        (await conn.execute(sa_text("SELECT * FROM search_presets")))
                        .mappings()
                        .all()
                    )
                    for r in rows:
                        allow_packs = bool(r.get("allow_season_packs") or False)
                        prefer_packs = (
                            bool(r.get("prefer_season_packs") or False)
                            if allow_packs
                            else False
                        )
                        saved_presets.append(
                            {
                                "id": r.get("id"),
                                "name": str(r.get("name") or "Default (Best)"),
                                "is_default": bool(r.get("is_default") or False),
                                "primary_language": str(
                                    r.get("primary_language") or "en"
                                ),
                                "fallback_language": r.get("fallback_language"),
                                "video_quality_mode": str(
                                    r.get("video_quality_mode") or "best"
                                ),
                                "audio_quality_mode": str(
                                    r.get("audio_quality_mode") or "best"
                                ),
                                "allow_season_packs": allow_packs,
                                "prefer_season_packs": prefer_packs,
                                "custom_config_json": str(
                                    r.get("custom_config_json") or "{}"
                                ),
                            }
                        )

                # 3. Drop all retired v2 tables, media/history tables, and legacy-schema config tables
                await conn.execute(sa_text("PRAGMA foreign_keys=OFF;"))
                for tbl_to_drop in (
                    *retired_v2_tables,
                    "download_history",
                    "episodes",
                    "seasons",
                    "blacklisted_releases",
                    "failure_logs",
                    "media_items",
                    "providers",
                    "system_settings",
                    "notification_channels",
                    "search_presets",
                ):
                    await conn.execute(sa_text(f"DROP TABLE IF EXISTS {tbl_to_drop};"))
                await conn.execute(sa_text("PRAGMA foreign_keys=ON;"))

        # Create all tables defined in models.py (idempotent — safe to call on restart)
        await conn.run_sync(Base.metadata.create_all)

        # Restore preserved settings after v2 -> v3 soft reset
        if is_v2_upgrade and database_url.startswith("sqlite"):
            from sqlalchemy import text as sa_text

            for p_row in saved_providers:
                await conn.execute(
                    sa_text(
                        "INSERT INTO providers "
                        "(id, category, type, name, api_key, api_url, priority, is_active, "
                        "username, client_id, access_token, config_json) "
                        "VALUES (:id, :category, :type, :name, :api_key, :api_url, :priority, :is_active, "
                        ":username, :client_id, :access_token, :config_json)"
                    ),
                    p_row,
                )
            for s_row in saved_settings:
                await conn.execute(
                    sa_text(
                        "INSERT INTO system_settings "
                        "(id, sh_max_retries, download_timeout_hours, discord_webhook_url, "
                        "discord_enabled, notify_on_push_initiated, notify_on_completed, "
                        "notify_on_failure, notify_on_auto_advance) "
                        "VALUES (:id, :sh_max_retries, :download_timeout_hours, :discord_webhook_url, "
                        ":discord_enabled, :notify_on_push_initiated, :notify_on_completed, "
                        ":notify_on_failure, :notify_on_auto_advance)"
                    ),
                    s_row,
                )
            for c_row in saved_channels:
                await conn.execute(
                    sa_text(
                        "INSERT INTO notification_channels (id, type, name, bot_token, chat_id) "
                        "VALUES (:id, :type, :name, :bot_token, :chat_id)"
                    ),
                    c_row,
                )
            for pr_row in saved_presets:
                await conn.execute(
                    sa_text(
                        "INSERT INTO search_presets "
                        "(id, name, is_default, primary_language, fallback_language, "
                        "video_quality_mode, audio_quality_mode, allow_season_packs, "
                        "prefer_season_packs, custom_config_json) "
                        "VALUES (:id, :name, :is_default, :primary_language, :fallback_language, "
                        ":video_quality_mode, :audio_quality_mode, :allow_season_packs, "
                        ":prefer_season_packs, :custom_config_json)"
                    ),
                    pr_row,
                )
            logger.info(
                "Completed one-time v2 -> v3 database soft reset "
                "(preserved %d provider(s), %d settings row(s), %d notification channel(s)).",
                len(saved_providers),
                len(saved_settings),
                len(saved_channels),
            )

    # Enable SQLite WAL mode for better concurrent access
    if database_url.startswith("sqlite"):
        async with _session_factory() as session:
            # Perform a crude migration if the table exists but column is missing
            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE system_settings ADD COLUMN scoring_settings JSON;"
                    )
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE media_items ADD COLUMN provider_id INTEGER DEFAULT 1;"
                    )
                )
                # Create a default provider if we just added the column, so foreign keys don't break
                await session.execute(
                    __import__("sqlalchemy").text(
                        "INSERT OR IGNORE INTO providers (id, type, name) VALUES (1, 'simkl', 'Default Simkl');"
                    )
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE download_history ADD COLUMN torbox_id VARCHAR(200);"
                    )
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE media_items ADD COLUMN tvdb_id INTEGER;"
                    )
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE media_items ADD COLUMN mal_id INTEGER;"
                    )
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE media_items ADD COLUMN anilist_id INTEGER;"
                    )
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE media_items ADD COLUMN alt_title VARCHAR(500);"
                    )
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE media_items ADD COLUMN is_anime_movie BOOLEAN NOT NULL DEFAULT 0;"
                    )
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE download_history ADD COLUMN episode_id INTEGER;"
                    )
                )
            except Exception:
                pass

            for tbl in ["media_items", "seasons", "episodes"]:
                try:
                    await session.execute(
                        __import__("sqlalchemy").text(
                            f"ALTER TABLE {tbl} ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0;"
                        )
                    )
                    await session.execute(
                        __import__("sqlalchemy").text(
                            f"ALTER TABLE {tbl} ADD COLUMN last_error TEXT;"
                        )
                    )
                except Exception:
                    pass

            await session.execute(
                __import__("sqlalchemy").text("PRAGMA journal_mode=WAL;")
            )
            await session.execute(
                __import__("sqlalchemy").text("PRAGMA foreign_keys=ON;")
            )

            # Providers multi-category columns migration
            for col, col_type in [
                ("category", "VARCHAR(50) NOT NULL DEFAULT 'watchlist'"),
                ("api_key", "VARCHAR(500)"),
                ("api_url", "VARCHAR(500)"),
                ("priority", "INTEGER NOT NULL DEFAULT 1"),
                ("is_active", "BOOLEAN NOT NULL DEFAULT 1"),
            ]:
                try:
                    await session.execute(
                        __import__("sqlalchemy").text(
                            f"ALTER TABLE providers ADD COLUMN {col} {col_type};"
                        )
                    )
                except Exception:
                    pass

            # Season external identifiers (simkl_id, anilist_id)
            for col in ["simkl_id", "anilist_id"]:
                try:
                    await session.execute(
                        __import__("sqlalchemy").text(
                            f"ALTER TABLE seasons ADD COLUMN {col} INTEGER;"
                        )
                    )
                except Exception:
                    pass
                try:
                    await session.execute(
                        __import__("sqlalchemy").text(
                            f"CREATE INDEX IF NOT EXISTS ix_seasons_{col} ON seasons ({col});"
                        )
                    )
                except Exception:
                    pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE seasons ADD COLUMN title VARCHAR(512);"
                    )
                )
            except Exception:
                pass

            # Metadata TTL tracking
            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE media_items ADD COLUMN last_metadata_refreshed_at DATETIME;"
                    )
                )
            except Exception:
                pass

            # Stalled download timeout migration (ADR-054)
            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE system_settings ADD COLUMN download_timeout_hours INTEGER NOT NULL DEFAULT 24;"
                    )
                )
            except Exception:
                pass

            # Download history language tracking (ADR-060)
            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE download_history ADD COLUMN is_fallback BOOLEAN NOT NULL DEFAULT 0;"
                    )
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE download_history ADD COLUMN grabbed_language VARCHAR(50);"
                    )
                )
            except Exception:
                pass

            # Backfill existing providers category
            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "DELETE FROM providers WHERE type IN ('hardcover', 'openlibrary') OR category = 'print_media';"
                    )
                )
                await session.execute(
                    __import__("sqlalchemy").text(
                        "UPDATE providers SET category = 'metadata' WHERE type IN ('anilist', 'tmdb');"
                    )
                )
                await session.execute(
                    __import__("sqlalchemy").text(
                        "UPDATE providers SET category = 'downloader' WHERE type = 'torbox';"
                    )
                )
                await session.execute(
                    __import__("sqlalchemy").text(
                        "UPDATE providers SET category = 'indexer' WHERE type = 'treasure_maps';"
                    )
                )
                await session.execute(
                    __import__("sqlalchemy").text(
                        "UPDATE providers SET category = 'watchlist' WHERE type = 'simkl';"
                    )
                )
            except Exception:
                pass

            # --- v3.0.0 Schema Migrations ---
            v3_migrations = [
                # SearchPreset two-checkbox season pack columns (ADR-078)
                "ALTER TABLE search_presets ADD COLUMN allow_season_packs BOOLEAN NOT NULL DEFAULT 0;",
                "ALTER TABLE search_presets ADD COLUMN prefer_season_packs BOOLEAN NOT NULL DEFAULT 0;",
                # MediaItem sticky & Simkl watch progress columns
                "ALTER TABLE media_items ADD COLUMN preset_id INTEGER REFERENCES search_presets(id) ON DELETE SET NULL;",
                "ALTER TABLE media_items ADD COLUMN custom_search_config_json TEXT;",
                "ALTER TABLE media_items ADD COLUMN allow_season_packs BOOLEAN NOT NULL DEFAULT 0;",
                "ALTER TABLE media_items ADD COLUMN prefer_season_packs BOOLEAN NOT NULL DEFAULT 0;",
                "ALTER TABLE media_items ADD COLUMN auto_advance_seasons BOOLEAN NOT NULL DEFAULT 0;",
                "ALTER TABLE media_items ADD COLUMN last_watched_order_simkl INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE media_items ADD COLUMN simkl_watched_completed BOOLEAN NOT NULL DEFAULT 0;",
                # Season chronological franchise columns
                "ALTER TABLE seasons ADD COLUMN entry_type VARCHAR(20) NOT NULL DEFAULT 'season';",
                "ALTER TABLE seasons ADD COLUMN watch_order INTEGER NOT NULL DEFAULT 1;",
                "ALTER TABLE seasons ADD COLUMN type_number INTEGER NOT NULL DEFAULT 1;",
                # DownloadHistory live transfer, push mode, and denormalized snapshot columns (ADR-082)
                "ALTER TABLE download_history ADD COLUMN push_mode VARCHAR(20) NOT NULL DEFAULT 'auto';",
                "ALTER TABLE download_history ADD COLUMN progress_pct FLOAT NOT NULL DEFAULT 0.0;",
                "ALTER TABLE download_history ADD COLUMN download_speed_bytes BIGINT NOT NULL DEFAULT 0;",
                "ALTER TABLE download_history ADD COLUMN eta_seconds INTEGER;",
                "ALTER TABLE download_history ADD COLUMN status_detail VARCHAR(200);",
                "ALTER TABLE download_history ADD COLUMN is_dismissed BOOLEAN NOT NULL DEFAULT 0;",
                "ALTER TABLE download_history ADD COLUMN notification_sent BOOLEAN NOT NULL DEFAULT 0;",
                "ALTER TABLE download_history ADD COLUMN auto_replaced_count INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE download_history ADD COLUMN media_title VARCHAR(500);",
                "ALTER TABLE download_history ADD COLUMN media_year INTEGER;",
                "ALTER TABLE download_history ADD COLUMN media_type_label VARCHAR(30);",
                "ALTER TABLE download_history ADD COLUMN target_label VARCHAR(200);",
                "ALTER TABLE download_history ADD COLUMN poster_url VARCHAR(1000);",
                "ALTER TABLE download_history ADD COLUMN simkl_id INTEGER;",
                "ALTER TABLE download_history ADD COLUMN tmdb_id INTEGER;",
                "ALTER TABLE download_history ADD COLUMN imdb_id VARCHAR(20);",
                "ALTER TABLE download_history ADD COLUMN anilist_id INTEGER;",
                # SystemSettings Discord webhook & per-event notification columns
                "ALTER TABLE system_settings ADD COLUMN discord_webhook_url VARCHAR(500);",
                "ALTER TABLE system_settings ADD COLUMN discord_enabled BOOLEAN NOT NULL DEFAULT 0;",
                "ALTER TABLE system_settings ADD COLUMN notify_on_push_initiated BOOLEAN NOT NULL DEFAULT 0;",
                "ALTER TABLE system_settings ADD COLUMN notify_on_completed BOOLEAN NOT NULL DEFAULT 1;",
                "ALTER TABLE system_settings ADD COLUMN notify_on_failure BOOLEAN NOT NULL DEFAULT 1;",
                "ALTER TABLE system_settings ADD COLUMN notify_on_auto_advance BOOLEAN NOT NULL DEFAULT 1;",
                # Provider Simkl sync config_json column
                "ALTER TABLE providers ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}';",
                # Migrate retired v2 MANUAL_GRAB status values to SEARCHING
                "UPDATE media_items SET status = 'SEARCHING' WHERE status IN ('MANUAL_GRAB', 'manual_grab');",
                "UPDATE seasons SET status = 'SEARCHING' WHERE status IN ('MANUAL_GRAB', 'manual_grab');",
                "UPDATE episodes SET status = 'SEARCHING' WHERE status IN ('MANUAL_GRAB', 'manual_grab');",
                # Clean up orphaned v2 print media failure logs
                "DELETE FROM failure_logs WHERE media_item_id IS NULL;",
                # Enforce two-checkbox season pack invariant (!allow_season_packs => !prefer_season_packs)
                "UPDATE search_presets SET prefer_season_packs = 0 WHERE allow_season_packs = 0 AND prefer_season_packs = 1;",
                "UPDATE media_items SET prefer_season_packs = 0 WHERE allow_season_packs = 0 AND prefer_season_packs = 1;",
                # Backfill existing terminal DownloadHistory rows to notification_sent = 1
                "UPDATE download_history SET notification_sent = 1 WHERE status_detail IN ('completed', 'failed', 'deleted', 'replaced', 'canceled') OR LOWER(COALESCE(status_detail, '')) LIKE 'failed%';",
            ]
            for sql_stmt in v3_migrations:
                try:
                    await session.execute(__import__("sqlalchemy").text(sql_stmt))
                except Exception:
                    pass

            # --- Rebuild download_history if media_item_id is still NOT NULL or ON DELETE CASCADE (ADR-082) ---
            try:
                from sqlalchemy import text as sa_text

                ti_rows = (
                    await session.execute(
                        sa_text("PRAGMA table_info(download_history)")
                    )
                ).fetchall()
                fk_rows = (
                    await session.execute(
                        sa_text("PRAGMA foreign_key_list(download_history)")
                    )
                ).fetchall()
                needs_rebuild = any(
                    r[1] == "media_item_id" and int(r[3] or 0) == 1 for r in ti_rows
                ) or any(str(r[6] or "").upper() == "CASCADE" for r in fk_rows)

                if needs_rebuild:
                    await session.commit()
                    await session.execute(sa_text("PRAGMA foreign_keys=OFF;"))
                    await session.execute(
                        sa_text("DROP TABLE IF EXISTS download_history_v3_new;")
                    )
                    await session.execute(
                        sa_text(
                            """
                            CREATE TABLE download_history_v3_new (
                                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                                media_item_id INTEGER REFERENCES media_items(id) ON DELETE SET NULL,
                                season_id INTEGER REFERENCES seasons(id) ON DELETE SET NULL,
                                episode_id INTEGER REFERENCES episodes(id) ON DELETE SET NULL,
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
                                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                            );
                            """
                        )
                    )
                    target_cols = [
                        "id",
                        "media_item_id",
                        "season_id",
                        "episode_id",
                        "media_title",
                        "media_year",
                        "media_type_label",
                        "target_label",
                        "poster_url",
                        "simkl_id",
                        "tmdb_id",
                        "imdb_id",
                        "anilist_id",
                        "nzb_title",
                        "nzb_guid",
                        "score",
                        "size_bytes",
                        "resolution",
                        "video_codec",
                        "audio_codec",
                        "source",
                        "release_group",
                        "bitrate_mbps",
                        "torbox_hash",
                        "torbox_id",
                        "torbox_sent_at",
                        "is_fallback",
                        "grabbed_language",
                        "push_mode",
                        "progress_pct",
                        "download_speed_bytes",
                        "eta_seconds",
                        "status_detail",
                        "is_dismissed",
                        "notification_sent",
                        "auto_replaced_count",
                        "created_at",
                    ]
                    existing_col_names = {r[1] for r in ti_rows}
                    shared_cols = [c for c in target_cols if c in existing_col_names]
                    cols_csv = ", ".join(shared_cols)
                    await session.execute(
                        sa_text(
                            f"INSERT INTO download_history_v3_new ({cols_csv}) "
                            f"SELECT {cols_csv} FROM download_history;"
                        )
                    )
                    await session.execute(sa_text("DROP TABLE download_history;"))
                    await session.execute(
                        sa_text(
                            "ALTER TABLE download_history_v3_new RENAME TO download_history;"
                        )
                    )
                    for idx_col in (
                        "media_item_id",
                        "season_id",
                        "episode_id",
                        "simkl_id",
                        "tmdb_id",
                        "imdb_id",
                        "anilist_id",
                        "nzb_guid",
                        "torbox_hash",
                        "torbox_id",
                    ):
                        await session.execute(
                            sa_text(
                                f"CREATE INDEX IF NOT EXISTS ix_download_history_{idx_col} "
                                f"ON download_history ({idx_col});"
                            )
                        )
                    await session.commit()
                    await session.execute(sa_text("PRAGMA foreign_keys=ON;"))
                    logger.info(
                        "Rebuilt download_history table with nullable foreign keys (ON DELETE SET NULL)."
                    )
            except Exception as e:
                logger.warning("Error rebuilding download_history table: %s", e)

            # --- Backfill denormalized snapshot fields on existing DownloadHistory rows (ADR-082) ---
            try:
                from sqlalchemy import select
                from sqlalchemy.orm import selectinload

                from app.core.push_engine import populate_history_snapshot
                from app.db.models import DownloadHistory

                unpopulated_stmt = (
                    select(DownloadHistory)
                    .where(
                        DownloadHistory.media_item_id.isnot(None),
                        DownloadHistory.media_title.is_(None),
                    )
                    .options(
                        selectinload(DownloadHistory.media_item),
                        selectinload(DownloadHistory.season),
                        selectinload(DownloadHistory.episode),
                    )
                )
                unpopulated_rows = (
                    (await session.execute(unpopulated_stmt)).scalars().all()
                )
                for dh in unpopulated_rows:
                    if dh.media_item is not None:
                        populate_history_snapshot(
                            dh,
                            dh.media_item,
                            season=dh.season,
                            episode=dh.episode,
                            only_missing=True,
                        )
                if unpopulated_rows:
                    await session.commit()
            except Exception as e:
                logger.warning("Error backfilling download_history snapshots: %s", e)

            # Backfill existing seasons watch_order and type_number from season_number
            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "UPDATE seasons SET watch_order = season_number, type_number = season_number "
                        "WHERE watch_order = 1 AND season_number > 1;"
                    )
                )
            except Exception:
                pass

            # Backfill missing parent media_items.release_date from earliest non-special season air_date
            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "UPDATE media_items SET release_date = ("
                        "  SELECT MIN(seasons.air_date) FROM seasons "
                        "  WHERE seasons.media_item_id = media_items.id "
                        "    AND seasons.season_number > 0 "
                        "    AND seasons.air_date IS NOT NULL"
                        ") WHERE release_date IS NULL AND EXISTS ("
                        "  SELECT 1 FROM seasons "
                        "  WHERE seasons.media_item_id = media_items.id "
                        "    AND seasons.season_number > 0 "
                        "    AND seasons.air_date IS NOT NULL"
                        ");"
                    )
                )
                await session.commit()
            except Exception as e:
                logger.warning("Error backfilling media_items.release_date: %s", e)

            # Deduplicate any duplicate MediaItem rows (e.g. from concurrent syncs)
            try:
                from app.core.automation import deduplicate_media_items

                removed_dups = await deduplicate_media_items(session)
                if removed_dups > 0:
                    await session.commit()
            except Exception as e:
                logger.warning("Error deduplicating media_items during startup: %s", e)

            # Reconcile any COMPLETED/DOWNLOADED items, seasons, or episodes with zero active/completed DownloadHistory rows
            try:
                from app.core.transfer_poller import (
                    reconcile_all_items_transfer_truth,
                )

                await reconcile_all_items_transfer_truth(session)
            except Exception as e:
                logger.warning("Error reconciling transfer truth during startup: %s", e)

            # --- Domain migration: treasuremaps.net → treasure-maps.com ---
            try:
                from sqlalchemy import func, select, update

                from app.db.models import Provider

                migrated = await session.execute(
                    update(Provider)
                    .where(Provider.api_url.contains("treasuremaps.net"))
                    .values(
                        api_url=func.replace(
                            Provider.api_url,
                            "treasuremaps.net",
                            "treasure-maps.com",
                        )
                    )
                )
                row_count = getattr(migrated, "rowcount", 0)
                if row_count:
                    logger.info(
                        "Migrated %d provider(s) from treasuremaps.net to treasure-maps.com",
                        row_count,
                    )
            except Exception as e:
                logger.warning("Error migrating indexer domain: %s", e)

            # --- Migrate legacy cutoffs.target_score if set to 2500 ---
            try:
                from app.core.default_scoring import DEFAULT_SCORING_CONFIG
                from app.db.models import SystemSettings

                settings_res = await session.execute(
                    select(SystemSettings).where(SystemSettings.id == 1)
                )
                db_settings = settings_res.scalars().first()
                if db_settings and db_settings.scoring_settings:
                    import copy

                    sc = copy.deepcopy(db_settings.scoring_settings)
                    cutoffs = sc.get("cutoffs")
                    if (
                        isinstance(cutoffs, dict)
                        and cutoffs.get("target_score") == 2500
                    ):
                        raw_cutoffs = (
                            DEFAULT_SCORING_CONFIG.get("cutoffs")
                            if isinstance(DEFAULT_SCORING_CONFIG, dict)
                            else None
                        )
                        default_target = (
                            raw_cutoffs.get("target_score", 8000)
                            if isinstance(raw_cutoffs, dict)
                            else 8000
                        )
                        cutoffs["target_score"] = default_target
                        db_settings.scoring_settings = sc
                        logger.info(
                            "Migrated legacy target_score cutoff from 2500 to %d",
                            default_target,
                        )
            except Exception as e:
                logger.warning("Error migrating scoring cutoffs: %s", e)

            # Auto-seed default SearchPreset if no presets exist
            try:
                from sqlalchemy import select

                from app.db.models import SearchPreset

                existing_presets = (
                    (
                        await session.execute(
                            select(SearchPreset).order_by(SearchPreset.id.asc())
                        )
                    )
                    .scalars()
                    .all()
                )
                if not existing_presets:
                    default_preset = SearchPreset(
                        name="Default (Best)",
                        is_default=True,
                        primary_language="en",
                        fallback_language=None,
                        video_quality_mode="best",
                        audio_quality_mode="best",
                        allow_season_packs=False,
                        prefer_season_packs=False,
                        custom_config_json="{}",
                    )
                    session.add(default_preset)
                elif not any(p.is_default for p in existing_presets):
                    existing_presets[0].is_default = True
            except Exception as e:
                logger.warning("Error seeding default SearchPreset: %s", e)

            await session.commit()

    logger.info("Database initialized successfully.")


async def close_db() -> None:
    """Dispose the async engine (called on application shutdown)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("Database connection closed.")


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a managed async database session.

    The session is automatically committed on success and rolled back on
    any exception. Always closed at the end of the request lifecycle.

    Yields:
        AsyncSession: An active SQLAlchemy async session.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# Convenience alias for use in background jobs (non-FastAPI context)
async_session_factory = lambda: get_session_factory()()
