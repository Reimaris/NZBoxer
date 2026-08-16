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

import logging
import os
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from app.core.default_scoring import DEFAULT_SCORING_CONFIG

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class Settings:
    """Application settings loaded from environment variables.

    All settings have sensible defaults so the app can start without a
    fully configured .env (useful for testing).
    """

    # --- Database ---
    # Default to a local SQLite database in the config volume mapping
    database_url: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./config/nzboxer.db")

    # --- API Keys (Now populated entirely from the DB on startup) ---
    tmdb_api_key: str = ""
    treasure_maps_api_key: str = ""
    torbox_api_key: str = ""

    # Self-Healing & Automation
    automation_state: str = "active"
    scan_interval_multiplier: int = int(os.getenv("SCAN_INTERVAL_MULTIPLIER", "1"))
    sh_max_retries: int = int(os.getenv("SH_MAX_RETRIES", "3"))
    sh_max_time_hours: float = float(os.getenv("SH_MAX_TIME_HOURS", "12.0"))
    sh_auto_retry: bool = os.getenv("SH_AUTO_RETRY", "True").lower() == "true"
    sh_retry_wait_hours: float = float(os.getenv("SH_RETRY_WAIT_HOURS", "24.0"))
    dry_run: bool = os.getenv("DRY_RUN", "False").lower() == "true"

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
    db_settings = result.scalar_one_or_none()

    if not db_settings:
        # Create default from current environment/defaults if not in DB
        db_settings = SystemSettings(
            id=1,
            tmdb_api_key=settings.tmdb_api_key,
            treasure_maps_api_key=settings.treasure_maps_api_key,
            torbox_api_key=settings.torbox_api_key,
            scan_interval_multiplier=settings.scan_interval_multiplier,
            sh_max_retries=settings.sh_max_retries,
            sh_max_time_hours=settings.sh_max_time_hours,
            sh_auto_retry=settings.sh_auto_retry,
            sh_retry_wait_hours=settings.sh_retry_wait_hours,
            dry_run=settings.dry_run,
            automation_state=settings.automation_state,
            scoring_settings=scoring_config
        )
        session.add(db_settings)
        await session.commit()
        await session.refresh(db_settings)

    # Overwrite global settings cache
    settings.tmdb_api_key = db_settings.tmdb_api_key
    settings.treasure_maps_api_key = db_settings.treasure_maps_api_key
    settings.torbox_api_key = db_settings.torbox_api_key
    settings.scan_interval_multiplier = db_settings.scan_interval_multiplier
    settings.sh_max_retries = db_settings.sh_max_retries
    settings.sh_max_time_hours = db_settings.sh_max_time_hours
    settings.sh_auto_retry = db_settings.sh_auto_retry
    settings.sh_retry_wait_hours = db_settings.sh_retry_wait_hours
    settings.dry_run = db_settings.dry_run
    settings.automation_state = db_settings.automation_state.value if hasattr(db_settings.automation_state, 'value') else db_settings.automation_state

    # Overwrite scoring config cache
    if db_settings.scoring_settings:
        scoring_config.clear()
        scoring_config.update(db_settings.scoring_settings)
