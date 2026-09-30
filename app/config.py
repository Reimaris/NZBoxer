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

DEFAULT_USER_AGENT = "NZBoxer/3.0.0 (Linux; x64)"


class Settings:
    """Application settings loaded from environment variables.

    All settings have sensible defaults so the app can start without a
    fully configured .env (useful for testing).
    """

    # --- Database ---
    database_url: str = os.getenv(
        "DATABASE_URL", "sqlite+aiosqlite:///./config/nzboxer.db"
    )

    # --- API Keys (Populated from DB on startup) ---
    tmdb_api_key: str = ""
    treasure_maps_api_key: str = ""
    torbox_api_key: str = ""

    # Self-Healing & Active Push Monitoring
    scan_interval_multiplier: int = int(os.getenv("SCAN_INTERVAL_MULTIPLIER", "1"))
    sh_max_retries: int = int(os.getenv("SH_MAX_RETRIES", "3"))
    sh_max_time_hours: float = float(os.getenv("SH_MAX_TIME_HOURS", "12.0"))
    sh_auto_retry: bool = os.getenv("SH_AUTO_RETRY", "True").lower() == "true"
    sh_retry_wait_hours: float = float(os.getenv("SH_RETRY_WAIT_HOURS", "24.0"))
    download_timeout_hours: int = int(os.getenv("DOWNLOAD_TIMEOUT_HOURS", "24"))
    dry_run: bool = os.getenv("DRY_RUN", "False").lower() == "true"

    # Discord & Event Notification Settings (v3.0.0)
    discord_webhook_url: str | None = os.getenv("DISCORD_WEBHOOK_URL") or None
    discord_enabled: bool = os.getenv("DISCORD_ENABLED", "False").lower() == "true"
    notify_on_push_initiated: bool = (
        os.getenv("NOTIFY_ON_PUSH_INITIATED", "False").lower() == "true"
    )
    notify_on_completed: bool = (
        os.getenv("NOTIFY_ON_COMPLETED", "True").lower() == "true"
    )
    notify_on_failure: bool = os.getenv("NOTIFY_ON_FAILURE", "True").lower() == "true"
    notify_on_auto_advance: bool = (
        os.getenv("NOTIFY_ON_AUTO_ADVANCE", "True").lower() == "true"
    )

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
    """Load and cache the default scoring configuration."""
    import copy

    return copy.deepcopy(DEFAULT_SCORING_CONFIG)


# Convenience module-level singletons
settings: Settings = get_settings()
scoring_config: dict[str, Any] = get_scoring_config()


async def reload_settings_from_db(session: AsyncSession) -> None:
    """Load settings from the SQLite database and update the global cache."""
    from sqlalchemy import select

    from app.db.models import SystemSettings

    stmt = select(SystemSettings).where(SystemSettings.id == 1)
    result = await session.execute(stmt)
    db_settings = result.scalars().first()

    if not db_settings:
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
            download_timeout_hours=settings.download_timeout_hours,
            dry_run=settings.dry_run,
            discord_webhook_url=settings.discord_webhook_url,
            discord_enabled=settings.discord_enabled,
            notify_on_push_initiated=settings.notify_on_push_initiated,
            notify_on_completed=settings.notify_on_completed,
            notify_on_failure=settings.notify_on_failure,
            notify_on_auto_advance=settings.notify_on_auto_advance,
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
    settings.sh_max_retries = db_settings.sh_max_retries
    settings.sh_max_time_hours = db_settings.sh_max_time_hours
    settings.sh_auto_retry = db_settings.sh_auto_retry
    settings.sh_retry_wait_hours = db_settings.sh_retry_wait_hours
    settings.download_timeout_hours = getattr(
        db_settings, "download_timeout_hours", settings.download_timeout_hours
    )
    settings.dry_run = db_settings.dry_run
    settings.discord_webhook_url = getattr(db_settings, "discord_webhook_url", None)
    settings.discord_enabled = bool(getattr(db_settings, "discord_enabled", False))
    settings.notify_on_push_initiated = bool(
        getattr(db_settings, "notify_on_push_initiated", False)
    )
    settings.notify_on_completed = bool(
        getattr(db_settings, "notify_on_completed", True)
    )
    settings.notify_on_failure = bool(getattr(db_settings, "notify_on_failure", True))
    settings.notify_on_auto_advance = bool(
        getattr(db_settings, "notify_on_auto_advance", True)
    )

    if db_settings.scoring_settings:
        scoring_config.clear()
        scoring_config.update(db_settings.scoring_settings)
