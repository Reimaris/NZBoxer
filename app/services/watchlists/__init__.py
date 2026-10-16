"""Watchlist providers (Simkl, AniList, Plex, MyAnimeList, Jellyseerr, Overseerr)."""

WATCHLIST_TYPES: frozenset[str] = frozenset(
    {"simkl", "anilist", "plex", "mal", "jellyseerr", "overseerr"}
)
WATCHLIST_DISPLAY_NAMES: dict[str, str] = {
    "simkl": "Simkl",
    "anilist": "AniList",
    "plex": "Plex",
    "mal": "MyAnimeList",
    "jellyseerr": "Jellyseerr",
    "overseerr": "Overseerr",
}
