"""
NZBoxer Application Configuration
====================================
Loads settings from environment variables (.env) and the scoring/automation
config from config.yaml. Provides a single validated settings object to the
rest of the application.

Usage::

    from app.config import settings, scoring_config

    print(settings.database_url)
    print(scoring_config["scoring"]["resolution"]["1080p"])
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

# We need a forward reference for typing if needed, but we'll import locally in the function
from typing import TYPE_CHECKING, Any

import yaml
from dotenv import load_dotenv

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Load .env file from the project root (one level up from app/)
_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")

logger = logging.getLogger(__name__)


class Settings:
    """Application settings loaded from environment variables.

    All settings have sensible defaults so the app can start without a
    fully configured .env (useful for testing).
    """

    # --- Database ---
    database_url: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./nzboxer.db")

    # --- TMDB ---
    tmdb_api_key: str = os.getenv("TMDB_API_KEY", "")

    treasure_maps_api_key: str = os.getenv("TREASURE_MAPS_API_KEY", "")

    # --- TorBox ---
    torbox_api_key: str = os.getenv("TORBOX_API_KEY", "")

    # Self-Healing & Automation
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
    """Load and cache the config.yaml scoring/automation configuration.

    Returns:
        dict: The parsed YAML content.

    Raises:
        FileNotFoundError: If config.yaml does not exist at the project root.
        yaml.YAMLError: If config.yaml contains invalid YAML syntax.
    """
    config_path = _ROOT / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {config_path}. "
            "Please ensure it exists in the project root."
        )
    with config_path.open("r", encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    logger.info("Loaded config.yaml from %s", config_path)
    return data


# Convenience module-level singletons
settings: Settings = get_settings()
scoring_config: dict[str, Any] = get_scoring_config()


async def reload_settings_from_db(session: AsyncSession) -> None:
    """Load settings from the SQLite database and update the global cache.
    
    If the SystemSettings row does not exist, it will be created using the
    current in-memory defaults (which were loaded from .env/config.yaml).
    """
    from sqlalchemy import select

    from app.db.models import SystemSettings

    stmt = select(SystemSettings).where(SystemSettings.id == 1)
    result = await session.execute(stmt)
    db_settings = result.scalar_one_or_none()

    if not db_settings:
        # Create default from current environment/yaml if not in DB
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

    # Overwrite scoring config cache
    if db_settings.scoring_settings:
        scoring_config.clear()
        scoring_config.update(db_settings.scoring_settings)

