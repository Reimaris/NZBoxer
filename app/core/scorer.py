"""
Scoring Engine
==============
Calculates a numerical score for a parsed release based on `config.yaml`.
Evaluates whitelists, blacklists, and minimum constraints.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import scoring_config
from app.core.parser import ParsedRelease

logger = logging.getLogger(__name__)


@dataclass
class ScoreResult:
    """Result of scoring an NZB release."""
    score: float
    is_rejected: bool
    reject_reason: str | None
    bitrate_mbps: float | None


def calculate_bitrate_mbps(size_bytes: int, runtime_minutes: int | None) -> float | None:
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
    runtime_minutes: int | None,
    age_days: int = 0,
    expected_title: str | None = None,
    expected_year: int | None = None,
    expected_alt_title: str | None = None,
    expected_season: int | None = None,
    expected_episode: int | None = None,
    required_language: str | None = None,
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
        return ScoreResult(0, True, f"Too small: {size_mb:.1f}MB < {min_size_mb}MB", None)
    if size_gb > max_size_gb:
        return ScoreResult(0, True, f"Too large: {size_gb:.1f}GB > {max_size_gb}GB", None)
    if age_days < min_nzb_age:
        return ScoreResult(0, True, f"Too new: {age_days}d < {min_nzb_age}d", None)

    # Title matching
    if expected_title and parsed.title:
        import re
        def normalize(t: str) -> str:
            # Remove punctuation and lowercase
            return re.sub(r'[^a-z0-9]', '', t.lower())
        
        norm_expected = normalize(expected_title)
        norm_parsed = normalize(parsed.title)
        
        match_found = (norm_expected in norm_parsed) or (norm_parsed in norm_expected)
        
        if not match_found and expected_alt_title:
            norm_alt = normalize(expected_alt_title)
            match_found = (norm_alt in norm_parsed) or (norm_parsed in norm_alt)
            
        if not match_found:
            return ScoreResult(0, True, f"Title mismatch: '{parsed.title}' vs '{expected_title}' (alt: '{expected_alt_title}')", None)

    # Year matching (allow +/- 1 year tolerance for movies)
    if expected_year and parsed.year:
        if abs(parsed.year - expected_year) > 1:
            return ScoreResult(0, True, f"Year mismatch: {parsed.year} != {expected_year}", None)

    # Season and Episode matching
    if expected_season is not None:
        if parsed.season and parsed.season != expected_season:
            return ScoreResult(0, True, f"Season mismatch: {parsed.season} != {expected_season}", None)
        
        if expected_episode is not None:
            # We are looking for a specific episode
            if parsed.episode and parsed.episode != expected_episode:
                return ScoreResult(0, True, f"Episode mismatch: {parsed.episode} != {expected_episode}", None)
            if not parsed.episode:
                # If we expect an episode but none is found, it might be a season pack or misparsed
                return ScoreResult(0, True, f"Missing episode in release name, expected {expected_episode}", None)
        else:
            # We are looking for a season pack. Reject if it's a single episode!
            if parsed.episode is not None:
                return ScoreResult(0, True, f"Rejected single episode {parsed.episode} for season pack search", None)

    # Language check — normalize common variants then hard-reject if language not present
    if required_language and parsed.languages:
        _LANG_MAP = {
            # English
            "english": "en", "eng": "en",
            # German
            "german": "de", "deutsch": "de", "ger": "de",
            # French — VOSTFR (French audio or subtitles) counts as French
            "french": "fr", "fra": "fr", "fre": "fr", "vostfr": "fr", "vf": "fr",
            # Japanese
            "japanese": "ja", "jpn": "ja",
            # Spanish
            "spanish": "es", "spa": "es",
            # Italian
            "italian": "it", "ita": "it",
            # Portuguese
            "portuguese": "pt", "por": "pt",
            # Russian
            "russian": "ru", "rus": "ru",
        }
        req_lang = required_language.strip().lower()
        req_lang = _LANG_MAP.get(req_lang, req_lang)  # normalize
        release_langs = {_LANG_MAP.get(l.lower(), l.lower()) for l in parsed.languages}
        if req_lang not in release_langs:
            return ScoreResult(0, True, f"Language mismatch: release has {list(parsed.languages)} but required '{required_language}'", None)

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
    elif pref_langs: # No languages parsed, apply penalty just in case? Usually we don't.
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
        for bracket in sorted(bitrate_brackets, key=lambda x: x["min_mbps"], reverse=True):
            if effective_bitrate >= bracket["min_mbps"]:
                score += bracket["score"]
                break

    # 4. Final Multipliers (Whitelist)
    if group and group in whitelist:
        whitelist_mult = groups_cfg.get("whitelist_multiplier", 1.2)
        score *= whitelist_mult
        # Fallback for older configs that only had additive bonus
        score += groups_cfg.get("whitelist_bonus", 0)

    return ScoreResult(score, False, None, bitrate_mbps)
