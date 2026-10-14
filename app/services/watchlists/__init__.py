"""Watchlist providers (Simkl, AniList, Plex, MyAnimeList)."""

WATCHLIST_TYPES: frozenset[str] = frozenset({"simkl", "anilist", "plex", "mal"})
WATCHLIST_DISPLAY_NAMES: dict[str, str] = {
    "simkl": "Simkl",
    "anilist": "AniList",
    "plex": "Plex",
    "mal": "MyAnimeList",
}
