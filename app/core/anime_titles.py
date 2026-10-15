"""Anime title language resolution (m65, ADR-144).

Pure helpers: pick the display title from stored AniList variants using pinned
fallback chains, plus search-corpus helpers. Display/search only; the scorer and
indexer queries keep using ``title`` / ``alt_title``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from app.db.models import MediaItem, Season

AnimeTitleLanguage = Literal["romaji", "english", "native"]
ANIME_TITLE_LANGUAGES: tuple[AnimeTitleLanguage, ...] = ("romaji", "english", "native")
DEFAULT_ANIME_TITLE_LANGUAGE: AnimeTitleLanguage = "romaji"

FALLBACK_CHAINS: dict[str, tuple[str, ...]] = {
    "english": ("english", "romaji", "native"),
    "romaji": ("romaji", "english", "native"),
    "native": ("native", "romaji", "english"),
}


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def normalize_anime_title_language(value: object) -> AnimeTitleLanguage:
    """Return ``value`` if it is a canonical language (case/space-insensitive), else romaji."""
    if isinstance(value, str):
        cleaned = value.strip().lower()
        for lang in ANIME_TITLE_LANGUAGES:
            if cleaned == lang:
                return lang
    return DEFAULT_ANIME_TITLE_LANGUAGE


def resolve_display_title(
    language: str,
    *,
    english: str | None,
    romaji: str | None,
    native: str | None,
    title: str | None,
) -> str:
    """First non-blank variant along the fallback chain, then ``title``, then ''."""
    variants = {"english": english, "romaji": romaji, "native": native}
    for key in FALLBACK_CHAINS[normalize_anime_title_language(language)]:
        cleaned = _clean(variants[key])
        if cleaned:
            return cleaned
    return _clean(title) or ""


def extract_title_variants(node: Mapping[str, Any]) -> dict[str, str | None]:
    """Map an AniList media node's ``title`` object to ``title_*`` column values."""
    raw = node.get("title")
    titles: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    return {
        "title_english": _clean(titles.get("english")),
        "title_romaji": _clean(titles.get("romaji")),
        "title_native": _clean(titles.get("native")),
    }


def is_anime_scope(item: MediaItem) -> bool:
    """True when the item is an anime series or an anime movie."""
    from app.db.models import MediaType

    return item.media_type == MediaType.ANIME or bool(item.is_anime_movie)


def anime_title_for(item: MediaItem, language: str) -> str:
    """Display title for ``item``; non-anime items always use ``title``."""
    return item.display_title(language) if is_anime_scope(item) else item.title


def anime_season_title_for(item: MediaItem, season: Season, language: str) -> str:
    """Display label for a consolidated child (season/movie)."""
    if is_anime_scope(item):
        resolved = season.display_title(language)
        if resolved:
            return resolved
    return season.title or f"Season {season.season_number}"


def search_names(*values: str | None) -> str:
    """Lower-cased, de-duplicated, space-joined non-blank names."""
    seen: list[str] = []
    for value in values:
        cleaned = _clean(value)
        if cleaned and cleaned.lower() not in seen:
            seen.append(cleaned.lower())
    return " ".join(seen)


def find_matched_child(
    children: Iterable[Mapping[str, Any]],
    parent_names: str,
    q: str,
    displayed_title: str,
) -> dict[str, str] | None:
    """First child matching ``q`` when the parent itself does not (hit badge)."""
    needle = q.strip().lower()
    if not needle or needle in parent_names.lower():
        return None
    shown = displayed_title.strip().lower()
    for child in children:
        title = str(child.get("title") or "")
        if needle in str(child.get("names") or "").lower() and (
            title.strip().lower() != shown
        ):
            return {"label": str(child.get("label") or ""), "title": title}
    return None


def apply_title_variants(target: MediaItem | Season, node: Mapping[str, Any]) -> None:
    """Write AniList title variants onto ``target``; blank values never overwrite."""
    for column, value in extract_title_variants(node).items():
        if value:
            setattr(target, column, value)
