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

    # TorBox Transfer Poller & Failure Recovery
    sh_max_retries: int = int(os.getenv("SH_MAX_RETRIES", "3"))
    download_timeout_hours: int = int(os.getenv("DOWNLOAD_TIMEOUT_HOURS", "24"))

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
    notify_on_errors: bool = os.getenv("NOTIFY_ON_ERRORS", "True").lower() == "true"

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
            sh_max_retries=settings.sh_max_retries,
            download_timeout_hours=settings.download_timeout_hours,
            discord_webhook_url=settings.discord_webhook_url,
            discord_enabled=settings.discord_enabled,
            notify_on_push_initiated=settings.notify_on_push_initiated,
            notify_on_completed=settings.notify_on_completed,
            notify_on_failure=settings.notify_on_failure,
            notify_on_auto_advance=settings.notify_on_auto_advance,
            notify_on_errors=settings.notify_on_errors,
            scoring_settings=scoring_config,
        )
        session.add(db_settings)
        await session.commit()
        await session.refresh(db_settings)

    # Overwrite global settings cache
    settings.sh_max_retries = db_settings.sh_max_retries
    settings.download_timeout_hours = getattr(
        db_settings, "download_timeout_hours", settings.download_timeout_hours
    )
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
    settings.notify_on_errors = bool(getattr(db_settings, "notify_on_errors", True))

    import copy

    authoritative_cfg = copy.deepcopy(DEFAULT_SCORING_CONFIG)
    if isinstance(db_settings.scoring_settings, dict):
        if "manual_search_defaults" in db_settings.scoring_settings:
            authoritative_cfg["manual_search_defaults"] = copy.deepcopy(
                db_settings.scoring_settings["manual_search_defaults"]
            )
    scoring_config.clear()
    scoring_config.update(authoritative_cfg)
    if db_settings.scoring_settings != authoritative_cfg:
        db_settings.scoring_settings = copy.deepcopy(authoritative_cfg)
        await session.commit()
