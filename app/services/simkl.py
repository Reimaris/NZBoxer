"""
Simkl API Client
================
Fetches the user's "Plan to Watch" (watchlist) for movies and shows.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

SIMKL_BASE_URL = "https://api.simkl.com"


class SimklError(Exception):
    """Base exception for Simkl API errors."""
    pass


async def get_watchlist(media_type: str = "movies") -> list[dict[str, Any]]:
    """Fetch the 'plantowatch' list for movies or shows.

    Args:
        media_type: 'movies' or 'shows'.

    Returns:
        List of dictionaries containing item metadata from Simkl.
    """
    if not settings.simkl_client_id or not settings.simkl_access_token:
        logger.warning("Simkl API keys missing; returning empty watchlist.")
        return []

    url = f"{SIMKL_BASE_URL}/sync/all-items/{media_type}/plantowatch"
    headers = {
        "simkl-api-key": settings.simkl_client_id,
        "Authorization": f"Bearer {settings.simkl_access_token}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            # The API returns {"movies": [...]} or {"shows": [...]}
            return data.get(media_type, [])
            
        except httpx.HTTPError as e:
            logger.error("Simkl API error: %s", e)
            raise SimklError(f"Simkl sync failed: {e}") from e
