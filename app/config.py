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
from typing import Any

import yaml
from dotenv import load_dotenv

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

    # --- Simkl ---
    simkl_client_id: str = os.getenv("SIMKL_CLIENT_ID", "")
    simkl_access_token: str = os.getenv("SIMKL_ACCESS_TOKEN", "")

    # --- TMDB ---
    tmdb_api_key: str = os.getenv("TMDB_API_KEY", "")

    # --- Treasure Maps / Newznab ---
    treasure_maps_url: str = os.getenv("TREASURE_MAPS_URL", "")
    treasure_maps_api_key: str = os.getenv("TREASURE_MAPS_API_KEY", "")

    # --- TorBox ---
    torbox_api_key: str = os.getenv("TORBOX_API_KEY", "")

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
