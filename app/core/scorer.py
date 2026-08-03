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
    age_days: int = 0
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
