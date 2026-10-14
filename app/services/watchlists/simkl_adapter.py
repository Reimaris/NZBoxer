"""Simkl list-reading adapter: normalizes Simkl lists into ``WatchlistEntry`` objects."""

from __future__ import annotations

import re
from typing import Any

from app.db.database import async_session_factory
from app.db.models import Provider
from app.services.watchlist_providers import ListStatus, WatchlistEntry
from app.services.watchlists import simkl

_KIND_BY_KEY = {"movie": "movie", "show": "show", "anime": "anime"}


def _simkl_list_status(raw: Any) -> ListStatus:
    """Map a Simkl list status to ``ListStatus`` (``hold`` counts as ``watching``)."""
    status = str(raw or "plantowatch").lower()
    if status == "hold":
        return "watching"
    if status in ("plantowatch", "watching", "completed", "dropped"):
        return status  # type: ignore[return-value]
    return "plantowatch"


def _simkl_watched_episodes(item: dict[str, Any]) -> int:
    """Watched-episode count from the same payload keys auto-advance reads."""
    for key in ("watched_episodes_count", "watched_episodes"):
        val = item.get(key)
        if isinstance(val, list):
            val = len(val)
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            return val
    seasons = item.get("seasons")
    if isinstance(seasons, list):
        return sum(
            len(s["episodes"])
            for s in seasons
            if isinstance(s, dict) and isinstance(s.get("episodes"), list)
        )
    return 0


def _simkl_watched_season(item: dict[str, Any]) -> int | None:
    """Furthest season with watched episodes, from ``seasons[]`` and ``last_watched``."""
    numbers: list[int] = []
    for season in item.get("seasons") or []:
        if isinstance(season, dict) and season.get("episodes"):
            try:
                numbers.append(int(season.get("number")))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
    last = item.get("last_watched")
    if isinstance(last, str) and (m := re.search(r"(?i)S(\d+)\s*E(\d+)", last)):
        numbers.append(int(m.group(1)))
    positive = [n for n in numbers if n > 0]
    return max(positive) if positive else None


class SimklWatchlistAdapter:
    """Read-only Simkl list adapter."""

    async def fetch_entries(self, provider: Provider) -> list[WatchlistEntry]:
        async with async_session_factory() as session:
            target = (
                await session.get(Provider, provider.id) if provider.id else None
            ) or provider
            client_id, access_token = await simkl.ensure_valid_simkl_token(
                session, target
            )
            entries: list[WatchlistEntry] = []
            for category in ("movies", "shows", "anime"):
                (
                    raw,
                    client_id,
                    access_token,
                ) = await simkl._fetch_simkl_category_with_retry(
                    session, target, category, client_id, access_token
                )
                for item in raw:
                    key = next((k for k in _KIND_BY_KEY if item.get(k)), None)
                    media = item.get(key) if key else None
                    ids = (media or {}).get("ids") or {}
                    if not isinstance(media, dict) or ids.get("simkl") is None:
                        continue
                    entries.append(
                        WatchlistEntry(
                            remote_id=str(ids["simkl"]),
                            media_kind=_KIND_BY_KEY[key or "show"],  # type: ignore[arg-type]
                            title=str(media.get("title") or ""),
                            year=media.get("year"),
                            list_status=_simkl_list_status(item.get("status")),
                            watched_episodes=_simkl_watched_episodes(item),
                            watched_season=_simkl_watched_season(item),
                            tmdb_id=_int_or_none(ids.get("tmdb")),
                            imdb_id=ids.get("imdb"),
                            tvdb_id=_int_or_none(ids.get("tvdb")),
                            anilist_id=_int_or_none(ids.get("anilist")),
                            mal_id=_int_or_none(ids.get("mal")),
                        )
                    )
        return entries


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


ADAPTER = SimklWatchlistAdapter()
