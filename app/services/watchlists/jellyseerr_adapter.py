"""Jellyseerr/Overseerr request adapter (m68, ADR-147): approved requests → entries."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.core.log_redaction import redact_text
from app.db.models import Provider
from app.services.watchlist_providers import (
    MediaKind,
    WatchlistEntry,
    WatchlistFetchError,
)
from app.services.watchlists import jellyseerr

logger = logging.getLogger(__name__)

_TITLE_CONCURRENCY = 5


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _merge_requests(
    requests: list[dict[str, Any]],
) -> dict[str, tuple[MediaKind, int, int | None, frozenset[int] | None]]:
    """Merge requests per ``media.id`` → ``(kind, tmdb_id, tvdb_id, seasons)``.

    An empty/missing season list means all seasons, so ``None`` dominates.
    """
    merged: dict[str, tuple[MediaKind, int, int | None, frozenset[int] | None]] = {}
    for req in requests:
        if req.get("status") != 2:
            continue
        media = req.get("media") or {}
        tmdb_id = _int_or_none(media.get("tmdbId"))
        if media.get("id") is None or not tmdb_id:
            continue
        kind: MediaKind = "movie" if req.get("type") == "movie" else "show"
        seasons: frozenset[int] | None = None
        if kind == "show":
            nums = {
                n
                for s in req.get("seasons") or []
                if isinstance(s, dict)
                and (n := _int_or_none(s.get("seasonNumber"))) is not None
            }
            seasons = frozenset(nums) or None
        key = str(media["id"])
        if key in merged:
            _, _, _, prev = merged[key]
            seasons = None if prev is None or seasons is None else prev | seasons
        merged[key] = (kind, tmdb_id, _int_or_none(media.get("tvdbId")), seasons)
    return merged


class JellyseerrWatchlistAdapter:
    """Approved Jellyseerr/Overseerr requests (and optional admin watchlist)."""

    async def fetch_entries(self, provider: Provider) -> list[WatchlistEntry]:
        cfg = provider.watchlist_config
        url = str(cfg.get("server_url") or "").strip()
        key = str(cfg.get("api_key") or "").strip()
        try:
            requests = await jellyseerr.get_approved_requests(url, key)
            watchlist = (
                await jellyseerr.get_admin_watchlist(url, key)
                if cfg.get("sync_admin_watchlist")
                else []
            )
        except jellyseerr.JellyseerrUnauthorizedError as exc:
            raise WatchlistFetchError("Jellyseerr/Overseerr API key rejected") from exc
        except httpx.HTTPError as exc:
            raise WatchlistFetchError(redact_text(str(exc))) from exc

        sem = asyncio.Semaphore(_TITLE_CONCURRENCY)

        async def resolve(kind: MediaKind, tmdb_id: int) -> tuple[str, int | None]:
            async with sem:
                try:
                    return await jellyseerr.get_title(
                        url, key, "movie" if kind == "movie" else "tv", tmdb_id
                    )
                except (httpx.HTTPError, jellyseerr.JellyseerrUnauthorizedError):
                    logger.debug("Title lookup failed for TMDB %d", tmdb_id)
                    return f"TMDB {tmdb_id}", None

        merged = _merge_requests(requests)
        titles = await asyncio.gather(
            *(resolve(kind, tmdb) for kind, tmdb, _, _ in merged.values())
        )
        entries = [
            WatchlistEntry(
                remote_id=remote_id,
                media_kind=kind,
                title=title,
                year=year,
                tmdb_id=tmdb_id,
                tvdb_id=tvdb_id,
                requested_seasons=seasons,
            )
            for (remote_id, (kind, tmdb_id, tvdb_id, seasons)), (title, year) in zip(
                merged.items(), titles, strict=True
            )
        ]
        requested_tmdb = {(e.media_kind, e.tmdb_id) for e in entries}
        for item in watchlist:
            tmdb_id = _int_or_none(item.get("tmdbId"))
            wl_kind: MediaKind = "movie" if item.get("mediaType") == "movie" else "show"
            if not tmdb_id or (wl_kind, tmdb_id) in requested_tmdb:
                continue
            entries.append(
                WatchlistEntry(
                    remote_id=f"wl-{tmdb_id}",
                    media_kind=wl_kind,
                    title=str(item.get("title") or f"TMDB {tmdb_id}"),
                    tmdb_id=tmdb_id,
                )
            )
        return entries


ADAPTER = JellyseerrWatchlistAdapter()
