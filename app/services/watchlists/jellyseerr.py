"""Jellyseerr/Overseerr client (m68, ADR-147): approved requests, admin watchlist, titles."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import DEFAULT_USER_AGENT
from app.core.ttl_cache import TTLCache

_PAGE_SIZE = 100
_TIMEOUT = 15.0
_STATUS_APPROVED = 2
_TITLE_TTL_SECONDS = 3600

_title_cache: TTLCache[tuple[str, int | None]] = TTLCache(
    ttl_seconds=_TITLE_TTL_SECONDS
)


class JellyseerrUnauthorizedError(Exception):
    """The server rejected the API key (HTTP 401/403)."""


def _client(api_key: str, timeout: float = _TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        headers={
            "X-Api-Key": api_key,
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        },
    )


def _api(server_url: str, path: str) -> str:
    return f"{server_url.rstrip('/')}/api/v1{path}"


async def _get_json(
    client: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    resp = await client.get(url, params=params)
    if resp.status_code in (401, 403):
        raise JellyseerrUnauthorizedError(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


async def get_approved_requests(server_url: str, api_key: str) -> list[dict[str, Any]]:
    """All approved requests (``status == 2``), paged ``take=100``."""
    out: list[dict[str, Any]] = []
    skip = 0
    async with _client(api_key) as client:
        while True:
            data = await _get_json(
                client,
                _api(server_url, "/request"),
                {
                    "filter": "approved",
                    "take": _PAGE_SIZE,
                    "skip": skip,
                    "sort": "added",
                },
            )
            page = [r for r in data.get("results") or [] if isinstance(r, dict)]
            out.extend(r for r in page if r.get("status") == _STATUS_APPROVED)
            if len(page) < _PAGE_SIZE:
                return out
            skip += _PAGE_SIZE


async def get_admin_watchlist(server_url: str, api_key: str) -> list[dict[str, Any]]:
    """The admin user's watchlist entries across all pages."""
    out: list[dict[str, Any]] = []
    page = 1
    async with _client(api_key) as client:
        while True:
            data = await _get_json(
                client, _api(server_url, "/user/watchlist"), {"page": page}
            )
            out.extend(r for r in data.get("results") or [] if isinstance(r, dict))
            pages = int((data.get("pageInfo") or {}).get("pages") or 1)
            if page >= pages:
                return out
            page += 1


async def get_title(
    server_url: str, api_key: str, media_type: str, tmdb_id: int
) -> tuple[str, int | None]:
    """Resolve ``(title, year)`` for a TMDB id (``media_type`` ``movie`` or ``tv``)."""
    is_movie = media_type == "movie"
    key = f"{server_url.rstrip('/')}|{'movie' if is_movie else 'tv'}|{tmdb_id}"
    cached = _title_cache.get(key)
    if cached is not None:
        return cached
    path = f"/movie/{tmdb_id}" if is_movie else f"/tv/{tmdb_id}"
    async with _client(api_key) as client:
        data = await _get_json(client, _api(server_url, path))
    title = str(data.get("title" if is_movie else "name") or "")
    date = str(data.get("releaseDate" if is_movie else "firstAirDate") or "")
    year = int(date[:4]) if date[:4].isdigit() else None
    _title_cache.set(key, (title, year))
    return title, year


async def mark_available(server_url: str, api_key: str, media_id: int) -> None:
    """Tell the server the media is available (non-4K)."""
    async with _client(api_key, timeout=10.0) as client:
        resp = await client.post(
            _api(server_url, f"/media/{media_id}/available"), json={"is4k": False}
        )
        resp.raise_for_status()
