"""
Treasure Maps (Newznab) API Client
==================================
Searches the configured Usenet indexer for NZB releases.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from app.config import DEFAULT_USER_AGENT, settings
from app.core.rate_limiter import RateLimiter

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
_limiter = RateLimiter(0.6)


class IndexerError(Exception):
    pass


async def resolve_indexer(
    session: AsyncSession | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
) -> tuple[str, str]:
    """Resolve active indexer API URL and API key."""
    if api_url and api_key:
        return api_url, api_key

    from app.services.provider_service import get_active_indexers

    if session is not None:
        indexers = await get_active_indexers(session)
        if indexers and indexers[0].api_key:
            return (
                indexers[0].api_url or "https://treasure-maps.com/api",
                indexers[0].api_key or "",
            )
    else:
        from app.db.database import async_session_factory

        try:
            async with async_session_factory() as db:
                indexers = await get_active_indexers(db)
                if indexers and indexers[0].api_key:
                    return (
                        indexers[0].api_url or "https://treasure-maps.com/api",
                        indexers[0].api_key or "",
                    )
        except Exception:
            pass

    return (
        api_url or "https://treasure-maps.com/api",
        api_key or settings.treasure_maps_api_key or "",
    )


async def search_movie(
    imdb_id: str | None = None,
    tmdb_id: int | None = None,
    title: str | None = None,
    category: int | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """Search for a movie on the Newznab indexer.

    Args:
        imdb_id: The IMDb ID, e.g., 'tt1234567' or '1234567'.
        tmdb_id: The TMDB ID.
        title: Movie title for fallback search.
        category: Optional TreasureMaps category ID (e.g. 2000).
        api_url: Optional explicit indexer API URL.
        api_key: Optional explicit indexer API key.
        session: Optional DB session to resolve active provider.

    Returns:
        A list of search result items (dict).
    """
    resolved_url, resolved_key = await resolve_indexer(
        session=session, api_url=api_url, api_key=api_key
    )
    if not resolved_key:
        logger.warning("Indexer API key missing.")
        return []

    url = resolved_url
    params: dict[str, Any] = {
        "apikey": resolved_key,
        "t": "movie",
        "o": "json",
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


async def search_show(
    tvdb_id: str | int | None = None,
    tmdb_id: int | None = None,
    imdb_id: str | None = None,
    title: str | None = None,
    season: int | None = None,
    ep: int | str | None = None,
    category: int | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """Search for a TV show season or episode on the Newznab indexer.

    Args:
        tvdb_id: Optional TVDB ID.
        tmdb_id: Optional TMDB ID.
        imdb_id: Optional IMDB ID.
        title: Show title (used if tvdb_id is not available or for general search).
        season: Season number to search.
        ep: Optional episode number or string (e.g. '1,2,3' or '1').
        category: Optional TreasureMaps category ID (e.g. 5000 or 5070 for Anime).
        api_url: Optional explicit indexer API URL.
        api_key: Optional explicit indexer API key.
        session: Optional DB session to resolve active provider.

    Returns:
        List of search result items.
    """
    resolved_url, resolved_key = await resolve_indexer(
        session=session, api_url=api_url, api_key=api_key
    )
    if not resolved_key:
        logger.warning("Indexer API key missing.")
        return []

    url = resolved_url
    params: dict[str, Any] = {
        "apikey": resolved_key,
        "t": "tvsearch",
        "o": "json",
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
        params["imdbid"] = (
            imdb_id.replace("tt", "") if isinstance(imdb_id, str) else imdb_id
        )

    if title:
        params["q"] = title

    return await _execute_search(url, params)


async def search_raw(
    query: str | None = None,
    category: int | str | None = None,
    season: int | None = None,
    ep: int | str | None = None,
    imdb_id: str | None = None,
    tmdb_id: int | None = None,
    tvdb_id: int | str | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    session: AsyncSession | None = None,
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
        api_url: Optional explicit indexer API URL.
        api_key: Optional explicit indexer API key.
        session: Optional DB session to resolve active provider.

    Returns:
        List of search result items.
    """
    resolved_url, resolved_key = await resolve_indexer(
        session=session, api_url=api_url, api_key=api_key
    )
    if not resolved_key:
        logger.warning("Indexer API key missing.")
        return []

    url = resolved_url

    # Dynamically determine the best 't' parameter based on what fields we have
    search_type = "search"
    if season is not None or ep is not None or tvdb_id is not None:
        search_type = "tvsearch"
    elif imdb_id is not None and category == 2000:
        search_type = "movie"

    params: dict[str, Any] = {
        "apikey": resolved_key,
        "t": search_type,
        "o": "json",
    }

    if query:
        params["q"] = query
    if category:
        params["cat"] = category
    if season is not None:
        params["season"] = season
    if ep:
        params["ep"] = ep
    if imdb_id:
        params["imdbid"] = (
            imdb_id.replace("tt", "") if isinstance(imdb_id, str) else imdb_id
        )
    if tmdb_id:
        params["tmdbid"] = tmdb_id
    if tvdb_id:
        params["tvdbid"] = tvdb_id

    return await _execute_search(url, params)


async def _execute_search(url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Execute the HTTP request to the indexer API and normalize results."""
    await _limiter.wait()
    req_preview = httpx.Request("GET", url, params=params)
    logger.info("    🔍 Indexer Search URL: %s", req_preview.url)
    async with httpx.AsyncClient(
        headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=20.0
    ) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "json" not in content_type.lower():
                logger.error(
                    "Indexer returned non-JSON response (Content-Type: %s). Domain may be parked or blocking requests. Snippet: %s",
                    content_type,
                    response.text[:200],
                )
                return []

            data = response.json()

            # Check for API error
            if "@attributes" in data and "code" in data["@attributes"]:
                code = data["@attributes"].get("code")
                desc = data["@attributes"].get("description", "Unknown error")
                logger.error("Indexer returned API error %s: %s", code, desc)
                raise IndexerError(f"API Error {code}: {desc}")

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
                    enc_attrs = (
                        enc.get("@attributes", {}) if isinstance(enc, dict) else {}
                    )
                    flat["size"] = enc_attrs.get("length", 0)

                # Use direct link for download URL
                if not flat.get("link"):
                    enc = item.get("enclosure", {})
                    enc_attrs = (
                        enc.get("@attributes", {}) if isinstance(enc, dict) else {}
                    )
                    flat["link"] = enc_attrs.get("url", "")

                flat["size"] = int(flat["size"]) if flat["size"] else 0
                normalized.append(flat)

            logger.info("    📦 Indexer returned %d raw result(s).", len(normalized))
            return normalized
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            if status_code in (429, 503):
                logger.warning(
                    "Indexer returned HTTP %d (rate limit / unavailable): %s",
                    status_code,
                    e,
                )
                raise IndexerError(f"HTTP {status_code}: {e}") from e
            logger.error("Indexer search failed with HTTP %d: %s", status_code, e)
            return []
        except httpx.HTTPError as e:
            logger.error("Indexer search failed: %s", e)
            return []
        except IndexerError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to parse indexer results: %s", e)
            return []


async def fetch_nzb_bytes(
    guid_or_url: str,
    api_url: str | None = None,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> tuple[bytes, str]:
    """Fetch raw NZB XML content bytes locally from Treasure Maps / Newznab using GUID or full download URL.

    Args:
        guid_or_url: GUID or direct download URL.
        api_url: Optional explicit indexer API URL.
        api_key: Optional explicit indexer API key.
        session: Optional DB session to resolve active provider.

    Returns:
        (content_bytes, filename)

    Raises:
        IndexerError: If fetching fails or API key missing or response is invalid.
    """
    resolved_url, resolved_key = await resolve_indexer(
        session=session, api_url=api_url, api_key=api_key
    )
    if not resolved_key:
        raise IndexerError("Indexer API key missing.")

    if guid_or_url.startswith("http"):
        url = guid_or_url
        params = None
        # If url doesn't have apikey, append it if it's treasure-maps
        if (
            ("treasuremaps.net" in url or "treasure-maps.com" in url) or "api" in url
        ) and "apikey=" not in url:
            url += (
                f"&apikey={resolved_key}" if "?" in url else f"?apikey={resolved_key}"
            )
    else:
        url = resolved_url
        params = {
            "t": "get",
            "id": guid_or_url,
            "apikey": resolved_key,
        }

    await _limiter.wait()
    async with httpx.AsyncClient(
        headers={"User-Agent": DEFAULT_USER_AGENT},
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()

            content = response.content
            if not content or b"<nzb" not in content.lower():
                logger.error(
                    "Indexer returned response that does not appear to be an NZB XML document"
                )
                raise IndexerError(
                    "Received response from indexer is not a valid NZB XML document"
                )

            # Extract filename from Content-Disposition header if available
            filename = f"{guid_or_url if not guid_or_url.startswith('http') else 'download'}.nzb"
            cd = response.headers.get("content-disposition", "")
            if "filename=" in cd:
                fname = cd.split("filename=")[-1].strip("\";' ")
                if fname:
                    filename = fname

            return content, filename
        except httpx.HTTPError as e:
            logger.error("Failed to fetch NZB bytes from indexer: %s", e)
            raise IndexerError(f"HTTP error fetching NZB: {e}") from e


async def get_download_url(
    guid: str,
    api_url: str | None = None,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> str:
    """Generate the download URL for a specific NZB given its GUID/ID."""
    resolved_url, resolved_key = await resolve_indexer(
        session=session, api_url=api_url, api_key=api_key
    )
    params = {"t": "get", "id": guid, "apikey": resolved_key}
    request = httpx.Request("GET", resolved_url, params=params)
    return str(request.url)
