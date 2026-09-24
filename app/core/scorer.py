"""
Scoring Engine
==============
Calculates a numerical score for a parsed release based on `config.yaml`.
Evaluates whitelists, blacklists, and minimum constraints.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.config import scoring_config
from app.core.parser import ParsedRelease

logger = logging.getLogger(__name__)

_TOKEN_REGEX = re.compile(r"[a-z0-9]+")


def _tokenize(t: str) -> set[str]:
    return set(_TOKEN_REGEX.findall(t.lower()))


@dataclass
class ScoreResult:
    """Result of scoring an NZB release."""

    score: float
    is_rejected: bool
    reject_reason: str | None
    bitrate_mbps: float | None
    is_primary: bool = True
    is_fallback: bool = False
    matched_language: str | None = None


def calculate_bitrate_mbps(
    size_bytes: int, runtime_minutes: int | None
) -> float | None:
    """Calculate the average bitrate in Megabits per second."""
    if not size_bytes or not runtime_minutes or runtime_minutes <= 0:
        return None
    # Megabits = (bytes * 8) / 1_000_000
    # Seconds = minutes * 60
    megabits = (size_bytes * 8) / 1_000_000
    seconds = runtime_minutes * 60
    return megabits / seconds


def score_release(
    parsed: ParsedRelease,
    size_bytes: int,
    runtime_minutes: int | None = None,
    age_days: int = 0,
    expected_title: str | None = None,
    expected_year: int | None = None,
    expected_alt_title: str | None = None,
    expected_season: int | None = None,
    expected_episode: int | None = None,
    expected_season_title: str | None = None,
    required_language: str | None = None,
    primary_language: str | None = None,
    fallback_language: str | None = None,
    api_language: str | None = None,
) -> ScoreResult:
    """Calculate the score for a parsed release."""
    # 1. Apply hard filters (size, age, blacklist)
    filters = scoring_config.get("filters", {})
    min_size_mb = filters.get("min_size_mb", 0)
    max_size_gb = filters.get("max_size_gb", 9999)
    min_nzb_age = filters.get("min_nzb_age_days", 0)

    size_mb = size_bytes / (1024 * 1024)
    size_gb = size_mb / 1024

    if size_mb < min_size_mb:
        return ScoreResult(
            0, True, f"Too small: {size_mb:.1f}MB < {min_size_mb}MB", None
        )
    if size_gb > max_size_gb:
        return ScoreResult(
            0, True, f"Too large: {size_gb:.1f}GB > {max_size_gb}GB", None
        )
    if age_days < min_nzb_age:
        return ScoreResult(0, True, f"Too new: {age_days}d < {min_nzb_age}d", None)

    # Title matching
    if expected_title and parsed.title:

        def normalize(t: str) -> str:
            # Remove punctuation and lowercase
            return re.sub(r"[^a-z0-9]", "", t.lower())

        norm_expected = normalize(expected_title)
        norm_parsed = normalize(parsed.title)

        match_found = (norm_expected in norm_parsed) or (norm_parsed in norm_expected)

        if not match_found and expected_alt_title:
            norm_alt = normalize(expected_alt_title)
            match_found = (norm_alt in norm_parsed) or (norm_parsed in norm_alt)

        if not match_found and expected_season_title:
            norm_st = normalize(expected_season_title)
            match_found = (norm_st in norm_parsed) or (norm_parsed in norm_st)

        if not match_found:
            return ScoreResult(
                0,
                True,
                f"Title mismatch: '{parsed.title}' vs '{expected_title}' (alt: '{expected_alt_title}', season: '{expected_season_title}')",
                None,
            )

    # Year matching (allow +/- 1 year tolerance for movies)
    if expected_year and parsed.year:
        if abs(parsed.year - expected_year) > 1:
            return ScoreResult(
                0, True, f"Year mismatch: {parsed.year} != {expected_year}", None
            )

    # Season and Episode matching
    if expected_season is not None:
        if parsed.season and parsed.season != expected_season:
            return ScoreResult(
                0, True, f"Season mismatch: {parsed.season} != {expected_season}", None
            )

        # Cross-season anime subtitle isolation check:
        # If expected_season >= 2, parsed has no explicit season tag,
        # and a distinct expected_season_title exists (e.g. "Motto To LOVE-Ru" vs "To LOVE-Ru"):
        if (
            expected_season >= 2
            and parsed.season is None
            and expected_season_title
            and expected_title
        ):
            base_tokens = _tokenize(expected_title)
            season_tokens = _tokenize(expected_season_title)
            distinctive_tokens = season_tokens - base_tokens

            parsed_tokens = _tokenize(parsed.title or "")

            if distinctive_tokens and not (distinctive_tokens & parsed_tokens):
                return ScoreResult(
                    0,
                    True,
                    f"Cross-season isolation: Release '{parsed.title}' missing required season subtitle keywords {distinctive_tokens} for Season {expected_season}",
                    None,
                )

        if expected_episode is not None:
            # We are looking for a specific episode
            if parsed.episode and parsed.episode != expected_episode:
                return ScoreResult(
                    0,
                    True,
                    f"Episode mismatch: {parsed.episode} != {expected_episode}",
                    None,
                )
            if not parsed.episode:
                # If we expect an episode but none is found, it might be a season pack or misparsed
                return ScoreResult(
                    0,
                    True,
                    f"Missing episode in release name, expected {expected_episode}",
                    None,
                )
        else:
            # We are looking for a season pack. Reject if it's a single episode!
            if parsed.episode is not None:
                return ScoreResult(
                    0,
                    True,
                    f"Rejected single episode {parsed.episode} for season pack search",
                    None,
                )

    # Language check — normalize common variants and evaluate Primary / Fallback hierarchy
    _LANG_MAP = {
        # English
        "english": "en",
        "eng": "en",
        "en": "en",
        # German
        "german": "de",
        "deutsch": "de",
        "ger": "de",
        "de": "de",
        # French — VOSTFR (French audio or subtitles) counts as French
        "french": "fr",
        "fra": "fr",
        "fre": "fr",
        "vostfr": "fr",
        "vf": "fr",
        "fr": "fr",
        # Japanese
        "japanese": "ja",
        "jpn": "ja",
        "ja": "ja",
        # Spanish
        "spanish": "es",
        "spa": "es",
        "es": "es",
        # Italian
        "italian": "it",
        "ita": "it",
        "it": "it",
        # Portuguese
        "portuguese": "pt",
        "por": "pt",
        "pt": "pt",
        # Russian
        "russian": "ru",
        "rus": "ru",
        "ru": "ru",
    }

    if primary_language is None and required_language:
        primary_language = required_language

    primary_norm = (
        _LANG_MAP.get(
            primary_language.strip().lower(), primary_language.strip().lower()
        )
        if primary_language
        and primary_language.strip().lower() not in ("any", "none", "")
        else None
    )
    fallback_norm = (
        _LANG_MAP.get(
            fallback_language.strip().lower(), fallback_language.strip().lower()
        )
        if fallback_language
        and fallback_language.strip().lower() not in ("any", "none", "")
        else None
    )

    release_langs: set[str] = set()
    if parsed.languages:
        for l in parsed.languages:
            release_langs.add(_LANG_MAP.get(l.lower(), l.lower()))

    if api_language:
        api_langs = [
            l.strip().lower()
            for l in api_language.replace("/", ",").split(",")
            if l.strip()
        ]
        for al in api_langs:
            release_langs.add(_LANG_MAP.get(al, al))

    # Dual-Language / multi check (e.g. .DL., .Dual., .Multi.)
    title_lower = (parsed.title or "").lower()
    orig_lower = (parsed.original_title or "").lower()
    full_text = f"{title_lower} {orig_lower}"
    is_dual_language = bool(re.search(r"(\.dl\b|\bdual\b|\bmulti\b)", full_text))

    # ADR-058: Usenet Scene Unflagged Language Inference Policy
    # If neither title nor API identifies a language, infer default as English ('en')
    if not release_langs:
        release_langs = {"en"}

    # If it is a dual-language release, ensure English is also recognized alongside foreign audio
    if is_dual_language:
        release_langs.add("en")

    is_primary = True
    is_fallback = False
    matched_language = list(release_langs)[0] if release_langs else None

    if primary_norm is not None:
        if primary_norm in release_langs:
            is_primary = True
            is_fallback = False
            matched_language = primary_norm
        elif fallback_norm is not None and fallback_norm in release_langs:
            is_primary = False
            is_fallback = True
            matched_language = fallback_norm
        else:
            return ScoreResult(
                0,
                True,
                f"Language mismatch: release has {release_langs}, expected primary '{primary_norm}' or fallback '{fallback_norm}'",
                None,
                is_primary=False,
                is_fallback=False,
                matched_language=None,
            )
    elif fallback_norm is not None:
        if fallback_norm in release_langs:
            is_primary = True
            is_fallback = False
            matched_language = fallback_norm
        else:
            return ScoreResult(
                0,
                True,
                f"Language mismatch: release has {release_langs}, expected fallback '{fallback_norm}'",
                None,
                is_primary=False,
                is_fallback=False,
                matched_language=None,
            )

    groups_cfg = scoring_config.get("release_groups", {})
    blacklist = [g.lower() for g in groups_cfg.get("blacklist", [])]
    whitelist = [g.lower() for g in groups_cfg.get("whitelist", [])]
    group = parsed.release_group.lower() if parsed.release_group else ""

    if group and group in blacklist:
        return ScoreResult(0, True, f"Blacklisted group: {group}", None)

    # 2. Base Scoring
    score = 0.0
    cfg = scoring_config.get("scoring", {})

    def add_score(category: str, key: str | None) -> None:
        nonlocal score
        cat_scores = cfg.get(category, {})
        if not key:
            score += cat_scores.get("default", 0)
            return

        # Exact match
        if key in cat_scores:
            score += cat_scores[key]
            return

        # Substring match (e.g. 'dts-hd' in 'dts-hd ma')
        for k, v in cat_scores.items():
            if k != "default" and k in key:
                score += v
                return

        score += cat_scores.get("default", 0)

    add_score("resolution", parsed.resolution)
    add_score("video_codec", parsed.video_codec)
    add_score("audio_codec", parsed.audio_codec)
    add_score("audio_channels", parsed.audio_channels)
    add_score("source", parsed.source)

    # HDR matching (guessit 'other' or 'color_depth' can contain multiple tokens)
    if parsed.hdr:
        add_score("hdr", parsed.hdr)

    # Whitelist bonus
    lang_cfg = scoring_config.get("language_preferences", {})
    pref_langs = [l.lower() for l in lang_cfg.get("preferred", [])]
    if pref_langs and parsed.languages:
        if any(l in pref_langs for l in parsed.languages):
            score += lang_cfg.get("preferred_bonus", 0)
        else:
            score -= lang_cfg.get("missing_penalty", 0)
    elif (
        pref_langs
    ):  # No languages parsed, apply penalty just in case? Usually we don't.
        pass

    # 3. Bitrate Scoring with Codec Efficiency Multipliers
    bitrate_mbps = calculate_bitrate_mbps(size_bytes, runtime_minutes)

    # Determine codec multiplier for bitrate compensation
    codec_mult = 1.0
    if parsed.video_codec:
        vc = parsed.video_codec.lower()
        if "av1" in vc:
            codec_mult = 2.0  # AV1 is highly efficient
        elif "hevc" in vc or "h265" in vc or "h.265" in vc:
            codec_mult = 1.5  # HEVC is ~50% more efficient than H.264
        elif "h264" in vc or "avc" in vc or "h.264" in vc:
            codec_mult = 1.0

    if bitrate_mbps is not None:
        effective_bitrate = bitrate_mbps * codec_mult
        bitrate_brackets = cfg.get("bitrate_brackets", [])
        for bracket in sorted(
            bitrate_brackets, key=lambda x: x["min_mbps"], reverse=True
        ):
            if effective_bitrate >= bracket["min_mbps"]:
                score += bracket["score"]
                break

    # 4. Final Multipliers (Whitelist)
    if group and group in whitelist:
        whitelist_mult = groups_cfg.get("whitelist_multiplier", 1.2)
        score *= whitelist_mult
        # Fallback for older configs that only had additive bonus
        score += groups_cfg.get("whitelist_bonus", 0)

    return ScoreResult(
        score,
        False,
        None,
        bitrate_mbps,
        is_primary=is_primary,
        is_fallback=is_fallback,
        matched_language=matched_language,
    )
