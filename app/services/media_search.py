"""Ad-hoc media search orchestrator: Tier 1 hits and Tier 2 hierarchy (m52).

Routes smart-box input to TMDB (movies, series) and AniList (anime), caches
successful lookups for 600 s and lists seasons/episodes lazily.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, TypedDict

from app.core.ttl_cache import TTLCache
from app.services import anilist, tmdb

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

AD_HOC_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"any", "movie", "series", "anime", "anime-movie"}
)
DEFAULT_AD_HOC_CATEGORY: Final[str] = "any"

MAX_HITS_PER_BRANCH: Final[int] = 10
_TMDB_POSTER_BASE: Final[str] = "https://image.tmdb.org/t/p/w185"

Kind = Literal["movie", "series", "anime", "anime-movie"]
Provider = Literal["tmdb", "anilist"]


class MediaHit(TypedDict):
    provider: Provider
    kind: Kind
    ext_id: int
    title: str
    alt_titles: list[str]
    year: int | None
    overview: str
    poster_url: str | None
    imdb_id: str | None
    tmdb_id: int | None
    tvdb_id: int | None
    anilist_id: int | None


class SeasonSummary(TypedDict):
    season_number: int
    name: str
    episode_count: int
    air_year: int | None


class EpisodeSummary(TypedDict):
    episode_number: int
    name: str
    air_date: str | None


@dataclass(frozen=True)
class SmartQuery:
    mode: Literal["imdb", "numeric", "title"]
    value: str


_metadata_cache: TTLCache[Any] = TTLCache()


def clear_metadata_cache() -> None:
    _metadata_cache.clear()


def classify_query(raw: str) -> SmartQuery:
    s = (raw or "").strip()
    if re.fullmatch(r"tt\d+", s, re.IGNORECASE):
        return SmartQuery("imdb", s.lower())
    if s.isdigit():
        return SmartQuery("numeric", s)
    return SmartQuery("title", s)


def normalize_category(category: str | None) -> str:
    value = (category or "").strip().lower()
    return value if value in AD_HOC_CATEGORIES else DEFAULT_AD_HOC_CATEGORY


# --------------------------------------------------------------------------
# Hit construction
# --------------------------------------------------------------------------


def _year(value: Any) -> int | None:
    text = str(value or "")[:4]
    return int(text) if text.isdigit() and len(text) == 4 else None


def _tmdb_hit(
    raw: dict[str, Any],
    kind: Kind,
    *,
    imdb_id: str | None = None,
    tvdb_id: int | None = None,
) -> MediaHit:
    is_movie = kind in ("movie", "anime-movie")
    title = str(raw.get("title" if is_movie else "name") or "")
    original = str(raw.get("original_title" if is_movie else "original_name") or "")
    poster = raw.get("poster_path")
    return MediaHit(
        provider="tmdb",
        kind=kind,
        ext_id=int(raw["id"]),
        title=title,
        alt_titles=[original] if original and original != title else [],
        year=_year(raw.get("release_date" if is_movie else "first_air_date")),
        overview=str(raw.get("overview") or ""),
        poster_url=f"{_TMDB_POSTER_BASE}{poster}" if poster else None,
        imdb_id=imdb_id or raw.get("imdb_id") or None,
        tmdb_id=int(raw["id"]),
        tvdb_id=tvdb_id,
        anilist_id=None,
    )


def _anilist_kind(raw: dict[str, Any]) -> Kind:
    return "anime-movie" if raw.get("format") == "MOVIE" else "anime"


def _anilist_hit(raw: dict[str, Any], *, tvdb_id: int | None = None) -> MediaHit:
    names = raw.get("title") or {}
    ordered = [names.get("english"), names.get("romaji"), names.get("native")]
    distinct = list(dict.fromkeys(n for n in ordered if n))
    title = distinct[0] if distinct else ""
    cover = (raw.get("coverImage") or {}).get("large")
    return MediaHit(
        provider="anilist",
        kind=_anilist_kind(raw),
        ext_id=int(raw["id"]),
        title=title,
        alt_titles=distinct[1:],
        year=_year((raw.get("startDate") or {}).get("year")),
        overview=str(raw.get("description") or ""),
        poster_url=cover or None,
        imdb_id=None,
        tmdb_id=None,
        tvdb_id=tvdb_id,
        anilist_id=int(raw["id"]),
    )


# --------------------------------------------------------------------------
# Cached branches
# --------------------------------------------------------------------------


async def _cached(
    key: str, factory: Callable[[], Awaitable[list[Any] | None]]
) -> list[Any]:
    """Return the cached value or compute it; empty/failed results aren't cached.

    Exceptions from ``factory`` propagate (and are therefore never cached).
    """
    hit = _metadata_cache.get(key)
    if hit is not None:
        return list(hit)
    value = await factory()
    if not value:
        return []
    _metadata_cache.set(key, value)
    return list(value)


async def _search_tmdb(
    kind: Literal["movie", "series"],
    query: str,
    tvdb_id: int | None,
    session: AsyncSession | None,
) -> list[MediaHit]:
    search = tmdb.search_movies if kind == "movie" else tmdb.search_tv

    async def fetch() -> list[MediaHit]:
        raw = await search(query, session=session)
        return [_tmdb_hit(r, kind) for r in raw[:MAX_HITS_PER_BRANCH]]

    return _with_tvdb(
        await _cached(f"search:tmdb:{kind}:{query.lower()}", fetch), tvdb_id
    )


async def _search_anilist(
    kind: Literal["any", "anime", "anime-movie"],
    query: str,
    tvdb_id: int | None,
) -> list[MediaHit]:
    """Search AniList; ``anime`` drops films, ``anime-movie`` keeps only films."""

    async def fetch() -> list[MediaHit]:
        raw = await anilist.search_anime_media(query, per_page=MAX_HITS_PER_BRANCH)
        return [_anilist_hit(r) for r in raw]

    hits: list[MediaHit] = await _cached(f"search:anilist:anime:{query.lower()}", fetch)
    if kind == "anime":
        hits = [h for h in hits if h["kind"] == "anime"]
    elif kind == "anime-movie":
        hits = [h for h in hits if h["kind"] == "anime-movie"]
    return _with_tvdb(hits[:MAX_HITS_PER_BRANCH], tvdb_id)


def _with_tvdb(hits: list[MediaHit], tvdb_id: int | None) -> list[MediaHit]:
    if tvdb_id is None:
        return hits
    return [MediaHit(**{**h, "tvdb_id": tvdb_id}) for h in hits]  # type: ignore[typeddict-item]


async def _detail_tmdb(
    kind: Literal["movie", "series"],
    ext_id: int,
    tvdb_id: int | None,
    session: AsyncSession | None,
) -> list[MediaHit]:
    getter = tmdb.get_movie_details if kind == "movie" else tmdb.get_show_details

    async def fetch() -> list[MediaHit]:
        raw = await getter(ext_id, session=session)
        return [_tmdb_hit(raw, kind)] if raw else []

    return _with_tvdb(await _cached(f"detail:tmdb:{kind}:{ext_id}", fetch), tvdb_id)


async def _detail_anilist(
    kind: Literal["anime", "anime-movie"], ext_id: int, tvdb_id: int | None
) -> list[MediaHit]:
    raw = await _anilist_media(ext_id)
    if not raw:
        return []
    hit = _anilist_hit(raw)
    if kind == "anime-movie" and hit["kind"] != "anime-movie":
        return []
    return _with_tvdb([hit], tvdb_id)


async def _anilist_media(ext_id: int) -> dict[str, Any] | None:
    async def fetch() -> list[dict[str, Any]]:
        raw = await anilist.get_anime_media_by_id(ext_id)
        return [raw] if raw else []

    cached = await _cached(f"detail:anilist:anime:{ext_id}", fetch)
    return cached[0] if cached else None


async def _imdb_hits(
    imdb_id: str, category: str, tvdb_id: int | None, session: AsyncSession | None
) -> list[MediaHit]:
    async def fetch() -> list[Any]:
        found = await tmdb.find_all_by_imdb_id(imdb_id, session=session)
        return [found] if found.get("movie_results") or found.get("tv_results") else []

    found = (await _cached(f"find:tmdb:{imdb_id}", fetch) or [{}])[0]
    movies: list[dict[str, Any]] = found.get("movie_results") or []
    shows: list[dict[str, Any]] = found.get("tv_results") or []

    if category == "anime":
        seed = movies[0] if movies else shows[0] if shows else None
        if seed is None:
            return []
        title = str(seed.get("title") or seed.get("name") or "")
        return await _search_anilist("anime", title, tvdb_id) if title else []

    movie_kind: Kind = "anime-movie" if category == "anime-movie" else "movie"
    hits: list[MediaHit] = []
    if category in ("any", "movie", "anime-movie"):
        hits += [
            _tmdb_hit(r, movie_kind, imdb_id=imdb_id, tvdb_id=tvdb_id)
            for r in movies[:MAX_HITS_PER_BRANCH]
        ]
    if category in ("any", "series"):
        hits += [
            _tmdb_hit(r, "series", imdb_id=imdb_id, tvdb_id=tvdb_id)
            for r in shows[:MAX_HITS_PER_BRANCH]
        ]
    return hits


async def _numeric_hits(
    ext_id: int, category: str, tvdb_id: int | None, session: AsyncSession | None
) -> list[MediaHit]:
    if category == "movie":
        return await _detail_tmdb("movie", ext_id, tvdb_id, session)
    if category == "series":
        return await _detail_tmdb("series", ext_id, tvdb_id, session)
    if category in ("anime", "anime-movie"):
        kind: Literal["anime", "anime-movie"] = (
            "anime-movie" if category == "anime-movie" else "anime"
        )
        return await _detail_anilist(kind, ext_id, tvdb_id)
    return _flatten(
        await asyncio.gather(
            _detail_tmdb("movie", ext_id, tvdb_id, session),
            _detail_tmdb("series", ext_id, tvdb_id, session),
            return_exceptions=True,
        )
    )


async def _title_hits(
    title: str, category: str, tvdb_id: int | None, session: AsyncSession | None
) -> list[MediaHit]:
    if not title:
        return []
    if category == "movie":
        return await _search_tmdb("movie", title, tvdb_id, session)
    if category == "series":
        return await _search_tmdb("series", title, tvdb_id, session)
    if category in ("anime", "anime-movie"):
        kind: Literal["anime", "anime-movie"] = (
            "anime-movie" if category == "anime-movie" else "anime"
        )
        return await _search_anilist(kind, title, tvdb_id)
    return _flatten(
        await asyncio.gather(
            _search_tmdb("movie", title, tvdb_id, session),
            _search_tmdb("series", title, tvdb_id, session),
            _search_anilist("any", title, tvdb_id),
            return_exceptions=True,
        )
    )


def _flatten(branches: Sequence[list[MediaHit] | BaseException]) -> list[MediaHit]:
    hits: list[MediaHit] = []
    for branch in branches:
        if isinstance(branch, BaseException):
            logger.error("Ad-hoc media search branch failed: %s", branch)
            continue
        hits.extend(branch)
    return hits


async def search_media(
    query: str,
    category: str = "any",
    *,
    imdb_id: str | None = None,
    tmdb_id: int | None = None,
    tvdb_id: int | None = None,
    session: AsyncSession | None = None,
    force_title: bool = False,
) -> list[MediaHit]:
    """Tier 1 search. Precedence: imdb_id > tmdb_id > tvdb_id > smart query.

    A ``tvdb_id`` alone cannot be resolved to a title, so it only tags hits
    and the smart query still drives the lookup.
    """
    category = normalize_category(category)

    if imdb_id:
        smart = SmartQuery("imdb", imdb_id.strip().lower())
    elif tmdb_id is not None:
        smart = SmartQuery("numeric", str(tmdb_id))
    else:
        smart = classify_query(query)
        if force_title:
            smart = SmartQuery("title", (query or "").strip())

    if smart.mode == "imdb":
        return await _imdb_hits(smart.value, category, tvdb_id, session)
    if smart.mode == "numeric":
        return await _numeric_hits(int(smart.value), category, tvdb_id, session)
    return await _title_hits(smart.value, category, tvdb_id, session)


# --------------------------------------------------------------------------
# Tier 2
# --------------------------------------------------------------------------


def _anime_episode_count(raw: dict[str, Any]) -> int:
    episodes = raw.get("episodes")
    if episodes:
        return int(episodes)
    upcoming = (raw.get("nextAiringEpisode") or {}).get("episode")
    return max(int(upcoming) - 1, 0) if upcoming else 0


async def list_seasons(
    provider: str,
    ext_id: int,
    kind: Kind,
    session: AsyncSession | None = None,
) -> list[SeasonSummary]:
    if kind in ("movie", "anime-movie"):
        return []

    if provider == "anilist":

        async def fetch_anime() -> list[SeasonSummary]:
            raw = await _anilist_media(ext_id)
            if not raw:
                return []
            return [
                SeasonSummary(
                    season_number=1,
                    name="Season 1",
                    episode_count=_anime_episode_count(raw),
                    air_year=_year((raw.get("startDate") or {}).get("year")),
                )
            ]

        return await _cached(f"seasons:anilist:{ext_id}", fetch_anime)

    async def fetch_tv() -> list[SeasonSummary]:
        details = await tmdb.get_show_details(ext_id, session=session)
        return [
            SeasonSummary(
                season_number=int(s["season_number"]),
                name=str(s.get("name") or f"Season {s['season_number']}"),
                episode_count=int(s.get("episode_count") or 0),
                air_year=_year(s.get("air_date")),
            )
            for s in (details or {}).get("seasons") or []
            if s.get("season_number") != 0
        ]

    return await _cached(f"seasons:tmdb:{ext_id}", fetch_tv)


async def list_episodes(
    provider: str,
    ext_id: int,
    kind: Kind,
    season_number: int,
    session: AsyncSession | None = None,
) -> list[EpisodeSummary]:
    if kind in ("movie", "anime-movie"):
        return []

    if provider == "anilist":
        if season_number != 1:
            return []
        raw = await _anilist_media(ext_id)
        count = _anime_episode_count(raw) if raw else 0
        return [
            EpisodeSummary(episode_number=n, name=f"Episode {n}", air_date=None)
            for n in range(1, count + 1)
        ]

    async def fetch_tv() -> list[EpisodeSummary]:
        details = await tmdb.get_season_details(ext_id, season_number, session=session)
        return [
            EpisodeSummary(
                episode_number=int(e["episode_number"]),
                name=str(e.get("name") or f"Episode {e['episode_number']}"),
                air_date=e.get("air_date") or None,
            )
            for e in (details or {}).get("episodes") or []
        ]

    return await _cached(f"episodes:tmdb:{ext_id}:{season_number}", fetch_tv)
