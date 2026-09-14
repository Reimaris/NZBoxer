"""
NZBoxer Application Configuration
====================================
Loads settings from environment variables and the database.
Provides a single validated settings object to the rest of the application.

Usage::

    from app.config import settings, scoring_config

    print(settings.database_url)
    print(scoring_config["scoring"]["resolution"]["1080p"])
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from app.core.default_scoring import DEFAULT_SCORING_CONFIG

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_USER_AGENT = "NZBoxer/2.8.0 (Linux; x64)"


class Settings:
    """Application settings loaded from environment variables.

    All settings have sensible defaults so the app can start without a
    fully configured .env (useful for testing).
    """

    # --- Database ---
    # Default to a local SQLite database in the config volume mapping
    database_url: str = os.getenv(
        "DATABASE_URL", "sqlite+aiosqlite:///./config/nzboxer.db"
    )

    # --- API Keys (Now populated entirely from the DB on startup) ---
    tmdb_api_key: str = ""
    treasure_maps_api_key: str = ""
    torbox_api_key: str = ""

    # Self-Healing & Automation
    automation_state: str = "active"
    scan_interval_multiplier: int = int(os.getenv("SCAN_INTERVAL_MULTIPLIER", "1"))
    self_healing_interval: int = int(os.getenv("SELF_HEALING_INTERVAL", "15"))
    video_search_interval: int = int(os.getenv("VIDEO_SEARCH_INTERVAL", "60"))
    print_search_interval: int = int(os.getenv("PRINT_SEARCH_INTERVAL", "60"))
    sh_max_retries: int = int(os.getenv("SH_MAX_RETRIES", "3"))
    sh_max_time_hours: float = float(os.getenv("SH_MAX_TIME_HOURS", "12.0"))
    sh_auto_retry: bool = os.getenv("SH_AUTO_RETRY", "True").lower() == "true"
    sh_retry_wait_hours: float = float(os.getenv("SH_RETRY_WAIT_HOURS", "24.0"))
    upgrade_search_interval_hours: int = int(
        os.getenv("UPGRADE_SEARCH_INTERVAL_HOURS", "24")
    )
    max_upgrade_attempts: int = int(os.getenv("MAX_UPGRADE_ATTEMPTS", "7"))
    backoff_tier2_skip: int = int(os.getenv("BACKOFF_TIER2_SKIP", "6"))
    backoff_tier3_skip: int = int(os.getenv("BACKOFF_TIER3_SKIP", "24"))
    dry_run: bool = os.getenv("DRY_RUN", "False").lower() == "true"
    auto_grab_title_fallbacks: bool = (
        os.getenv("AUTO_GRAB_TITLE_FALLBACKS", "False").lower() == "true"
    )

    # Print Media Configuration
    reading_download_dir: str = os.getenv("READING_DOWNLOAD_DIR", "downloads")
    manga_blacklisted_formats: str = os.getenv("MANGA_BLACKLISTED_FORMATS", "")
    book_blacklisted_formats: str = os.getenv("BOOK_BLACKLISTED_FORMATS", "")
    magazine_blacklisted_formats: str = os.getenv("MAGAZINE_BLACKLISTED_FORMATS", "")

    # --- App ---
    app_env: str = os.getenv("APP_ENV", "development")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    def is_development(self) -> bool:
        return self.app_env.lower() == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the singleton Settings instance (cached after first call)."""
    return Settings()


@lru_cache(maxsize=1)
def get_scoring_config() -> dict[str, Any]:
    """Load and cache the default scoring configuration.

    Returns:
        dict: The default configuration dict.
    """
    # Create a fresh copy to prevent mutating the default imported dict
    import copy

    return copy.deepcopy(DEFAULT_SCORING_CONFIG)


# Convenience module-level singletons
settings: Settings = get_settings()
scoring_config: dict[str, Any] = get_scoring_config()


async def reload_settings_from_db(session: AsyncSession) -> None:
    """Load settings from the SQLite database and update the global cache.

    If the SystemSettings row does not exist, it will be created using the
    current in-memory defaults.
    """
    from sqlalchemy import select

    from app.db.models import SystemSettings

    stmt = select(SystemSettings).where(SystemSettings.id == 1)
    result = await session.execute(stmt)
    db_settings = result.scalars().first()

    if not db_settings:
        # Create default from current environment/defaults if not in DB
        db_settings = SystemSettings(
            id=1,
            tmdb_api_key=settings.tmdb_api_key,
            treasure_maps_api_key=settings.treasure_maps_api_key,
            torbox_api_key=settings.torbox_api_key,
            scan_interval_multiplier=settings.scan_interval_multiplier,
            self_healing_interval=settings.self_healing_interval,
            video_search_interval=settings.video_search_interval,
            print_search_interval=settings.print_search_interval,
            sh_max_retries=settings.sh_max_retries,
            sh_max_time_hours=settings.sh_max_time_hours,
            sh_auto_retry=settings.sh_auto_retry,
            sh_retry_wait_hours=settings.sh_retry_wait_hours,
            upgrade_search_interval_hours=settings.upgrade_search_interval_hours,
            max_upgrade_attempts=settings.max_upgrade_attempts,
            backoff_tier2_skip=settings.backoff_tier2_skip,
            backoff_tier3_skip=settings.backoff_tier3_skip,
            dry_run=settings.dry_run,
            auto_grab_title_fallbacks=settings.auto_grab_title_fallbacks,
            automation_state=settings.automation_state,
            scoring_settings=scoring_config,
        )
        session.add(db_settings)
        await session.commit()
        await session.refresh(db_settings)

    # Overwrite global settings cache
    settings.tmdb_api_key = db_settings.tmdb_api_key
    settings.treasure_maps_api_key = db_settings.treasure_maps_api_key
    settings.torbox_api_key = db_settings.torbox_api_key
    settings.scan_interval_multiplier = db_settings.scan_interval_multiplier
    settings.self_healing_interval = getattr(
        db_settings, "self_healing_interval", settings.self_healing_interval
    )
    settings.video_search_interval = getattr(
        db_settings, "video_search_interval", settings.video_search_interval
    )
    settings.print_search_interval = getattr(
        db_settings, "print_search_interval", settings.print_search_interval
    )
    settings.sh_max_retries = db_settings.sh_max_retries
    settings.sh_max_time_hours = db_settings.sh_max_time_hours
    settings.sh_auto_retry = db_settings.sh_auto_retry
    settings.sh_retry_wait_hours = db_settings.sh_retry_wait_hours
    settings.upgrade_search_interval_hours = getattr(
        db_settings,
        "upgrade_search_interval_hours",
        settings.upgrade_search_interval_hours,
    )
    settings.max_upgrade_attempts = db_settings.max_upgrade_attempts
    settings.backoff_tier2_skip = getattr(
        db_settings, "backoff_tier2_skip", settings.backoff_tier2_skip
    )
    settings.backoff_tier3_skip = getattr(
        db_settings, "backoff_tier3_skip", settings.backoff_tier3_skip
    )
    settings.dry_run = db_settings.dry_run
    settings.auto_grab_title_fallbacks = getattr(
        db_settings, "auto_grab_title_fallbacks", False
    )
    settings.automation_state = (
        db_settings.automation_state.value
        if hasattr(db_settings.automation_state, "value")
        else db_settings.automation_state
    )
    if (
        hasattr(db_settings, "reading_download_dir")
        and db_settings.reading_download_dir
    ):
        settings.reading_download_dir = db_settings.reading_download_dir
    if hasattr(db_settings, "manga_blacklisted_formats"):
        settings.manga_blacklisted_formats = db_settings.manga_blacklisted_formats or ""
    if hasattr(db_settings, "book_blacklisted_formats"):
        settings.book_blacklisted_formats = db_settings.book_blacklisted_formats or ""
    if hasattr(db_settings, "magazine_blacklisted_formats"):
        settings.magazine_blacklisted_formats = (
            db_settings.magazine_blacklisted_formats or ""
        )

    # Overwrite scoring config cache
    if db_settings.scoring_settings:
        scoring_config.clear()
        scoring_config.update(db_settings.scoring_settings)
