"""
Treasure Maps (Newznab) API Client
==================================
Searches the configured Usenet indexer for NZB releases.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class IndexerError(Exception):
    pass


async def search_movie(imdb_id: str, category: int | None = None) -> list[dict[str, Any]]:
    """Search for a movie on the Newznab indexer.

    Args:
        imdb_id: The IMDb ID, e.g., 'tt1234567' or '1234567'.
        category: Optional TreasureMaps category ID (e.g. 2000).

    Returns:
        A list of search result items (dict).
    """
    if not settings.treasure_maps_api_key:
        logger.warning("Indexer API key missing.")
        return []

    # Newznab usually expects the IMDB ID without the 'tt' prefix
    imdb_id = imdb_id.removeprefix("tt")

    url = "https://treasure-maps.com/api"
    params = {
        "apikey": settings.treasure_maps_api_key,
        "t": "movie",
        "imdbid": imdb_id,
        "o": "json"
    }
    
    if category:
        params["cat"] = category

    return await _execute_search(url, params)


async def search_show(tvdb_id: str | int | None, title: str, season: int, ep: int | str | None = None, category: int | None = None) -> list[dict[str, Any]]:
    """Search for a TV show season or episode on the Newznab indexer.

    Args:
        tvdb_id: Optional TVDB ID.
        title: Show title (used if tvdb_id is not available or for general search).
        season: Season number to search.
        ep: Optional episode number or string (e.g. '1,2,3' or '1').
        category: Optional TreasureMaps category ID (e.g. 5000).

    Returns:
        List of search result items.
    """
    if not settings.treasure_maps_api_key:
        logger.warning("Indexer API key missing.")
        return []

    url = "https://treasure-maps.com/api"
    params = {
        "apikey": settings.treasure_maps_api_key,
        "t": "tvsearch",
        "season": season,
        "o": "json"
    }

    if ep:
        params["ep"] = ep
        
    if category:
        params["cat"] = category

    if tvdb_id:
        params["tvdbid"] = tvdb_id
    else:
        params["q"] = title

    return await _execute_search(url, params)


async def _execute_search(url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Execute the search and parse the JSON results."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            
            # Note: Depending on the Newznab implementation, the JSON format can vary slightly.
            # Typical format: {"channel": {"item": [...]}}
            data = response.json()
            channel = data.get("channel", {})
            items = channel.get("item", [])
            
            if isinstance(items, dict):
                # Sometimes a single result is returned as a dict rather than a list
                items = [items]
                
            return items
        except httpx.HTTPError as e:
            logger.error("Indexer search failed: %s", e)
            return []
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to parse indexer results: %s", e)
            return []


async def get_download_url(guid: str) -> str:
    """Generate the download URL for a specific NZB given its GUID/ID."""
    url = "https://treasure-maps.com/api"
    params = {
        "t": "get",
        "id": guid,
        "apikey": settings.treasure_maps_api_key
    }
    # Rather than executing it, we can just construct the URL,
    # as the TorBox service usually takes a URL or we need to download it first.
    # We will just return the built URL for the downloader.
    request = httpx.Request("GET", url, params=params)
    return str(request.url)
