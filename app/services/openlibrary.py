import logging
from typing import Any
from urllib.parse import quote

import httpx

from app.config import DEFAULT_USER_AGENT
from app.core.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

_limiter = RateLimiter(1.0)


async def search_books(query: str) -> list[dict[str, Any]]:
    """Search OpenLibrary for books."""
    await _limiter.wait()
    url = f"https://openlibrary.org/search.json?q={quote(query)}&limit=10"
    async with httpx.AsyncClient(
        headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=10.0
    ) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data.get("docs", [])
        except Exception as e:
            logger.error(f"OpenLibrary search failed: {e}")
            return []


async def get_book_by_isbn(isbn: str) -> dict[str, Any] | None:
    """Fetch specific book details from OpenLibrary via ISBN."""
    await _limiter.wait()
    url = (
        f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
    )
    async with httpx.AsyncClient(
        headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=10.0
    ) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data.get(f"ISBN:{isbn}")
        except Exception as e:
            logger.error(f"OpenLibrary get book failed: {e}")
            return None
