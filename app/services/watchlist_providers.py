"""Watchlist provider adapter contract and implementations (m56, ADR-130).

Adapters normalize provider-specific lists into :class:`WatchlistEntry` objects.
They are read-only: NZBoxer never writes to any watchlist.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from app.db.models import Provider

ListStatus = Literal["plantowatch", "watching", "completed", "dropped"]
MediaKind = Literal["movie", "show", "anime", "anime_movie"]


class WatchlistFetchError(Exception):
    """Raised when a provider list cannot be fetched (auth/HTTP failure)."""


@dataclass(frozen=True)
class WatchlistEntry:
    remote_id: str
    media_kind: MediaKind
    title: str
    year: int | None = None
    list_status: ListStatus = "plantowatch"
    watched_episodes: int = 0
    watched_season: int | None = None  # furthest season with watched episodes
    tmdb_id: int | None = None
    imdb_id: str | None = None
    tvdb_id: int | None = None
    anilist_id: int | None = None
    mal_id: int | None = None
    poster_url: str | None = None


class WatchlistAdapter(Protocol):
    async def fetch_entries(self, provider: Provider) -> list[WatchlistEntry]: ...


_ANILIST_STATUS_MAP: dict[str, ListStatus] = {
    "PLANNING": "plantowatch",
    "CURRENT": "watching",
    "REPEATING": "watching",
    "PAUSED": "watching",
    "COMPLETED": "completed",
    "DROPPED": "dropped",
}


class AniListWatchlistAdapter:
    """Public-username (optional token) AniList anime list adapter."""

    async def fetch_entries(self, provider: Provider) -> list[WatchlistEntry]:
        from app.services import anilist

        raw = await anilist.get_user_media_list(
            (provider.username or "").strip(), provider.access_token or None
        )
        entries: list[WatchlistEntry] = []
        for item in raw:
            media = item.get("media") or {}
            media_id = media.get("id")
            if not media_id:
                continue
            title_obj = media.get("title") or {}
            title = title_obj.get("english") or title_obj.get("romaji") or ""
            entries.append(
                WatchlistEntry(
                    remote_id=str(media_id),
                    media_kind="anime_movie"
                    if media.get("format") == "MOVIE"
                    else "anime",
                    title=title,
                    year=(media.get("startDate") or {}).get("year"),
                    list_status=_ANILIST_STATUS_MAP.get(
                        str(item.get("status")), "plantowatch"
                    ),
                    watched_episodes=max(0, int(item.get("progress") or 0)),
                    anilist_id=int(media_id),
                    mal_id=media.get("idMal"),
                    poster_url=(media.get("coverImage") or {}).get("large") or None,
                )
            )
        return entries


class PlexWatchlistAdapter:
    """Plex Cloud (Discover) watchlist adapter; every entry is ``plantowatch``.

    No progress by design: ``watched_episodes`` is always ``0``.
    """

    async def fetch_entries(self, provider: Provider) -> list[WatchlistEntry]:
        from app.services.watchlists import plex

        client_identifier = str(
            provider.watchlist_config.get("plex_client_identifier") or ""
        )
        raw = await plex.get_watchlist(provider.access_token or "", client_identifier)
        entries: list[WatchlistEntry] = []
        for item in raw:
            rating_key = item.get("ratingKey")
            kind = item.get("type")
            if not rating_key or kind not in ("movie", "show"):
                continue
            ids: dict[str, str] = {}
            for guid in item.get("Guid") or []:
                scheme, sep, value = str(guid.get("id", "")).partition("://")
                if sep and value:
                    ids[scheme] = value
            entries.append(
                WatchlistEntry(
                    remote_id=str(rating_key),
                    media_kind="movie" if kind == "movie" else "show",
                    title=str(item.get("title") or ""),
                    year=item.get("year"),
                    tmdb_id=_int_or_none(ids.get("tmdb")),
                    imdb_id=ids.get("imdb"),
                    tvdb_id=_int_or_none(ids.get("tvdb")),
                )
            )
        return entries


_MAL_STATUS_MAP: dict[str, ListStatus] = {
    "plan_to_watch": "plantowatch",
    "watching": "watching",
    "on_hold": "watching",
    "completed": "completed",
    "dropped": "dropped",
}


class MalWatchlistAdapter:
    """MyAnimeList anime list adapter (token auto-refresh)."""

    async def fetch_entries(self, provider: Provider) -> list[WatchlistEntry]:
        from app.db.database import async_session_factory
        from app.db.models import Provider as ProviderModel
        from app.services import anilist
        from app.services.watchlists import mal

        async with async_session_factory() as session:
            target = (
                await session.get(ProviderModel, provider.id) if provider.id else None
            ) or provider
            token = await mal.ensure_valid_mal_token(session, target)
            try:
                raw = await mal.get_animelist(token)
            except mal.MalUnauthorizedError:
                token = await mal.ensure_valid_mal_token(session, target, force=True)
                try:
                    raw = await mal.get_animelist(token)
                except mal.MalUnauthorizedError as exc:
                    raise WatchlistFetchError("MyAnimeList reconnect required") from exc

        nodes = [
            (item.get("node") or {}, item.get("list_status") or {}) for item in raw
        ]
        mal_ids = [int(n["id"]) for n, _ in nodes if n.get("id")]
        anilist_map = await anilist.get_anime_ids_by_mal_ids(mal_ids) if mal_ids else {}
        entries: list[WatchlistEntry] = []
        for node, status in nodes:
            if not node.get("id"):
                continue
            mal_id = int(node["id"])
            start = str(node.get("start_date") or "")[:4]
            entries.append(
                WatchlistEntry(
                    remote_id=str(mal_id),
                    media_kind="anime_movie"
                    if node.get("media_type") == "movie"
                    else "anime",
                    title=str(node.get("title") or ""),
                    year=int(start) if start.isdigit() else None,
                    list_status=_MAL_STATUS_MAP.get(
                        str(status.get("status")), "plantowatch"
                    ),
                    watched_episodes=max(
                        0, int(status.get("num_episodes_watched") or 0)
                    ),
                    anilist_id=anilist_map.get(mal_id),
                    mal_id=mal_id,
                    poster_url=(node.get("main_picture") or {}).get("large")
                    or (node.get("main_picture") or {}).get("medium")
                    or None,
                )
            )
        return entries


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


_ADAPTERS: dict[str, WatchlistAdapter] = {
    "anilist": AniListWatchlistAdapter(),
    "plex": PlexWatchlistAdapter(),
    "mal": MalWatchlistAdapter(),
}


def get_adapter(provider_type: str) -> WatchlistAdapter:
    """Return the adapter for *provider_type*; raises ``KeyError`` for unknown types.

    Other types ship an ``app.services.watchlists.<type>_adapter`` module exposing an
    ``ADAPTER`` instance, loaded on first use.
    """
    key = provider_type.lower()
    if key not in _ADAPTERS:
        try:
            module = import_module(f"app.services.watchlists.{key}_adapter")
        except ImportError as exc:
            raise KeyError(provider_type) from exc
        _ADAPTERS[key] = module.ADAPTER
    return _ADAPTERS[key]
