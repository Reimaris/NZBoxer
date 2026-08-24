import logging
from typing import Any

logger = logging.getLogger(__name__)

HARDCOVER_URL = "https://api.hardcover.app/v1/graphql"

async def search_books(query: str) -> list[dict[str, Any]]:
    """Stub for Hardcover book search integration."""
    logger.info(f"Searching Hardcover for: {query}")
    return []
