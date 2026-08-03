"""
Simkl API Client
================
Fetches the user's "Plan to Watch" (watchlist) for movies and shows.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)
_limiter = RateLimiter(1.0)

SIMKL_BASE_URL = "https://api.simkl.com"



class SimklError(Exception):
    """Base exception for Simkl API errors."""


async def request_pin(client_id: str) -> dict[str, Any]:
    """Request a PIN code from Simkl for device authorization."""
    url = f"{SIMKL_BASE_URL}/oauth/pin"
    params = {
        "client_id": client_id,
        "app-name": "nzboxer",
        "app-version": "0.1.0"
    }
    await _limiter.wait()
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


async def check_pin(client_id: str, user_code: str) -> dict[str, Any]:
    """Poll Simkl to see if the user has entered the PIN."""
    url = f"{SIMKL_BASE_URL}/oauth/pin/{user_code}"
    params = {
        "client_id": client_id,
        "app-name": "nzboxer",
        "app-version": "0.1.0"
    }
    await _limiter.wait()
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        return response.json()


async def get_watchlist(media_type: str, client_id: str, access_token: str) -> list[dict[str, Any]]:
    """Fetch the 'plantowatch' and 'watching' list for movies or shows.

    Args:
        media_type: 'movies' or 'shows'.
        client_id: Simkl Client ID
        access_token: Simkl Access Token

    Returns:
        List of dictionaries containing item metadata from Simkl.
    """
    if not client_id or not access_token:
        logger.warning("Simkl API keys missing; returning empty watchlist.")
        return []

    params = {
        "client_id": client_id,
        "app-name": "nzboxer",
        "app-version": "0.1.0"
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    statuses = ["plantowatch", "watching"]
    all_items = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        for status in statuses:
            url = f"{SIMKL_BASE_URL}/sync/all-items/{media_type}/{status}"
            await _limiter.wait()
            try:
                response = await client.get(url, params=params, headers=headers)
                response.raise_for_status()
                data = response.json()
                items = data.get(media_type, [])
                all_items.extend(items)
            except Exception as e:
                logger.error("Simkl API error for %s %s: %s", media_type, status, e)
                raise SimklError(f"Simkl sync failed for {media_type} {status}: {e}") from e

    return all_items
