"""
TMDB API Client
===============
Fetches release dates for movies (specifically digital release - type 4)
and metadata for TV shows (episode counts, first air dates).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

from app.config import DEFAULT_USER_AGENT, settings
from app.core.rate_limiter import RateLimiter

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
_limiter = RateLimiter(0.1)

TMDB_BASE_URL = "https://api.themoviedb.org/3"


class TMDBError(Exception):
    pass


async def resolve_api_key(
    session: AsyncSession | None = None, explicit_key: str | None = None
) -> str:
    """Resolve active TMDB API key from explicit argument, DB provider, or config settings."""
    if explicit_key:
        return explicit_key

    from app.services.provider_service import get_metadata_provider

    if session is not None:
        provider = await get_metadata_provider(session, "tmdb")
        if provider and provider.api_key:
            return provider.api_key
    else:
        from app.db.database import async_session_factory

        try:
            async with async_session_factory() as db:
                provider = await get_metadata_provider(db, "tmdb")
                if provider and provider.api_key:
                    return provider.api_key
        except Exception:
            pass

    return settings.tmdb_api_key or ""


def _build_auth(key: str) -> tuple[dict[str, Any], dict[str, str]]:
    params: dict[str, Any] = {}
    headers: dict[str, str] = {"User-Agent": DEFAULT_USER_AGENT}
    if key.startswith("ey"):
        headers["Authorization"] = f"Bearer {key}"
    else:
        params["api_key"] = key
    return params, headers


async def get_digital_release_date(
    tmdb_id: int,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> datetime | None:
    """Fetch the earliest digital release date (type 4) for a movie.

    Args:
        tmdb_id: The TMDB movie ID.
        api_key: Optional explicit API key.
        session: Optional DB session to resolve active provider.

    Returns:
        A timezone-aware datetime if found, else None.
    """
    key = await resolve_api_key(session=session, explicit_key=api_key)
    if not key:
        logger.warning("TMDB API key missing.")
        return None

    url = f"{TMDB_BASE_URL}/movie/{tmdb_id}/release_dates"
    params, headers = _build_auth(key)

    await _limiter.wait()
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()

            best_date = None
            fallback_date = None

            for country_release in data.get("results", []):
                for release in country_release.get("release_dates", []):
                    rtype = release.get("type")
                    date_str = release.get("release_date")
                    if date_str:
                        # Format: '2023-10-18T00:00:00.000Z'
                        try:
                            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                            if rtype in (4, 5):
                                if best_date is None or dt < best_date:
                                    best_date = dt
                            else:
                                if fallback_date is None or dt < fallback_date:
                                    fallback_date = dt
                        except ValueError:
                            pass

            return best_date or fallback_date
        except httpx.HTTPError as e:
            logger.error("TMDB release_dates error for %d: %s", tmdb_id, e)
            return None


async def get_movie_details(
    tmdb_id: int,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> dict[str, Any] | None:
    """Fetch details for a movie (including alternative titles)."""
    key = await resolve_api_key(session=session, explicit_key=api_key)
    if not key:
        logger.warning("TMDB API key missing.")
        return None

    url = f"{TMDB_BASE_URL}/movie/{tmdb_id}"
    params, headers = _build_auth(key)
    params["append_to_response"] = "alternative_titles,translations"

    await _limiter.wait()
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            res_dict: dict[str, Any] = response.json()
            return res_dict
        except httpx.HTTPError as e:
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404:
                logger.warning("TMDB movie details not found (404) for %d", tmdb_id)
            else:
                logger.error("TMDB movie details error for %d: %s", tmdb_id, e)
            return None


async def get_show_details(
    tmdb_id: int,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> dict[str, Any] | None:
    """Fetch details for a TV show (number of seasons, alternative titles, etc.)."""
    key = await resolve_api_key(session=session, explicit_key=api_key)
    if not key:
        logger.warning("TMDB API key missing.")
        return None

    url = f"{TMDB_BASE_URL}/tv/{tmdb_id}"
    params, headers = _build_auth(key)
    params["append_to_response"] = "alternative_titles,translations"

    await _limiter.wait()
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            res_dict: dict[str, Any] = response.json()
            return res_dict
        except httpx.HTTPError as e:
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404:
                logger.warning("TMDB tv details not found (404) for %d", tmdb_id)
            else:
                logger.error("TMDB tv details error for %d: %s", tmdb_id, e)
            return None


async def get_season_details(
    tmdb_id: int,
    season_number: int,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> dict[str, Any] | None:
    """Fetch details for a specific season to get episodes and air dates."""
    key = await resolve_api_key(session=session, explicit_key=api_key)
    if not key:
        logger.warning("TMDB API key missing.")
        return None

    url = f"{TMDB_BASE_URL}/tv/{tmdb_id}/season/{season_number}"
    params, headers = _build_auth(key)

    await _limiter.wait()
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            res_dict: dict[str, Any] = response.json()
            return res_dict
        except httpx.HTTPError as e:
            logger.error(
                "TMDB season details error for %d S%d: %s", tmdb_id, season_number, e
            )
            return None


async def find_by_external_id(
    external_id: str,
    source: str = "imdb_id",
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> dict[str, Any] | None:
    """Find TMDB metadata by an external ID (e.g. imdb_id).

    Returns a dictionary with 'type' ('movie' or 'tv') and 'id' (the TMDB ID),
    or None if not found.
    """
    key = await resolve_api_key(session=session, explicit_key=api_key)
    if not key:
        logger.warning("TMDB API key missing.")
        return None

    url = f"{TMDB_BASE_URL}/find/{external_id}"
    params, headers = _build_auth(key)
    params["external_source"] = source

    await _limiter.wait()
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()

            if data.get("movie_results"):
                return {"type": "movie", "id": data["movie_results"][0]["id"]}
            elif data.get("tv_results"):
                return {"type": "tv", "id": data["tv_results"][0]["id"]}

            return None
        except httpx.HTTPError as e:
            logger.error("TMDB find error for %s (%s): %s", external_id, source, e)
            return None
