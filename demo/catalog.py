"""Fictional demo library shared by the seeder and the mock services.

Every title, ID and name here is invented; nothing is derived from a real library.
"""

from __future__ import annotations

from typing import Any

# kind: movie | show | anime. tmdb ids double as the lookup key for the mock indexer.
MOVIES: list[dict[str, Any]] = [
    {
        "tmdb": 910001,
        "title": "Midnight Cartographer",
        "year": 2025,
        "state": "downloaded",
    },
    {"tmdb": 910002, "title": "The Lantern Keeper", "year": 2026, "state": "ready"},
    {"tmdb": 910003, "title": "Copper Sky", "year": 2024, "state": "ready"},
    {"tmdb": 910004, "title": "Salt and Static", "year": 2027, "state": "future"},
    {"tmdb": 910005, "title": "A Quiet Harvest", "year": 2025, "state": "downloading"},
]

# seasons: list of (status, episode_count); watched = seasons the user finished.
SHOWS: list[dict[str, Any]] = [
    {
        "tmdb": 920001,
        "title": "Harbor Lights",
        "year": 2022,
        "seasons": ["downloaded", "downloaded", "searching"],
        "watched": 2,
        "auto_advance": True,
    },
    {
        "tmdb": 920002,
        "title": "The Glass Orchard",
        "year": 2025,
        "seasons": ["searching"],
        "watched": 0,
    },
    {
        "tmdb": 920003,
        "title": "Quiet Orbit",
        "year": 2023,
        "seasons": ["downloaded", "future"],
        "watched": 1,
    },
    {
        "tmdb": 920004,
        "title": "Northern Signal",
        "year": 2021,
        "seasons": ["downloaded", "searching"],
        "watched": 1,
        "list_status": "on_hold",
    },
    {
        "tmdb": 920005,
        "title": "Paper Lanterns",
        "year": 2024,
        "seasons": ["searching", "searching"],
        "watched": 0,
        "downloading_season": 1,
    },
]

# entries: (entry_type, title, status)
ANIME: list[dict[str, Any]] = [
    {
        "tmdb": 930001,
        "anilist": 990001,
        "title": "Skybound Tide",
        "year": 2021,
        "entries": [
            ("season", "Skybound Tide", "downloaded"),
            ("season", "Skybound Tide 2nd Season", "downloaded"),
            ("movie", "Skybound Tide: The Last Current", "searching"),
            ("season", "Skybound Tide 3rd Season", "searching"),
        ],
        "watched": 2,
    },
    {
        "tmdb": 930002,
        "anilist": 990002,
        "title": "Ember Academy",
        "year": 2024,
        "entries": [
            ("season", "Ember Academy", "downloaded"),
            ("season", "Ember Academy Season 2", "future"),
        ],
        "watched": 1,
    },
    {
        "tmdb": 930003,
        "anilist": 990003,
        "title": "Moonlit Courier",
        "year": 2025,
        "entries": [("season", "Moonlit Courier", "searching")],
        "watched": 0,
    },
]

# Titles only discoverable through ad-hoc search (not on any watchlist).
ADHOC: list[dict[str, Any]] = [
    {"tmdb": 940001, "kind": "movie", "title": "Neon Meridian", "year": 2023},
    {"tmdb": 940002, "kind": "movie", "title": "Neon Meridian 2", "year": 2025},
    {"tmdb": 940003, "kind": "show", "title": "Neon Meridian: Origins", "year": 2024},
]


def all_titles() -> dict[int, dict[str, Any]]:
    """Map tmdb id -> {title, year, kind, seasons} for the mock services."""
    out: dict[int, dict[str, Any]] = {}
    for m in MOVIES:
        out[m["tmdb"]] = {
            "title": m["title"],
            "year": m["year"],
            "kind": "movie",
            "seasons": 0,
        }
    for s in SHOWS:
        out[s["tmdb"]] = {
            "title": s["title"],
            "year": s["year"],
            "kind": "show",
            "seasons": len(s["seasons"]),
        }
    for a in ANIME:
        out[a["tmdb"]] = {
            "title": a["title"],
            "year": a["year"],
            "kind": "show",
            "seasons": len(a["entries"]),
        }
    for x in ADHOC:
        out[x["tmdb"]] = {
            "title": x["title"],
            "year": x["year"],
            "kind": x["kind"],
            "seasons": 2 if x["kind"] == "show" else 0,
        }
    return out
