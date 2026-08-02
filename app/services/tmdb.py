"""
TMDB API Client
===============
Fetches release dates for movies (specifically digital release - type 4)
and metadata for TV shows (episode counts, first air dates).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TMDB_BASE_URL = "https://api.themoviedb.org/3"


class TMDBError(Exception):
    pass


async def get_digital_release_date(tmdb_id: int) -> datetime | None:
    """Fetch the earliest digital release date (type 4) for a movie.

    Args:
        tmdb_id: The TMDB movie ID.

    Returns:
        A timezone-aware datetime if found, else None.
    """
    if not settings.tmdb_api_key:
        logger.warning("TMDB API key missing.")
        return None

    url = f"{TMDB_BASE_URL}/movie/{tmdb_id}/release_dates"
    params = {"api_key": settings.tmdb_api_key}

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            # We are looking for release date type 4 (Digital)
            earliest_date = None
            
            for country_release in data.get("results", []):
                for release in country_release.get("release_dates", []):
                    if release.get("type") == 4:
                        date_str = release.get("release_date")
                        if date_str:
                            # Format: '2023-10-18T00:00:00.000Z'
                            try:
                                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                                if earliest_date is None or dt < earliest_date:
                                    earliest_date = dt
                            except ValueError:
                                pass

            return earliest_date
        except httpx.HTTPError as e:
            logger.error("TMDB release_dates error for %d: %s", tmdb_id, e)
            return None


async def get_show_details(tmdb_id: int) -> dict[str, Any] | None:
    """Fetch details for a TV show (number of seasons, etc.)."""
    if not settings.tmdb_api_key:
        logger.warning("TMDB API key missing.")
        return None

    url = f"{TMDB_BASE_URL}/tv/{tmdb_id}"
    params = {"api_key": settings.tmdb_api_key}

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error("TMDB tv details error for %d: %s", tmdb_id, e)
            return None
