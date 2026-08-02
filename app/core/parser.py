"""
Release Name Parser
===================
Uses `guessit` to extract structured metadata from raw NZB release names.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from guessit import guessit

logger = logging.getLogger(__name__)


@dataclass
class ParsedRelease:
    """Structured representation of an NZB release."""
    original_title: str
    title: str | None
    year: int | None
    resolution: str | None
    video_codec: str | None
    audio_codec: str | None
    audio_channels: str | None
    source: str | None
    release_group: str | None
    hdr: str | None
    languages: list[str]


def parse_release_name(release_name: str) -> ParsedRelease:
    """Parse a raw NZB release name using guessit.

    Args:
        release_name: The raw string (e.g. 'Movie.2023.2160p.UHD.BluRay.x265.DTS-HD.MA.7.1-GROUP')

    Returns:
        ParsedRelease dataclass with extracted fields.
    """
    try:
        guess = guessit(release_name)
    except Exception as e:
        logger.error("Guessit failed to parse '%s': %s", release_name, e)
        guess = {}

    def _get_str(key: str) -> str | None:
        val = guess.get(key)
        if isinstance(val, list) and val:
            return str(val[0]).lower()
        return str(val).lower() if val else None

    # Handle lists for languages
    lang_val = guess.get("language")
    languages = []
    if isinstance(lang_val, list):
        languages = [str(l).lower() for l in lang_val]
    elif lang_val:
        languages = [str(lang_val).lower()]

    return ParsedRelease(
        original_title=release_name,
        title=guess.get("title"),
        year=guess.get("year"),
        resolution=_get_str("screen_size"),
        video_codec=_get_str("video_codec"),
        audio_codec=_get_str("audio_codec"),
        audio_channels=_get_str("audio_channels"),
        source=_get_str("source"),
        release_group=_get_str("release_group"),
        hdr=_get_str("color_depth") or _get_str("other"), # Guessit sometimes puts HDR in 'other'
        languages=languages
    )
