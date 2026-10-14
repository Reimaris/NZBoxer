"""Experimental flag: providers the maintainer has not tested against the live service."""

from __future__ import annotations

from app.services.indexers.catalog import INDEXER_PRESETS, get_preset

# Non-indexer provider types without maintainer testing; indexers carry
# `IndexerPreset.experimental` instead.
EXPERIMENTAL_PROVIDER_TYPES: frozenset[str] = frozenset(
    {"anilist", "plex", "mal", "sabnzbd", "premiumize"}
)


def is_experimental(provider_type: str | None) -> bool:
    slug = (provider_type or "").strip().lower()
    preset = get_preset(slug)
    if preset is not None:
        return preset.experimental
    return slug in EXPERIMENTAL_PROVIDER_TYPES


def experimental_types() -> list[str]:
    """All experimental provider type slugs (for client-side pill toggling)."""
    return sorted(
        EXPERIMENTAL_PROVIDER_TYPES
        | {p.type for p in INDEXER_PRESETS if p.experimental}
    )
