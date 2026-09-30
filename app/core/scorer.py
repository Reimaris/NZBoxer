"""
Scoring Engine
==============
Calculates a numerical score for a parsed release based on `config.yaml`.
Evaluates whitelists, blacklists, and minimum constraints.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.config import scoring_config
from app.core.parser import ParsedRelease

logger = logging.getLogger(__name__)

_TOKEN_REGEX = re.compile(r"[a-z0-9]+")


def _tokenize(t: str) -> set[str]:
    return set(_TOKEN_REGEX.findall(t.lower()))


def _normalize_str_list(val: Any) -> list[str]:
    if not val:
        return []
    if isinstance(val, str):
        return [x.strip().lower() for x in val.split(",") if x.strip()]
    if isinstance(val, (list, tuple, set)):
        return [str(x).strip().lower() for x in val if str(x).strip()]
    return []


def _matches_custom_resolution(
    allowed_resolutions: list[str], parsed_res: str | None, orig_lower: str
) -> bool:
    res = (parsed_res or "").lower()
    for target in allowed_resolutions:
        norm_t = "2160p" if target in ("4k", "uhd", "2160p") else target
        if norm_t and (norm_t in res or norm_t in orig_lower):
            return True
    return False


def _matches_custom_source(
    allowed_sources: list[str], parsed_src: str | None, orig_lower: str
) -> bool:
    src = (parsed_src or "").lower()
    combined = f"{src} {orig_lower}"
    for target in allowed_sources:
        t = target.strip().lower()
        if t == "remux" and "remux" in combined:
            return True
        if t in ("bluray", "blu-ray", "bdrip", "brrip") and any(
            k in combined for k in ("bluray", "blu-ray", "bdrip", "brrip")
        ):
            return True
        if t in ("web-dl", "webdl", "web") and any(
            k in combined for k in ("web-dl", "webdl", "web")
        ):
            return True
        if t == "webrip" and "webrip" in combined:
            return True
        if t == "hdtv" and "hdtv" in combined:
            return True
        if t and t in combined:
            return True
    return False


def _matches_custom_video_codec(
    allowed_codecs: list[str],
    parsed_vc: str | None,
    parsed_hdr: str | None,
    orig_lower: str,
) -> bool:
    vc = (parsed_vc or "").lower()
    hdr = (parsed_hdr or "").lower()
    combined = f"{vc} {hdr} {orig_lower}"
    for target in allowed_codecs:
        t = target.strip().lower()
        if t in ("h265", "hevc", "x265", "h.265") and any(
            k in combined for k in ("h265", "hevc", "x265", "h.265")
        ):
            return True
        if t in ("h264", "avc", "x264", "h.264") and any(
            k in combined for k in ("h264", "avc", "x264", "h.264")
        ):
            return True
        if t == "av1" and "av1" in combined:
            return True
        if t in ("dv", "dolby vision", "dovi") and any(
            k in combined for k in ("dv", "dolby vision", "dovi")
        ):
            return True
        if t in ("hdr10+", "hdr10plus") and any(
            k in combined for k in ("hdr10+", "hdr10plus")
        ):
            return True
        if t in ("hdr", "hdr10") and "hdr" in combined:
            return True
        if t and t in combined:
            return True
    return False


def _matches_custom_audio_format(
    allowed_formats: list[str],
    parsed_ac: str | None,
    parsed_ch: str | None,
    orig_lower: str,
) -> bool:
    ac = (parsed_ac or "").lower()
    ch = (parsed_ch or "").lower()
    combined = f"{ac} {ch} {orig_lower}"
    for target in allowed_formats:
        t = target.strip().lower()
        if t == "truehd" and "truehd" in combined:
            return True
        if t == "atmos" and "atmos" in combined:
            return True
        if t in ("dts-hd", "dtshd", "dts-hd ma") and any(
            k in combined for k in ("dts-hd", "dtshd", "dts.hd")
        ):
            return True
        if t in ("dts:x", "dtsx", "dts-x") and any(
            k in combined for k in ("dts:x", "dtsx", "dts-x")
        ):
            return True
        if t in ("eac3", "e-ac-3", "dd+", "ddp") and any(
            k in combined for k in ("eac3", "e-ac-3", "dd+", "ddp")
        ):
            return True
        if t in ("ac3", "dd", "dolby digital") and any(
            k in combined for k in ("ac3", "dolby digital", "dd5", "dd2")
        ):
            return True
        if t == "dts" and "dts" in combined:
            return True
        if t in ("pcm", "lpcm") and any(k in combined for k in ("pcm", "lpcm")):
            return True
        if t and t in combined:
            return True
    return False


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
    video_quality_mode: str = "best",
    audio_quality_mode: str = "best",
    custom_config: dict[str, Any] | str | None = None,
    preset: Any = None,
) -> ScoreResult:
    """Calculate the score for a parsed release."""
    if preset is not None:
        if primary_language is None and getattr(preset, "primary_language", None):
            primary_language = preset.primary_language
        if fallback_language is None and getattr(preset, "fallback_language", None):
            fallback_language = preset.fallback_language
        if video_quality_mode == "best" and getattr(preset, "video_quality_mode", None):
            video_quality_mode = str(preset.video_quality_mode)
        if audio_quality_mode == "best" and getattr(preset, "audio_quality_mode", None):
            audio_quality_mode = str(preset.audio_quality_mode)
        if custom_config is None:
            custom_config = getattr(preset, "custom_config", None) or getattr(
                preset, "custom_config_json", None
            )

    resolved_custom: dict[str, Any] = {}
    if isinstance(custom_config, dict):
        resolved_custom = custom_config
    elif isinstance(custom_config, str) and custom_config.strip():
        try:
            loaded = json.loads(custom_config)
            if isinstance(loaded, dict):
                resolved_custom = loaded
        except Exception:
            resolved_custom = {}

    v_mode = str(video_quality_mode or "best").strip().lower()
    a_mode = str(audio_quality_mode or "best").strip().lower()

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

    # 1.5 Apply Custom Preset / Push Modal Constraints ("Best" vs "Custom")
    if v_mode == "custom" and resolved_custom:
        allowed_resolutions = _normalize_str_list(resolved_custom.get("resolutions"))
        if allowed_resolutions and not _matches_custom_resolution(
            allowed_resolutions, parsed.resolution, orig_lower
        ):
            return ScoreResult(
                0,
                True,
                f"Custom resolution filter rejected '{parsed.resolution}' (allowed: {allowed_resolutions})",
                None,
                is_primary=is_primary,
                is_fallback=is_fallback,
                matched_language=matched_language,
            )

        allowed_sources = _normalize_str_list(resolved_custom.get("sources"))
        if allowed_sources and not _matches_custom_source(
            allowed_sources, parsed.source, orig_lower
        ):
            return ScoreResult(
                0,
                True,
                f"Custom source filter rejected '{parsed.source}' (allowed: {allowed_sources})",
                None,
                is_primary=is_primary,
                is_fallback=is_fallback,
                matched_language=matched_language,
            )

        allowed_codecs = _normalize_str_list(resolved_custom.get("video_codecs"))
        if allowed_codecs and not _matches_custom_video_codec(
            allowed_codecs, parsed.video_codec, parsed.hdr, orig_lower
        ):
            return ScoreResult(
                0,
                True,
                f"Custom video codec/HDR filter rejected '{parsed.video_codec}' (allowed: {allowed_codecs})",
                None,
                is_primary=is_primary,
                is_fallback=is_fallback,
                matched_language=matched_language,
            )

        raw_min_gb = resolved_custom.get("min_size_gb")
        if raw_min_gb is not None and raw_min_gb != "":
            try:
                custom_min_gb = float(raw_min_gb)
                if custom_min_gb > 0 and size_gb < custom_min_gb:
                    return ScoreResult(
                        0,
                        True,
                        f"Below custom min size: {size_gb:.2f}GB < {custom_min_gb:.2f}GB",
                        None,
                        is_primary=is_primary,
                        is_fallback=is_fallback,
                        matched_language=matched_language,
                    )
            except (TypeError, ValueError):
                pass

        raw_max_gb = resolved_custom.get("max_size_gb")
        if raw_max_gb is not None and raw_max_gb != "":
            try:
                custom_max_gb = float(raw_max_gb)
                if custom_max_gb > 0 and size_gb > custom_max_gb:
                    return ScoreResult(
                        0,
                        True,
                        f"Exceeds custom max size: {size_gb:.2f}GB > {custom_max_gb:.2f}GB",
                        None,
                        is_primary=is_primary,
                        is_fallback=is_fallback,
                        matched_language=matched_language,
                    )
            except (TypeError, ValueError):
                pass

        req_keywords = _normalize_str_list(resolved_custom.get("required_keywords"))
        if req_keywords:
            missing_kws = [kw for kw in req_keywords if kw not in orig_lower]
            if missing_kws:
                return ScoreResult(
                    0,
                    True,
                    f"Missing required keyword(s): {missing_kws}",
                    None,
                    is_primary=is_primary,
                    is_fallback=is_fallback,
                    matched_language=matched_language,
                )

        excl_keywords = _normalize_str_list(resolved_custom.get("excluded_keywords"))
        if excl_keywords:
            matched_excl = [kw for kw in excl_keywords if kw in orig_lower]
            if matched_excl:
                return ScoreResult(
                    0,
                    True,
                    f"Matched excluded keyword(s): {matched_excl}",
                    None,
                    is_primary=is_primary,
                    is_fallback=is_fallback,
                    matched_language=matched_language,
                )

    if a_mode == "custom" and resolved_custom:
        allowed_audio = _normalize_str_list(resolved_custom.get("audio_formats"))
        if allowed_audio and not _matches_custom_audio_format(
            allowed_audio, parsed.audio_codec, parsed.audio_channels, orig_lower
        ):
            return ScoreResult(
                0,
                True,
                f"Custom audio format filter rejected '{parsed.audio_codec}' (allowed: {allowed_audio})",
                None,
                is_primary=is_primary,
                is_fallback=is_fallback,
                matched_language=matched_language,
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
