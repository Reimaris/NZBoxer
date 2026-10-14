"""Watchlist provider adapter contract and implementations (m56, ADR-130).

Adapters normalize provider-specific lists into :class:`WatchlistEntry` objects.
They are read-only: NZBoxer never writes to any watchlist.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    tmdb_id: int | None = None
    imdb_id: str | None = None
    tvdb_id: int | None = None
    anilist_id: int | None = None
    mal_id: int | None = None


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
                )
            )
        return entries


class PlexWatchlistAdapter:
    """Plex Cloud (Discover) watchlist adapter; every entry is ``plantowatch``."""

    async def fetch_entries(self, provider: Provider) -> list[WatchlistEntry]:
        from app.services import plex

        client_identifier = str(provider.config.get("plex_client_identifier") or "")
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


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


_ADAPTERS: dict[str, WatchlistAdapter] = {
    "anilist": AniListWatchlistAdapter(),
    "plex": PlexWatchlistAdapter(),
}


def get_adapter(provider_type: str) -> WatchlistAdapter:
    """Return the adapter for *provider_type*; raises ``KeyError`` for unknown types."""
    return _ADAPTERS[provider_type.lower()]
