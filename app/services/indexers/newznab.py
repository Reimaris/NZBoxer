"""
Newznab API Client
==================================
Searches the configured Usenet indexer for NZB releases.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from app.config import DEFAULT_USER_AGENT
from app.core.rate_limiter import RateLimiter
from app.services.outage_classifier import (
    OutageVerdict,
    classify_exception,
    classify_newznab_error,
    classify_non_json_body,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
_limiter = RateLimiter(0.6)

DEFAULT_INDEXER_COOLDOWN_SECONDS: int = 3600
_indexer_cooldowns: dict[str, datetime] = {}
_indexer_names: dict[str, str] = {}


class IndexerError(Exception):
    pass


class IndexerOutageError(IndexerError):
    """Transient indexer outage (5xx, timeout, TLS, HTML intercept) — ADR-158."""

    def __init__(
        self,
        message: str,
        indexer_url: str = "",
        verdict: OutageVerdict | None = None,
    ) -> None:
        super().__init__(message)
        self.indexer_url = indexer_url
        self.verdict = verdict


class IndexerAuthError(IndexerError):
    """Credential failure (HTTP 401/403, Newznab 100-102) — never retried."""

    def __init__(
        self,
        message: str,
        indexer_url: str = "",
        verdict: OutageVerdict | None = None,
    ) -> None:
        super().__init__(message)
        self.indexer_url = indexer_url
        self.verdict = verdict


def _verdict_error(
    verdict: OutageVerdict, message: str, indexer_url: str
) -> IndexerOutageError | IndexerAuthError:
    cls = IndexerAuthError if verdict.kind == "auth" else IndexerOutageError
    return cls(message, indexer_url=indexer_url, verdict=verdict)


class IndexerRateLimitError(IndexerError):
    def __init__(
        self,
        message: str,
        indexer_url: str = "",
        retry_after_seconds: int = 3600,
        status_code: int = 429,
    ) -> None:
        super().__init__(message)
        self.indexer_url = indexer_url
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code


def _normalize_indexer_key(indexer_url: str) -> str:
    """Normalize indexer URL to host or cleaned URL string for cooldown tracking."""
    if not indexer_url:
        return "default"
    parsed = urlparse(indexer_url)
    if parsed.netloc:
        return parsed.netloc.lower()
    return indexer_url.strip().lower().rstrip("/")


def set_indexer_cooldown(indexer_url: str, seconds: int) -> None:
    """Set in-memory cooldown expiration timestamp for the given indexer."""
    key = _normalize_indexer_key(indexer_url)
    now = datetime.now(timezone.utc)
    from datetime import timedelta

    _indexer_cooldowns[key] = now + timedelta(seconds=max(0, seconds))
    logger.warning(
        "⏳ Indexer '%s' placed in cooldown for %ds (until %s)",
        key,
        seconds,
        _indexer_cooldowns[key].isoformat(),
    )


def get_indexer_cooldown_remaining(indexer_url: str) -> float:
    """Return remaining cooldown seconds for indexer, or 0.0 if not cooling down."""
    key = _normalize_indexer_key(indexer_url)
    expiry = _indexer_cooldowns.get(key)
    if not expiry:
        return 0.0
    now = datetime.now(timezone.utc)
    remaining = (expiry - now).total_seconds()
    if remaining <= 0:
        _indexer_cooldowns.pop(key, None)
        return 0.0
    return remaining


def is_indexer_in_cooldown(indexer_url: str) -> bool:
    """Check if indexer is currently cooling down from a rate limit."""
    return get_indexer_cooldown_remaining(indexer_url) > 0.0


def clear_all_indexer_cooldowns() -> None:
    """Clear all active indexer cooldown entries."""
    _indexer_cooldowns.clear()


def register_indexer_names(indexers: Any) -> None:
    """Remember provider display names per indexer host for rate-limit messages."""
    for p in indexers:
        if getattr(p, "api_url", None) and getattr(p, "name", None):
            _indexer_names[_normalize_indexer_key(p.api_url)] = p.name


def indexer_display_name(indexer_url: str) -> str:
    """Provider display name for an indexer URL, else its host key."""
    key = _normalize_indexer_key(indexer_url)
    return _indexer_names.get(key, key)


def indexer_key_for_name(name: str) -> str:
    """Inverse of ``indexer_display_name``: cooldown key for a display name."""
    for key, display in _indexer_names.items():
        if display == name:
            return key
    return name


def get_all_indexer_cooldowns() -> dict[str, float]:
    """Display name (provider name, else host key) -> remaining seconds (> 0 only)."""
    result: dict[str, float] = {}
    for key in list(_indexer_cooldowns.keys()):
        rem = get_indexer_cooldown_remaining(key)
        if rem > 0:
            result[_indexer_names.get(key, key)] = rem
    return result


def get_all_indexer_cooldowns_remaining() -> float:
    """Return the maximum remaining cooldown seconds across all tracked indexers."""
    keys = list(_indexer_cooldowns.keys())
    max_rem = 0.0
    for k in keys:
        rem = get_indexer_cooldown_remaining(k)
        if rem > max_rem:
            max_rem = rem
    return max_rem


async def resolve_indexer(
    session: AsyncSession | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
) -> tuple[str, str]:
    """Resolve active indexer API URL and API key."""
    if api_url and api_key:
        return api_url, api_key

    from app.services.provider_service import get_active_indexers

    # provider-specific code (allowlisted)
    if session is not None:
        indexers = await get_active_indexers(session)
        register_indexer_names(indexers)
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
                register_indexer_names(indexers)
                if indexers and indexers[0].api_key:
                    return (
                        indexers[0].api_url
                        or "https://treasure-maps.com/api",  # provider-specific code (allowlisted)
                        indexers[0].api_key or "",
                    )
        except Exception:
            pass

    # provider-specific code (allowlisted)
    return (
        api_url or "https://treasure-maps.com/api",
        api_key or "",
    )


async def search_movie(
    imdb_id: str | None = None,
    tmdb_id: int | None = None,
    title: str | None = None,
    category: int | str | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """Search for a movie on the Newznab indexer.

    Args:
        imdb_id: The IMDb ID, e.g., 'tt1234567' or '1234567'.
        tmdb_id: The TMDB ID.
        title: Movie title for fallback search.
        category: optional Newznab category ID (e.g. 2000).
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
    category: int | str | None = None,
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
        category: optional Newznab category ID (e.g. 5000 or 5070 for Anime).
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
        category: optional Newznab category ID.
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
    elif (
        imdb_id is not None
        and category is not None
        and str(category).split(",")[0] == "2000"
    ):
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


def _parse_retry_after(headers: Any) -> int:
    """Parse Retry-After header (seconds or delta), falling back to DEFAULT_INDEXER_COOLDOWN_SECONDS."""
    if not headers:
        return DEFAULT_INDEXER_COOLDOWN_SECONDS
    raw = None
    if hasattr(headers, "get"):
        raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw and isinstance(headers, dict):
        for k, v in headers.items():
            if str(k).lower() == "retry-after":
                raw = v
                break
    if not raw:
        return DEFAULT_INDEXER_COOLDOWN_SECONDS
    try:
        val = int(str(raw).strip())
        return max(1, val)
    except ValueError:
        # Check HTTP-date format
        try:
            from email.utils import parsedate_to_datetime

            target_dt = parsedate_to_datetime(raw)
            if target_dt:
                now = datetime.now(timezone.utc)
                diff = int((target_dt - now).total_seconds())
                return max(1, diff)
        except Exception:
            pass
    return DEFAULT_INDEXER_COOLDOWN_SECONDS


async def _execute_search(url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Execute the HTTP request to the indexer API and normalize results."""
    if is_indexer_in_cooldown(url):
        rem = int(get_indexer_cooldown_remaining(url))
        logger.warning(
            "⏳ Indexer '%s' is in active cooldown (%ds remaining). Aborting search.",
            url,
            rem,
        )
        raise IndexerRateLimitError(
            f"Indexer in active cooldown ({rem}s remaining)",
            indexer_url=url,
            retry_after_seconds=rem,
            status_code=429,
        )

    await _limiter.wait()
    req_preview = httpx.Request("GET", url, params=params)
    logger.info("🔍 Indexer Search URL: %s", req_preview.url)
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
                verdict = classify_non_json_body(content_type, response.text[:200])
                raise _verdict_error(
                    verdict, f"{verdict.label} (Content-Type: {content_type})", url
                )

            data = response.json()

            nz_verdict = classify_newznab_error(data)
            if nz_verdict is not None:
                logger.error("Indexer returned API error (%s)", nz_verdict.label)
                raise _verdict_error(nz_verdict, f"API Error: {nz_verdict.label}", url)

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
            # Newznab JSON puts size/guid/category inside:
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

                # Extract password and passwordurl attributes if provided by indexer
                raw_pwd = attr_map.get("password", flat.get("password", 0))
                try:
                    flat["password"] = int(raw_pwd or 0)
                except (ValueError, TypeError):
                    flat["password"] = 0
                flat["passwordurl"] = str(
                    attr_map.get("passwordurl", flat.get("passwordurl", "")) or ""
                )

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

            logger.info("📦 Indexer returned %d raw result(s).", len(normalized))
            return normalized
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            if status_code == 429:
                retry_after = _parse_retry_after(e.response.headers)
                set_indexer_cooldown(url, retry_after)
                logger.warning(
                    "Indexer returned HTTP 429 (rate limit): %s. Cooldown set to %ds.",
                    e,
                    retry_after,
                )
                raise IndexerRateLimitError(
                    f"HTTP 429: {e}",
                    indexer_url=url,
                    retry_after_seconds=retry_after,
                    status_code=429,
                ) from e
            status_verdict = classify_exception(e)
            if status_verdict is not None:
                logger.warning(
                    "Indexer returned HTTP %d (%s): %s",
                    status_code,
                    status_verdict.kind,
                    e,
                )
                raise _verdict_error(
                    status_verdict, f"HTTP {status_code}: {e}", url
                ) from e
            logger.error("Indexer search failed with HTTP %d: %s", status_code, e)
            return []
        except httpx.HTTPError as e:
            logger.error("Indexer search failed: %s", e)
            transport_verdict = classify_exception(e)
            if transport_verdict is not None:
                raise _verdict_error(
                    transport_verdict, str(e) or transport_verdict.label, url
                ) from e
            return []
        except IndexerError:
            raise
        except json.JSONDecodeError as e:
            logger.error("Indexer returned undecodable JSON: %s", e)
            raise _verdict_error(
                OutageVerdict("outage", "JSONDecodeError"), f"JSONDecodeError: {e}", url
            ) from e
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to parse indexer results: %s", e)
            return []


async def fetch_nzb_bytes(
    guid_or_url: str,
    api_url: str | None = None,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> tuple[bytes, str]:
    """Fetch raw NZB XML content bytes locally from a Newznab indexer using GUID or full download URL.

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
        # provider-specific code (allowlisted)
        # If url doesn't have apikey, append it for the default preset host
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
                if "html" in response.headers.get("content-type", "").lower():
                    verdict = classify_non_json_body("html", "")
                    raise _verdict_error(
                        verdict, f"{verdict.label} (HTML instead of NZB)", url
                    )
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
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            if status_code == 429:
                retry_after = _parse_retry_after(e.response.headers)
                set_indexer_cooldown(url, retry_after)
                logger.warning(
                    "Indexer returned HTTP 429 fetching NZB: %s. Cooldown set to %ds.",
                    e,
                    retry_after,
                )
                raise IndexerRateLimitError(
                    f"HTTP 429: {e}",
                    indexer_url=url,
                    retry_after_seconds=retry_after,
                    status_code=429,
                ) from e
            logger.error(
                "Failed to fetch NZB bytes from indexer with HTTP %d: %s",
                status_code,
                e,
            )
            status_verdict = classify_exception(e)
            if status_verdict is not None:
                raise _verdict_error(
                    status_verdict, f"HTTP error fetching NZB: {e}", url
                ) from e
            raise IndexerError(f"HTTP error fetching NZB: {e}") from e
        except httpx.HTTPError as e:
            logger.error("Failed to fetch NZB bytes from indexer: %s", e)
            transport_verdict = classify_exception(e)
            if transport_verdict is not None:
                raise _verdict_error(
                    transport_verdict, f"HTTP error fetching NZB: {e}", url
                ) from e
            raise IndexerError(f"HTTP error fetching NZB: {e}") from e
