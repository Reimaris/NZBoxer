import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)
ANILIST_URL = "https://graphql.anilist.co"


async def _post_with_retry(
    client: httpx.AsyncClient, json_data: dict, max_retries: int = 3
) -> httpx.Response:
    for attempt in range(max_retries):
        resp = await client.post(ANILIST_URL, json=json_data)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 10))
            if attempt < max_retries - 1:
                logger.warning(
                    f"AniList rate limited (429). Retrying after {retry_after} seconds..."
                )
                await asyncio.sleep(retry_after)
                continue
        resp.raise_for_status()

        # Pre-emptive sleep if remaining requests are very low
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining and int(remaining) < 3:
            logger.info(
                "AniList rate limit running low, backing off pre-emptively for 3 seconds..."
            )
            await asyncio.sleep(3)

        return resp
    return resp  # Should never reach here due to raise_for_status inside loop


async def search_manga(query: str) -> list[dict[str, Any]]:
    """Searches AniList for Manga by title."""
    query_str = """
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
    """
    variables = {"search": query}

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await _post_with_retry(
                client, {"query": query_str, "variables": variables}
            )
            data = resp.json()
            return data.get("data", {}).get("Page", {}).get("media", [])
        except Exception as e:
            logger.error(f"AniList search failed: {e}")
            return []


async def search_anime_id_by_title(
    title: str, year: int | None = None
) -> int | None:
    """
    Searches AniList for an Anime by title (and optionally matches year).
    Returns the matching AniList ID or None.
    """
    query_str = """
    query ($search: String) {
      Page(page: 1, perPage: 10) {
        media(search: $search, type: ANIME) {
          id
          title { romaji english native }
          startDate { year }
          format
        }
      }
    }
    """
    variables = {"search": title}

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await _post_with_retry(
                client, {"query": query_str, "variables": variables}
            )
            data = resp.json()
            media_list = data.get("data", {}).get("Page", {}).get("media", [])
            if not media_list:
                return None
            if year:
                for item in media_list:
                    item_year = item.get("startDate", {}).get("year")
                    if item_year and item_year == year:
                        return item.get("id")
            return media_list[0].get("id")
        except Exception as e:
            logger.error(
                f"AniList search_anime_id_by_title failed for '{title}': {e}"
            )
            return None



async def get_manga_details(anilist_id: int) -> dict[str, Any] | None:
    """Fetches details for a specific AniList Manga ID."""
    query_str = """
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
    """
    variables = {"id": anilist_id}

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await _post_with_retry(
                client, {"query": query_str, "variables": variables}
            )
            data = resp.json()
            return data.get("data", {}).get("Media")
        except Exception as e:
            logger.error(f"AniList get details failed: {e}")
            return None


async def get_anime_aliases(anilist_id: int) -> list[str]:
    """Fetches title aliases for a specific AniList Anime ID."""
    query_str = """
    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        title { romaji english native }
        synonyms
      }
    }
    """
    variables = {"id": anilist_id}

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await _post_with_retry(
                client, {"query": query_str, "variables": variables}
            )
            data = resp.json().get("data", {}).get("Media") or {}

            titles = data.get("title") or {}
            aliases = set()
            for k in ("romaji", "english", "native"):
                if titles.get(k):
                    aliases.add(titles[k])

            for syn in data.get("synonyms") or []:
                if syn:
                    aliases.add(syn)

            return list(aliases)
        except Exception as e:
            logger.error(f"AniList get_anime_aliases failed: {e}")
            return []


async def get_anime_season_details(anilist_id: int) -> dict[str, Any] | None:
    """Fetches episode counts and airing info for a specific AniList Anime ID."""
    query_str = """
    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        id
        title { romaji english native }
        episodes
        status
        startDate { year month day }
        endDate { year month day }
        synonyms
        idMal
      }
    }
    """
    variables = {"id": anilist_id}

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await _post_with_retry(
                client, {"query": query_str, "variables": variables}
            )
            data = resp.json()
            return data.get("data", {}).get("Media")
        except Exception as e:
            logger.error(f"AniList get_anime_season_details failed: {e}")
            return None


async def get_anime_sequels(anilist_id: int) -> list[dict[str, Any]]:
    """Traverses SEQUEL relations to fetch all direct sequels recursively or just a flat list?
    Usually, we fetch the immediate SEQUEL, then fetch its SEQUEL, etc.
    To avoid many API calls, we can fetch recursively in GraphQL or just fetch immediate.
    We will just fetch the immediate sequels. We can recursively fetch in the caller or here.
    Let's fetch recursively here."""
    sequels = []
    current_id: int | None = anilist_id

    query_str = """

    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        relations {
          edges {
            relationType
            node {
              id
              type
              format
              episodes
              title { romaji english native }
              status
              startDate { year month day }
              idMal
            }
          }
        }
      }
    }
    """

    async with httpx.AsyncClient(timeout=10.0) as client:
        visited = {current_id}
        while current_id:
            try:
                variables = {"id": current_id}
                resp = await _post_with_retry(
                    client, {"query": query_str, "variables": variables}
                )
                data = resp.json()
                media = data.get("data", {}).get("Media")
                if not media:
                    break

                edges = media.get("relations", {}).get("edges", [])
                next_id = None
                for edge in edges:
                    if edge.get("relationType") == "SEQUEL":
                        node = edge.get("node")
                        # We only want TV/OVA/ONA formats, maybe MOVIE? Usually TV sequels are next seasons.
                        if (
                            node
                            and node.get("type") == "ANIME"
                            and node.get("format") in ("TV", "TV_SHORT", "OVA", "ONA")
                        ):
                            node_id = node.get("id")
                            if node_id not in visited:
                                sequels.append(node)
                                visited.add(node_id)
                                next_id = node_id
                                break  # just follow the first direct sequel

                current_id = next_id

            except Exception as e:
                logger.error(f"AniList get_anime_sequels failed: {e}")
                break

    return sequels


async def get_anime_root_and_hierarchy(anilist_id: int) -> dict[str, Any] | None:
    """
    Traverses PREQUEL relations up to the root franchise anime, then traverses
    SEQUEL relations down to build the complete ordered season hierarchy.
    Excludes Movies, OVAs, Specials, and Music formats.
    """
    query_str = """
    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        id
        type
        format
        episodes
        title { romaji english native }
        status
        startDate { year month day }
        idMal
        relations {
          edges {
            relationType
            node {
              id
              type
              format
              episodes
              title { romaji english native }
              status
              startDate { year month day }
              idMal
            }
          }
        }
      }
    }
    """

    ALLOWED_FORMATS = {"TV", "TV_SHORT", "ONA"}

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Step 1: Traverse up to find root
        current_id: int | None = anilist_id
        root_node = None

        visited_up = {current_id}

        while current_id:
            try:
                variables = {"id": current_id}
                resp = await _post_with_retry(
                    client, {"query": query_str, "variables": variables}
                )
                data = resp.json()
                media = data.get("data", {}).get("Media")
                if not media:
                    break

                root_node = media

                edges = media.get("relations", {}).get("edges", [])
                next_id = None
                for edge in edges:
                    if edge.get("relationType") == "PREQUEL":
                        node = edge.get("node")
                        if (
                            node
                            and node.get("type") == "ANIME"
                            and node.get("format") in ALLOWED_FORMATS
                        ):
                            node_id = node.get("id")
                            if node_id not in visited_up:
                                next_id = node_id
                                visited_up.add(node_id)
                                break

                current_id = next_id

            except Exception as e:
                logger.error(f"AniList prequel traversal failed: {e}")
                break

        if not root_node:
            return None

        # Step 2: Traverse down to build hierarchy
        hierarchy = [root_node]
        current_id = root_node.get("id")
        visited_down = {current_id}

        current_media = root_node

        while current_media:
            edges = current_media.get("relations", {}).get("edges", [])
            next_id = None
            for edge in edges:
                if edge.get("relationType") == "SEQUEL":
                    node = edge.get("node")
                    if (
                        node
                        and node.get("type") == "ANIME"
                        and node.get("format") in ALLOWED_FORMATS
                    ):
                        node_id = node.get("id")
                        if node_id not in visited_down:
                            next_id = node_id
                            visited_down.add(node_id)
                            break

            if not next_id:
                break

            try:
                variables = {"id": next_id}
                resp = await _post_with_retry(
                    client, {"query": query_str, "variables": variables}
                )
                data = resp.json()
                current_media = data.get("data", {}).get("Media")
                if current_media:
                    hierarchy.append(current_media)
                else:
                    break
            except Exception as e:
                logger.error(f"AniList sequel traversal failed: {e}")
                break

        return {"root": root_node, "hierarchy": hierarchy}
