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
from app.core.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)
_limiter = RateLimiter(0.6)



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
    params: dict[str, Any] = {
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
    params: dict[str, Any] = {
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
    await _limiter.wait()
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()

            data = response.json()
            channel = data.get("channel", {})
            items = channel.get("item", [])

            if isinstance(items, dict):
                # Sometimes a single result is returned as a dict rather than a list
                items = [items]

            # Flatten the nested attr/@attributes structure into direct fields.
            # Treasure Maps (Newznab JSON) puts size/guid/category inside:
            # "attr": [{"@attributes": {"name": "size", "value": "12345"}}, ...]
            normalized = []
            for item in items:
                flat = dict(item)

                # Parse attr list
                attrs = item.get("attr", [])
                if isinstance(attrs, dict):
                    attrs = [attrs]
                attr_map: dict[str, str] = {}
                for a in attrs:
                    a_attrs = a.get("@attributes", {})
                    name = a_attrs.get("name")
                    value = a_attrs.get("value")
                    if name and value is not None:
                        attr_map[name] = value

                # Map known attr fields to flat keys
                if "size" not in flat or not flat["size"]:
                    flat["size"] = attr_map.get("size", 0)
                if "guid" not in flat or flat["guid"].startswith("http"):
                    # The 'guid' in attr is the hash, the top-level 'guid' is a URL
                    flat["guid"] = attr_map.get("guid", flat.get("guid", ""))

                # Fallback: size from enclosure length
                if not flat["size"]:
                    enc = item.get("enclosure", {})
                    enc_attrs = enc.get("@attributes", {}) if isinstance(enc, dict) else {}
                    flat["size"] = enc_attrs.get("length", 0)

                # Use direct link for download URL
                if not flat.get("link"):
                    enc = item.get("enclosure", {})
                    enc_attrs = enc.get("@attributes", {}) if isinstance(enc, dict) else {}
                    flat["link"] = enc_attrs.get("url", "")

                flat["size"] = int(flat["size"]) if flat["size"] else 0
                normalized.append(flat)

            return normalized
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
