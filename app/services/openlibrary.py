import logging
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


async def search_books(query: str) -> list[dict[str, Any]]:
    """Search OpenLibrary for books."""
    url = f"https://openlibrary.org/search.json?q={quote(query)}&limit=10"
    async with httpx.AsyncClient(timeout=10.0) as client:
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
    url = (
        f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data.get(f"ISBN:{isbn}")
        except Exception as e:
            logger.error(f"OpenLibrary get book failed: {e}")
            return None
