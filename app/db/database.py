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

    from sqlalchemy.pool import StaticPool

    kwargs = {
        "echo": False,
        "future": True,
        "connect_args": {"check_same_thread": False},
    }
    
    if ":memory:" in database_url:
        kwargs["poolclass"] = StaticPool

    _engine = create_async_engine(database_url, **kwargs)

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
                    __import__("sqlalchemy").text("ALTER TABLE system_settings ADD COLUMN scoring_settings JSON;")
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text("ALTER TABLE system_settings ADD COLUMN tmdb_api_key VARCHAR(200) DEFAULT '';")
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text("ALTER TABLE media_items ADD COLUMN provider_id INTEGER DEFAULT 1;")
                )
                # Create a default provider if we just added the column, so foreign keys don't break
                await session.execute(
                    __import__("sqlalchemy").text("INSERT OR IGNORE INTO providers (id, type, name) VALUES (1, 'simkl', 'Default Simkl');")
                )
            except Exception:
                pass
                
            try:
                await session.execute(
                    __import__("sqlalchemy").text("ALTER TABLE download_history ADD COLUMN torbox_id VARCHAR(200);")
                )
            except Exception:
                pass
                
            try:
                await session.execute(
                    __import__("sqlalchemy").text("ALTER TABLE providers ADD COLUMN anime_category_id INTEGER;")
                )
            except Exception:
                pass
                
            try:
                await session.execute(
                    __import__("sqlalchemy").text("ALTER TABLE media_items ADD COLUMN tvdb_id INTEGER;")
                )
            except Exception:
                pass
                
            try:
                await session.execute(
                    __import__("sqlalchemy").text("ALTER TABLE media_items ADD COLUMN alt_title VARCHAR(500);")
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text("ALTER TABLE media_items ADD COLUMN is_anime_movie BOOLEAN NOT NULL DEFAULT 0;")
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text("ALTER TABLE download_history ADD COLUMN episode_id INTEGER;")
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text("ALTER TABLE system_settings ADD COLUMN dry_run BOOLEAN NOT NULL DEFAULT 0;")
                )
            except Exception:
                pass

            try:
                await session.execute(
                    __import__("sqlalchemy").text("ALTER TABLE provider_profiles ADD COLUMN search_cycle_skip INTEGER NOT NULL DEFAULT 1;")
                )
                await session.execute(
                    __import__("sqlalchemy").text("ALTER TABLE provider_profiles ADD COLUMN current_cycle_count INTEGER NOT NULL DEFAULT 0;")
                )
            except Exception:
                pass
                
            try:
                await session.execute(
                    __import__("sqlalchemy").text("ALTER TABLE media_items ADD COLUMN auto_monitor_next_season BOOLEAN NOT NULL DEFAULT 0;")
                )
            except Exception:
                pass
                
            # Ensure provider_profiles.auto_monitor_next_season exists (model compat col, value unused)
            try:
                await session.execute(
                    __import__("sqlalchemy").text("ALTER TABLE provider_profiles ADD COLUMN auto_monitor_next_season BOOLEAN NOT NULL DEFAULT 0;")
                )
            except Exception:
                pass

            for tbl in ["media_items", "seasons", "episodes"]:
                try:
                    await session.execute(
                        __import__("sqlalchemy").text(f"ALTER TABLE {tbl} ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0;")
                    )
                    await session.execute(
                        __import__("sqlalchemy").text(f"ALTER TABLE {tbl} ADD COLUMN last_error TEXT;")
                    )
                except Exception:
                    pass

            await session.execute(
                __import__("sqlalchemy").text("PRAGMA journal_mode=WAL;")
            )
            await session.execute(
                __import__("sqlalchemy").text("PRAGMA foreign_keys=ON;")
            )
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
