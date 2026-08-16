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


async def search_movie(imdb_id: str | None = None, tmdb_id: int | None = None, title: str | None = None, category: int | None = None) -> list[dict[str, Any]]:
    """Search for a movie on the Newznab indexer.

    Args:
        imdb_id: The IMDb ID, e.g., 'tt1234567' or '1234567'.
        tmdb_id: The TMDB ID.
        title: Movie title for fallback search.
        category: Optional TreasureMaps category ID (e.g. 2000).

    Returns:
        A list of search result items (dict).
    """
    if not settings.treasure_maps_api_key:
        logger.warning("Indexer API key missing.")
        return []

    url = "https://treasure-maps.com/api"
    params: dict[str, Any] = {
        "apikey": settings.treasure_maps_api_key,
        "t": "movie",
        "o": "json"
    }

    if imdb_id:
        params["imdbid"] = imdb_id.removeprefix("tt")
    elif tmdb_id:
        params["tmdbid"] = tmdb_id
    elif title:
        params["q"] = title

    if category:
        params["cat"] = category

    return await _execute_search(url, params)


async def search_show(tvdb_id: str | int | None = None, tmdb_id: int | None = None, imdb_id: str | None = None, title: str | None = None, season: int | None = None, ep: int | str | None = None, category: int | None = None) -> list[dict[str, Any]]:
    """Search for a TV show season or episode on the Newznab indexer.

    Args:
        tvdb_id: Optional TVDB ID.
        tmdb_id: Optional TMDB ID.
        imdb_id: Optional IMDB ID.
        title: Show title (used if tvdb_id is not available or for general search).
        season: Season number to search.
        ep: Optional episode number or string (e.g. '1,2,3' or '1').
        category: Optional TreasureMaps category ID (e.g. 5000 or 5070 for Anime).

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
        "o": "json"
    }

    if season is not None:
        params["season"] = season
    if ep:
        params["ep"] = ep
    if category:
        params["cat"] = category

    if tvdb_id:
        params["tvdbid"] = tvdb_id
    elif tmdb_id:
        params["tmdbid"] = tmdb_id
    elif imdb_id:
        # Some indexers support imdbid for tvsearch too
        params["imdbid"] = imdb_id.replace("tt", "") if isinstance(imdb_id, str) else imdb_id
        
    if title:
        params["q"] = title

    return await _execute_search(url, params)


async def search_raw(
    query: str | None = None, 
    category: int | None = None,
    season: int | None = None,
    ep: int | str | None = None,
    imdb_id: str | None = None,
    tmdb_id: int | None = None,
    tvdb_id: int | str | None = None
) -> list[dict[str, Any]]:
    """Generic search on the Newznab indexer.

    Args:
        query: Search string.
        category: Optional TreasureMaps category ID.
        season: Optional season number.
        ep: Optional episode number.
        imdb_id: Optional IMDB ID.
        tmdb_id: Optional TMDB ID.
        tvdb_id: Optional TVDB ID.

    Returns:
        List of search result items.
    """
    if not settings.treasure_maps_api_key:
        logger.warning("Indexer API key missing.")
        return []

    url = "https://treasure-maps.com/api"
    
    # Dynamically determine the best 't' parameter based on what fields we have
    search_type = "search"
    if season is not None or ep is not None or tvdb_id is not None:
        search_type = "tvsearch"
    elif imdb_id is not None and category == 2000:
        search_type = "movie"
        
    params: dict[str, Any] = {
        "apikey": settings.treasure_maps_api_key,
        "t": search_type,
        "o": "json",
    }
    
    if query:
        params["q"] = query
    if category:
        params["cat"] = category
    if season is not None:
        params["season"] = season
    if ep is not None:
        params["ep"] = ep
    if imdb_id:
        params["imdbid"] = imdb_id.replace("tt", "") if isinstance(imdb_id, str) else imdb_id
    if tmdb_id:
        params["tmdbid"] = tmdb_id
    if tvdb_id:
        params["tvdbid"] = tvdb_id

    return await _execute_search(url, params)


async def _execute_search(url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Execute the search and parse the JSON results."""
    await _limiter.wait()
    # Log the search URL for debugging (hide API key)
    log_params = {k: v for k, v in params.items() if k != "apikey"}
    request_for_log = httpx.Request("GET", url, params=log_params)
    logger.info("    🔍 Indexer-Suche: %s", str(request_for_log.url))
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
                
                # Extract language attribute if provided by indexer
                if "language" in attr_map:
                    flat["api_language"] = attr_map["language"]

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
