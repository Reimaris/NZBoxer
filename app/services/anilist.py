import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)
ANILIST_URL = "https://graphql.anilist.co"

async def search_manga(query: str) -> list[dict[str, Any]]:
    """Searches AniList for Manga by title."""
    query_str = '''
    query ($search: String) {
      Page(page: 1, perPage: 10) {
        media(search: $search, type: MANGA) {
          id
          title { romaji english native }
          coverImage { large }
          description
          status
          volumes
          startDate { year }
        }
      }
    }
    '''
    variables = {"search": query}
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(ANILIST_URL, json={"query": query_str, "variables": variables})
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {}).get("Page", {}).get("media", [])
        except Exception as e:
            logger.error(f"AniList search failed: {e}")
            return []

async def get_manga_details(anilist_id: int) -> dict[str, Any] | None:
    """Fetches details for a specific AniList Manga ID."""
    query_str = '''
    query ($id: Int) {
      Media(id: $id, type: MANGA) {
        id
        title { romaji english native }
        coverImage { large }
        description
        status
        volumes
        startDate { year }
        synonyms
        staff { edges { role node { name { full } } } }
      }
    }
    '''
    variables = {"id": anilist_id}
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(ANILIST_URL, json={"query": query_str, "variables": variables})
            resp.raise_for_status()
            data = resp.json()
            return data.get("data", {}).get("Media")
        except Exception as e:
            logger.error(f"AniList get details failed: {e}")
            return None
