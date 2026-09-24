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

    # Create all tables defined in models.py (idempotent — safe to call on restart)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

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
                        "ALTER TABLE system_settings ADD COLUMN tmdb_api_key VARCHAR(200) DEFAULT '';"
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
                        "ALTER TABLE providers ADD COLUMN anime_category_id INTEGER;"
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

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE system_settings ADD COLUMN dry_run BOOLEAN NOT NULL DEFAULT 0;"
                    )
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE system_settings ADD COLUMN auto_grab_title_fallbacks BOOLEAN NOT NULL DEFAULT 0;"
                    )
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE system_settings ADD COLUMN automation_state VARCHAR(50) NOT NULL DEFAULT 'ACTIVE';"
                    )
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE provider_profiles ADD COLUMN search_cycle_skip INTEGER NOT NULL DEFAULT 1;"
                    )
                )
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE provider_profiles ADD COLUMN current_cycle_count INTEGER NOT NULL DEFAULT 0;"
                    )
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE provider_profiles ADD COLUMN source VARCHAR(100) DEFAULT 'any';"
                    )
                )
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE provider_profiles ADD COLUMN video_codec VARCHAR(100) DEFAULT 'any';"
                    )
                )
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE provider_profiles ADD COLUMN hdr VARCHAR(100) DEFAULT 'any';"
                    )
                )
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE provider_profiles ADD COLUMN audio_tier VARCHAR(100) DEFAULT 'any';"
                    )
                )
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE provider_profiles ADD COLUMN audio_channels VARCHAR(100) DEFAULT 'any';"
                    )
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE media_items ADD COLUMN auto_monitor_next_season BOOLEAN NOT NULL DEFAULT 0;"
                    )
                )
            except Exception:
                pass

            # Ensure provider_profiles.auto_monitor_next_season exists (model compat col, value unused)
            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE provider_profiles ADD COLUMN auto_monitor_next_season BOOLEAN NOT NULL DEFAULT 0;"
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

            # pending_candidate_json for manual grab fallback
            for tbl in ["media_items", "seasons", "episodes"]:
                try:
                    await session.execute(
                        __import__("sqlalchemy").text(
                            f"ALTER TABLE {tbl} ADD COLUMN pending_candidate_json JSON;"
                        )
                    )
                except Exception:
                    pass

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

            # Upgrade attempts migrations
            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE system_settings ADD COLUMN max_upgrade_attempts INTEGER NOT NULL DEFAULT 7;"
                    )
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE system_settings ADD COLUMN self_healing_interval INTEGER NOT NULL DEFAULT 15;"
                    )
                )
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE system_settings ADD COLUMN video_search_interval INTEGER NOT NULL DEFAULT 60;"
                    )
                )
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE system_settings ADD COLUMN print_search_interval INTEGER NOT NULL DEFAULT 60;"
                    )
                )
            except Exception:
                pass

            for tbl in [
                "media_items",
                "seasons",
                "episodes",
                "book_items",
                "manga_items",
                "manga_volumes",
                "magazine_subscriptions",
            ]:
                try:
                    await session.execute(
                        __import__("sqlalchemy").text(
                            f"ALTER TABLE {tbl} ADD COLUMN upgrade_attempts_count INTEGER NOT NULL DEFAULT 0;"
                        )
                    )
                except Exception:
                    pass

            for tbl in [
                "book_items",
                "manga_items",
                "manga_volumes",
                "magazine_subscriptions",
            ]:
                try:
                    await session.execute(
                        __import__("sqlalchemy").text(
                            f"ALTER TABLE {tbl} ADD COLUMN best_score FLOAT;"
                        )
                    )
                except Exception:
                    pass

            # Upgrade throttling & search interval migration (Ticket 01)
            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE system_settings ADD COLUMN upgrade_search_interval_hours INTEGER NOT NULL DEFAULT 24;"
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

            # Timestamps for last upgrade search
            for tbl in [
                "media_items",
                "seasons",
                "episodes",
                "book_items",
                "manga_volumes",
            ]:
                try:
                    await session.execute(
                        __import__("sqlalchemy").text(
                            f"ALTER TABLE {tbl} ADD COLUMN last_upgrade_search_at DATETIME;"
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

            # Provider profile language fields (ADR-057)
            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE provider_profiles ADD COLUMN primary_language VARCHAR(50);"
                    )
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "ALTER TABLE provider_profiles ADD COLUMN fallback_language VARCHAR(50);"
                    )
                )
            except Exception:
                pass

            # Backfill legacy languages_csv to primary_language
            try:
                await session.execute(
                    __import__("sqlalchemy").text(
                        "UPDATE provider_profiles SET primary_language = languages_csv WHERE languages_csv IS NOT NULL AND primary_language IS NULL;"
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
                        "UPDATE providers SET category = 'print_media' WHERE type IN ('hardcover', 'openlibrary');"
                    )
                )
                await session.execute(
                    __import__("sqlalchemy").text(
                        "UPDATE providers SET category = 'metadata' WHERE type = 'anilist';"
                    )
                )
                await session.execute(
                    __import__("sqlalchemy").text(
                        "UPDATE providers SET category = 'watchlist' WHERE type = 'simkl';"
                    )
                )
            except Exception:
                pass

            # Auto-seed providers from legacy SystemSettings
            try:
                from sqlalchemy import select

                from app.db.models import Provider, ProviderCategory, SystemSettings

                settings_res = await session.execute(
                    select(SystemSettings).where(SystemSettings.id == 1)
                )
                db_settings = settings_res.scalars().first()

                if db_settings:
                    prov_res = await session.execute(select(Provider))
                    existing_providers = prov_res.scalars().all()
                    existing_types = {p.type for p in existing_providers}

                    # TMDB
                    if (
                        db_settings.tmdb_api_key
                        and db_settings.tmdb_api_key != "your_tmdb_api_v3_key_here"
                        and "tmdb" not in existing_types
                    ):
                        tmdb_p = Provider(
                            category=ProviderCategory.METADATA.value,
                            type="tmdb",
                            name="The Movie Database (TMDB)",
                            api_key=db_settings.tmdb_api_key,
                            is_active=True,
                        )
                        session.add(tmdb_p)

                    # TorBox
                    if (
                        db_settings.torbox_api_key
                        and db_settings.torbox_api_key != "your_torbox_api_key_here"
                        and "torbox" not in existing_types
                    ):
                        torbox_p = Provider(
                            category=ProviderCategory.DOWNLOADER.value,
                            type="torbox",
                            name="TorBox Downloader",
                            api_key=db_settings.torbox_api_key,
                            is_active=True,
                        )
                        session.add(torbox_p)

                    # Treasure Maps
                    if (
                        db_settings.treasure_maps_api_key
                        and db_settings.treasure_maps_api_key
                        != "your_newznab_api_key_here"
                        and "treasure_maps" not in existing_types
                    ):
                        tm_p = Provider(
                            category=ProviderCategory.INDEXER.value,
                            type="treasure_maps",
                            name="Treasure Maps Indexer",
                            api_key=db_settings.treasure_maps_api_key,
                            api_url="https://treasure-maps.com/api",
                            priority=1,
                            is_active=True,
                        )
                        session.add(tm_p)
            except Exception as e:
                logger.warning("Error migrating legacy provider settings: %s", e)

            # --- Domain migration: treasuremaps.net → treasure-maps.com ---
            try:
                from sqlalchemy import func, update

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
