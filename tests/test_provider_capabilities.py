"""Provider capability registry (m70, ADR-153)."""

from __future__ import annotations

import dataclasses

import pytest

from app.services.downloaders.base import DOWNLOADER_TYPES
from app.services.indexers.catalog import INDEXER_PRESETS
from app.services.provider_capabilities import (
    CAPABILITIES,
    CATEGORY_ORDER,
    capabilities_by_category,
    get_capability,
    sources_support_auto_advance,
)
from app.services.provider_service import provider_category_for_type
from app.services.watchlists import WATCHLIST_TYPES

ALL_SLUGS = (
    set(WATCHLIST_TYPES)
    | set(DOWNLOADER_TYPES)
    | {"tmdb", "newznab"}
    | {p.type for p in INDEXER_PRESETS}
)


@pytest.mark.parametrize("slug", sorted(ALL_SLUGS))
def test_registry_complete_and_category_matches(slug: str) -> None:
    cap = CAPABILITIES[slug]
    assert cap.provider_type == slug
    assert cap.category == provider_category_for_type(slug)


def test_registry_has_no_extra_entries() -> None:
    assert set(CAPABILITIES) == ALL_SLUGS


def test_indexer_display_names_from_presets() -> None:
    for p in INDEXER_PRESETS:
        assert CAPABILITIES[p.type].display_name == p.name


_ALL = ("movies", "series", "anime")
# slug: (category, coverage, progress, air_dates, kind, unpack, delete, cat_map, custom)
PINNED = {
    "simkl": ("watchlist", _ALL, True, True, "", False, False, False, False),
    "anilist": ("watchlist", ("anime",), True, True, "", False, False, False, False),
    "mal": ("watchlist", ("anime",), True, False, "", False, False, False, False),
    "plex": (
        "watchlist",
        ("movies", "series"),
        False,
        False,
        "",
        False,
        False,
        False,
        False,
    ),
    "jellyseerr": (
        "watchlist",
        ("movies", "series"),
        False,
        False,
        "",
        False,
        False,
        False,
        False,
    ),
    "overseerr": (
        "watchlist",
        ("movies", "series"),
        False,
        False,
        "",
        False,
        False,
        False,
        False,
    ),
    "tmdb": (
        "metadata",
        ("movies", "series"),
        False,
        False,
        "",
        False,
        False,
        False,
        False,
    ),
    "torbox": ("downloader", _ALL, False, False, "cloud", True, True, False, False),
    "premiumize": (
        "downloader",
        _ALL,
        False,
        False,
        "cloud",
        False,
        True,
        False,
        False,
    ),
    "sabnzbd": ("downloader", _ALL, False, False, "local", True, True, True, False),
    "newznab": ("indexer", _ALL, False, False, "", False, False, False, True),
}
LIMITATIONS = {
    "plex": (
        "No watch progress (cannot trigger season auto-advance)",
        "No exact air dates (uses TMDB dates)",
    ),
    "mal": ("No exact air dates (uses AniList/TMDB dates)",),
    "premiumize": ("No unpack verification (completion is reported by Premiumize)",),
    "tmdb": ("No exact air dates for anime (AniList provides them)",),
    "simkl": ("Exact air dates for TV only",),
    "anilist": ("Exact air dates for anime only",),
}


@pytest.mark.parametrize("slug,expected", sorted(PINNED.items()))
def test_pinned_values(slug: str, expected: tuple) -> None:
    c = CAPABILITIES[slug]
    assert (
        c.category,
        c.media_coverage,
        c.supports_watch_progress,
        c.supports_exact_air_dates,
        c.downloader_kind,
        c.supports_unpack_verification,
        c.supports_remote_delete,
        c.supports_category_mapping,
        c.supports_custom_categories,
    ) == expected


@pytest.mark.parametrize("slug,expected", sorted(LIMITATIONS.items()))
def test_pinned_limitations(slug: str, expected: tuple[str, ...]) -> None:
    assert CAPABILITIES[slug].limitations == expected


def test_indexer_presets_share_indexer_values() -> None:
    for p in INDEXER_PRESETS:
        c = CAPABILITIES[p.type]
        assert (c.category, c.media_coverage, c.supports_custom_categories) == (
            "indexer",
            _ALL,
            True,
        )


def test_registry_immutable() -> None:
    with pytest.raises(TypeError):
        CAPABILITIES["plex"] = CAPABILITIES["mal"]  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        CAPABILITIES["plex"].supports_watch_progress = True  # type: ignore[misc]


def test_get_capability_case_insensitive() -> None:
    assert get_capability("  PLEX ") is CAPABILITIES["plex"]
    assert get_capability("bogus") is None
    assert get_capability("") is None


def test_capabilities_by_category_order() -> None:
    grouped = capabilities_by_category()
    assert tuple(grouped) == CATEGORY_ORDER
    assert all(grouped[c] for c in CATEGORY_ORDER)
    assert all(cap.category == c for c, caps in grouped.items() for cap in caps)


@pytest.mark.parametrize(
    "types,expected",
    [
        (["plex"], False),
        (["plex", "mal"], True),
        (["Simkl"], True),
        ([], False),
        (["bogus"], False),
        (["bogus", "anilist"], True),
    ],
)
def test_sources_support_auto_advance(types: list[str], expected: bool) -> None:
    assert sources_support_auto_advance(types) is expected
