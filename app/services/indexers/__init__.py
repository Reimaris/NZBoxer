"""Indexer providers (Newznab-compatible) and their preset catalog."""

from __future__ import annotations

_LEGACY_INDEXER_TYPE = "treasure_maps"  # legacy domain migration (allowlisted)


def normalize_indexer_type(raw: str | None) -> str:
    """Map a stored indexer ``Provider.type`` to its current slug.

    ``None``/empty and the pre-m59 legacy type become ``newznab``;
    every other value (e.g. a preset slug) is returned unchanged.
    """
    if not raw or raw == _LEGACY_INDEXER_TYPE:
        return "newznab"
    return raw
