"""
Release Name Parser & Quality-Tier Feature Pill Builder
=======================================================
Uses `guessit` plus scene token normalization to extract compound technical
metadata from raw NZB release names and construct 4-Tier Semantic Quality
Feature Pills (`build_release_feature_pills`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from guessit import guessit

logger = logging.getLogger(__name__)

TIER_CSS_MAP: dict[str, str] = {
    "ultra": "bg-emerald-500/20 text-emerald-300 border-emerald-500/30",
    "high": "bg-blue-500/20 text-blue-300 border-blue-500/30",
    "mid": "bg-amber-500/20 text-amber-300 border-amber-500/30",
    "neutral": "bg-zinc-700/40 text-zinc-300 border-zinc-600/40",
    "low": "bg-red-500/20 text-red-300 border-red-500/30",
}

_LANG_CODE_MAP: dict[str, str] = {
    "english": "EN",
    "eng": "EN",
    "en": "EN",
    "german": "DE",
    "deutsch": "DE",
    "ger": "DE",
    "de": "DE",
    "french": "FR",
    "fra": "FR",
    "fre": "FR",
    "vostfr": "FR",
    "truefrench": "FR",
    "vf": "FR",
    "fr": "FR",
    "japanese": "JA",
    "jpn": "JA",
    "jap": "JA",
    "ja": "JA",
    "spanish": "ES",
    "spa": "ES",
    "esp": "ES",
    "es": "ES",
    "italian": "IT",
    "ita": "IT",
    "it": "IT",
    "portuguese": "PT",
    "por": "PT",
    "pt": "PT",
    "russian": "RU",
    "rus": "RU",
    "ru": "RU",
}


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
    season: int | None
    episode: int | None
    is_dual_language: bool = False
    color_depth: str | None = None
    hdr_formats: list[str] = field(default_factory=list)


def parse_release_name(release_name: str) -> ParsedRelease:
    """Parse a raw NZB release name using guessit and compound scene token rules.

    Args:
        release_name: The raw string (e.g. 'Movie.2023.2160p.UHD.BluRay.x265.DTS-HD.MA.7.1-GROUP')

    Returns:
        ParsedRelease dataclass with extracted compound fields.
    """
    try:
        guess: dict[str, Any] = dict(guessit(release_name))
    except Exception as e:  # noqa: BLE001
        logger.error("Guessit failed to parse '%s': %s", release_name, e)
        guess = {}

    def _get_str(key: str) -> str | None:
        val = guess.get(key)
        if isinstance(val, list) and val:
            return str(val[0]).lower()
        return str(val).lower() if val else None

    def _get_list(key: str) -> list[str]:
        val = guess.get(key)
        if isinstance(val, list):
            return [str(x).lower() for x in val if x is not None]
        if val is not None:
            return [str(val).lower()]
        return []

    def _get_int(key: str) -> int | None:
        val = guess.get(key)
        if isinstance(val, list) and val:
            try:
                return int(val[0])
            except (ValueError, TypeError):
                return None
        if val is not None:
            try:
                return int(str(val))
            except (ValueError, TypeError):
                return None
        return None

    orig_lower = (release_name or "").lower()
    other_list = _get_list("other")

    # 1. Languages & Dual-Language (.DL. / Dual / Multi)
    languages = _get_list("language")
    explicit_lang_patterns = [
        (r"(?:^|[\.\s_\-\[])(german|ger)(?:$|[\.\s_\-\]])", "german"),
        (r"(?:^|[\.\s_\-\[])(english|eng)(?:$|[\.\s_\-\]])", "english"),
        (
            r"(?:^|[\.\s_\-\[])(french|fra|fre|vostfr|truefrench)(?:$|[\.\s_\-\]])",
            "french",
        ),
        (r"(?:^|[\.\s_\-\[])(japanese|jpn|jap)(?:$|[\.\s_\-\]])", "japanese"),
        (r"(?:^|[\.\s_\-\[])(italian|ita)(?:$|[\.\s_\-\]])", "italian"),
        (r"(?:^|[\.\s_\-\[])(spanish|spa|esp)(?:$|[\.\s_\-\]])", "spanish"),
    ]
    for pattern, canonical_lang in explicit_lang_patterns:
        if re.search(pattern, orig_lower):
            code = _LANG_CODE_MAP.get(canonical_lang, canonical_lang.upper())
            if not any(
                _LANG_CODE_MAP.get(existing, existing.upper()) == code
                for existing in languages
            ):
                languages.append(canonical_lang)

    orig_no_webdl = re.sub(r"web[\.\s_\-]?dl", "", orig_lower)
    is_dual_language = bool(
        re.search(r"(?:^|[\.\s_\-\[])(dl|dual|multi)(?:$|[\.\s_\-\]])", orig_no_webdl)
    ) or any(x in ("dual audio", "multi") for x in other_list)

    # 2. Year
    raw_year = guess.get("year")
    if isinstance(raw_year, list):
        year = int(raw_year[0]) if raw_year else None
    else:
        year = int(raw_year) if raw_year else None

    # 3. Compound Source & Remux Normalization
    raw_source = _get_str("source") or ""
    has_remux = ("remux" in other_list) or bool(
        re.search(r"(?:^|[\.\s_\-\[])remux(?:$|[\.\s_\-\]])", orig_lower)
    )
    has_bluray = any(b in raw_source for b in ("blu-ray", "bluray")) or bool(
        re.search(
            r"(?:^|[\.\s_\-\[])(bluray|blu-ray|bdrip|brrip|bdremux)(?:$|[\.\s_\-\]])",
            orig_lower,
        )
    )
    has_uhd = (
        "ultra hd blu-ray" in raw_source
        or "ultra hd" in other_list
        or bool(
            re.search(r"(?:^|[\.\s_\-\[])(uhd|2160p|4k)(?:$|[\.\s_\-\]])", orig_lower)
        )
    )

    if has_remux:
        source: str | None = "bluray remux"
    elif "ultra hd blu-ray" in raw_source or (has_bluray and has_uhd):
        source = "uhd bluray"
    elif has_bluray:
        source = "bluray"
    elif "web" in raw_source or bool(
        re.search(
            r"(?:^|[\.\s_\-\[])(web-dl|webdl|webrip|web-rip|web)(?:$|[\.\s_\-\]])",
            orig_lower,
        )
    ):
        if (
            bool(
                re.search(
                    r"(?:^|[\.\s_\-\[])(webrip|web-rip)(?:$|[\.\s_\-\]])", orig_lower
                )
            )
            or "rip" in other_list
        ):
            source = "webrip"
        else:
            source = "web-dl"
    elif (
        "hdtv" in raw_source
        or "pdtv" in raw_source
        or bool(re.search(r"(?:^|[\.\s_\-\[])(hdtv|pdtv)(?:$|[\.\s_\-\]])", orig_lower))
    ):
        source = "hdtv"
    elif "dvd" in raw_source or bool(
        re.search(r"(?:^|[\.\s_\-\[])(dvd|dvdrip|dvdr)(?:$|[\.\s_\-\]])", orig_lower)
    ):
        source = "dvd"
    else:
        source = raw_source or None

    # 4. Multi-Tag HDR / Dolby Vision & Color Depth
    raw_cd = _get_str("color_depth") or ""
    color_depth: str | None = None
    if (
        "10" in raw_cd
        or any("10-bit" in x or "10bit" in x for x in other_list)
        or bool(
            re.search(r"(?:^|[\.\s_\-\[])10[\.\s_-]?bit(?:$|[\.\s_\-\]])", orig_lower)
        )
    ):
        color_depth = "10-bit"

    hdr_formats: list[str] = []
    if any(x in ("dolby vision", "dovi", "dv") for x in other_list) or bool(
        re.search(
            r"(?:^|[\.\s_\-\[])(dv|dovi|dolby[\.\s_-]?vision)(?:$|[\.\s_\-\]])",
            orig_lower,
        )
    ):
        hdr_formats.append("DV")

    if any(x in ("hdr10+", "hdr10plus") for x in other_list) or bool(
        re.search(r"(?:^|[\.\s_\-\[])hdr10(?:\+|plus)(?:$|[\.\s_\-\]])", orig_lower)
    ):
        hdr_formats.append("HDR10+")
    elif bool(re.search(r"(?:^|[\.\s_\-\[])hdr10(?:$|[\.\s_\-\]])", orig_lower)):
        hdr_formats.append("HDR10")
    elif any(x in ("hdr", "hdr10", "hlg") for x in other_list) or bool(
        re.search(r"(?:^|[\.\s_\-\[])(hdr|hlg)(?:$|[\.\s_\-\]])", orig_lower)
    ):
        hdr_formats.append("HDR")

    if "DV" in hdr_formats:
        hdr: str | None = "dolby vision"
    elif "HDR10+" in hdr_formats:
        hdr = "hdr10+"
    elif "HDR10" in hdr_formats:
        hdr = "hdr10"
    elif "HDR" in hdr_formats:
        hdr = "hdr"
    elif color_depth == "10-bit":
        hdr = "10-bit"
    else:
        hdr = None

    # 5. Audio Codec & Atmos / Channels Normalization
    ac_list = _get_list("audio_codec")
    ac_joined = " ".join(ac_list)
    ap_list = _get_list("audio_profile")
    has_atmos = (
        ("atmos" in other_list)
        or any("atmos" in x for x in ap_list)
        or any("atmos" in x for x in ac_list)
        or bool(re.search(r"(?:^|[\.\s_\-\[])atmos(?:$|[\.\s_\-\]])", orig_lower))
    )

    if "truehd" in ac_joined or bool(
        re.search(r"(?:^|[\.\s_\-\[])truehd(?:$|[\.\s_\-\]])", orig_lower)
    ):
        audio_codec: str | None = "truehd atmos" if has_atmos else "truehd"
    elif "dts:x" in ac_joined or bool(
        re.search(r"(?:^|[\.\s_\-\[])dts[\s\.:_-]?x(?:$|[\.\s_\-\]])", orig_lower)
    ):
        audio_codec = "dts:x"
    elif (
        "dts-hd" in ac_joined
        or bool(
            re.search(
                r"(?:^|[\.\s_\-\[])dts[\s\._-]?hd(?:[\s\._-]?ma)?(?:$|[\.\s_\-\]])",
                orig_lower,
            )
        )
        or (
            "dts" in ac_joined
            and any("master audio" in x or x == "ma" for x in ap_list)
        )
    ):
        audio_codec = "dts-hd ma"
    elif any(k in ac_joined for k in ("dolby digital plus", "eac3", "e-ac-3")) or bool(
        re.search(
            r"(?:^|[\.\s_\-\[])(ddp|dd\+|eac3|e-ac-3)(?:7\.1|5\.1|2\.0)?(?:$|[\.\s_\-\]])",
            orig_lower,
        )
    ):
        audio_codec = "eac3 atmos" if has_atmos else "eac3"
    elif any(k in ac_joined for k in ("pcm", "lpcm")) or bool(
        re.search(r"(?:^|[\.\s_\-\[])(lpcm|pcm)(?:$|[\.\s_\-\]])", orig_lower)
    ):
        audio_codec = "lpcm"
    elif "flac" in ac_joined or bool(
        re.search(
            r"(?:^|[\.\s_\-\[])flac(?:2\.0|5\.1|7\.1)?(?:$|[\.\s_\-\]])", orig_lower
        )
    ):
        audio_codec = "flac"
    elif "dts" in ac_joined or bool(
        re.search(r"(?:^|[\.\s_\-\[])dts(?:$|[\.\s_\-\]])", orig_lower)
    ):
        audio_codec = "dts"
    elif any(k in ac_joined for k in ("dolby digital", "ac3", "ac-3")) or bool(
        re.search(
            r"(?:^|[\.\s_\-\[])(ac3|dd)(?:5\.1|2\.0)?(?:$|[\.\s_\-\]])", orig_lower
        )
    ):
        audio_codec = "ac3"
    elif "aac" in ac_joined or bool(
        re.search(r"(?:^|[\.\s_\-\[])aac(?:2\.0|5\.1)?(?:$|[\.\s_\-\]])", orig_lower)
    ):
        audio_codec = "aac"
    elif "opus" in ac_joined or bool(
        re.search(r"(?:^|[\.\s_\-\[])opus(?:2\.0|5\.1)?(?:$|[\.\s_\-\]])", orig_lower)
    ):
        audio_codec = "opus"
    elif "mp3" in ac_joined or bool(
        re.search(r"(?:^|[\.\s_\-\[])mp3(?:$|[\.\s_\-\]])", orig_lower)
    ):
        audio_codec = "mp3"
    else:
        audio_codec = _get_str("audio_codec")

    audio_channels = _get_str("audio_channels")
    if not audio_channels:
        ch_match = re.search(
            r"(?:^|[\.\s_\-\[]|(?:ddp|dd\+|dd|eac3|ac3|aac|dts|truehd|flac))(7\.1|5\.1|2\.0|1\.0)(?:$|[\.\s_\-\]])",
            orig_lower,
        )
        if ch_match:
            audio_channels = ch_match.group(1)

    return ParsedRelease(
        original_title=release_name,
        title=guess.get("title"),
        year=year,
        resolution=_get_str("screen_size"),
        video_codec=_get_str("video_codec"),
        audio_codec=audio_codec,
        audio_channels=audio_channels,
        source=source,
        release_group=_get_str("release_group"),
        hdr=hdr,
        languages=languages,
        season=_get_int("season"),
        episode=_get_int("episode"),
        is_dual_language=is_dual_language,
        color_depth=color_depth,
        hdr_formats=hdr_formats,
    )


def _make_pill(category: str, label: str, tier: str) -> dict[str, str]:
    return {
        "category": category,
        "label": label,
        "tier": tier,
        "css": TIER_CSS_MAP.get(tier, TIER_CSS_MAP["neutral"]),
    }


def build_release_feature_pills(
    parsed: ParsedRelease | None = None,
    *,
    release_title: str | None = None,
    score: float | int | None = None,
    size_bytes: int | None = None,
    is_fallback: bool = False,
    is_mismatch: bool = False,
    matched_language: str | None = None,
    api_language: str | None = None,
) -> list[dict[str, str]]:
    """Construct an ordered list of 4-Tier Semantic Quality Feature Pills for a release.

    Order: [score, language, resolution, source, video_codec, hdr, audio, size, group]
    """
    if parsed is None and release_title:
        parsed = parse_release_name(release_title)

    pills: list[dict[str, str]] = []

    # 1. Score Pill
    if score is not None:
        try:
            score_int = int(round(float(score)))
        except (TypeError, ValueError):
            score_int = None
        if score_int is not None:
            if score_int >= 5000:
                s_tier = "ultra"
            elif score_int >= 3000:
                s_tier = "high"
            elif score_int >= 1500:
                s_tier = "mid"
            else:
                s_tier = "low"
            pills.append(_make_pill("score", f"Score: {score_int}", s_tier))

    # 2. Language Pill
    lang_codes: list[str] = []
    if parsed and parsed.languages:
        for raw_l in parsed.languages:
            code = _LANG_CODE_MAP.get(raw_l.strip().lower(), raw_l.strip().upper())
            if code and code not in lang_codes:
                lang_codes.append(code)
    if api_language:
        for part in str(api_language).replace("/", ",").split(","):
            clean_p = part.strip().lower()
            if clean_p:
                code = _LANG_CODE_MAP.get(clean_p, clean_p.upper())
                if code and code not in lang_codes:
                    lang_codes.append(code)
    if matched_language and not lang_codes:
        m_clean = str(matched_language).strip().lower()
        if m_clean and m_clean not in ("any", "none"):
            code = _LANG_CODE_MAP.get(m_clean, m_clean.upper())
            if code not in lang_codes:
                lang_codes.append(code)

    is_dl = bool(parsed and parsed.is_dual_language)
    if is_dl and "EN" not in lang_codes:
        lang_codes.append("EN")
    if not lang_codes:
        lang_codes.append("EN")

    lang_joined = " + ".join(lang_codes)
    if is_dl:
        lang_label = f"{lang_joined} (DL)"
    else:
        lang_label = lang_joined

    if is_mismatch:
        l_tier = "low"
    elif is_fallback:
        l_tier = "mid"
    else:
        l_tier = "ultra"
    pills.append(_make_pill("language", lang_label, l_tier))

    # 3. Resolution Pill
    if parsed and parsed.resolution:
        res_low = parsed.resolution.lower()
        if res_low in ("2160p", "4k", "uhd"):
            pills.append(_make_pill("resolution", "4K", "ultra"))
        elif res_low in ("1440p", "1080p"):
            pills.append(_make_pill("resolution", res_low, "high"))
        elif res_low == "720p":
            pills.append(_make_pill("resolution", "720p", "mid"))
        else:
            pills.append(_make_pill("resolution", parsed.resolution, "low"))

    # 4. Source Pill
    if parsed and parsed.source:
        src_low = parsed.source.lower()
        if "remux" in src_low:
            pills.append(_make_pill("source", "BluRay Remux", "ultra"))
        elif "uhd" in src_low or "ultra hd" in src_low:
            pills.append(_make_pill("source", "UHD BluRay", "high"))
        elif "blu" in src_low or "bdrip" in src_low or "brrip" in src_low:
            pills.append(_make_pill("source", "BluRay", "high"))
        elif src_low in ("web-dl", "webdl", "web"):
            pills.append(_make_pill("source", "WEB-DL", "high"))
        elif "webrip" in src_low or "web-rip" in src_low:
            pills.append(_make_pill("source", "WEBRip", "mid"))
        elif src_low in ("hdtv", "pdtv"):
            pills.append(_make_pill("source", "HDTV", "mid"))
        elif any(k in src_low for k in ("dvd", "cam", "ts", "telesync")):
            pills.append(_make_pill("source", src_low.upper(), "low"))
        else:
            pills.append(_make_pill("source", parsed.source.upper(), "neutral"))

    # 5. Video Codec Pill
    if parsed and parsed.video_codec:
        vc_low = parsed.video_codec.lower()
        if "av1" in vc_low:
            pills.append(_make_pill("video_codec", "AV1", "ultra"))
        elif any(k in vc_low for k in ("hevc", "h.265", "h265", "x265")):
            pills.append(_make_pill("video_codec", "HEVC", "ultra"))
        elif any(k in vc_low for k in ("h.264", "h264", "x264", "avc")):
            pills.append(_make_pill("video_codec", "H.264", "mid"))
        else:
            pills.append(
                _make_pill("video_codec", parsed.video_codec.upper(), "neutral")
            )

    # 6. HDR / Dynamic Range Pill
    if parsed:
        if parsed.hdr_formats:
            hdr_label = " + ".join(parsed.hdr_formats)
            hdr_tier = (
                "ultra"
                if any(h in parsed.hdr_formats for h in ("DV", "HDR10+"))
                else "high"
            )
            pills.append(_make_pill("hdr", hdr_label, hdr_tier))
        elif parsed.color_depth == "10-bit" or (parsed.hdr and "10" in parsed.hdr):
            pills.append(_make_pill("hdr", "10-bit", "mid"))

    # 7. Audio + Channels Pill
    if parsed and (parsed.audio_codec or parsed.audio_channels):
        ac_low = (parsed.audio_codec or "").lower()
        ch = parsed.audio_channels or ""
        ch_part = f" {ch}" if ch else ""

        if "truehd" in ac_low:
            a_label = (
                f"TrueHD{ch_part} Atmos" if "atmos" in ac_low else f"TrueHD{ch_part}"
            )
            a_tier = "ultra"
        elif "dts:x" in ac_low or "dtsx" in ac_low:
            a_label = f"DTS:X{ch_part}"
            a_tier = "ultra"
        elif "dts-hd" in ac_low:
            a_label = f"DTS-HD MA{ch_part}"
            a_tier = "ultra"
        elif "lpcm" in ac_low or "pcm" in ac_low:
            a_label = f"LPCM{ch_part}"
            a_tier = "ultra"
        elif "flac" in ac_low:
            a_label = f"FLAC{ch_part}"
            a_tier = "ultra"
        elif any(k in ac_low for k in ("eac3", "dd+", "dolby digital plus")):
            a_label = f"DD+{ch_part} Atmos" if "atmos" in ac_low else f"DD+{ch_part}"
            a_tier = "high"
        elif "dts" in ac_low:
            a_label = f"DTS{ch_part}"
            a_tier = "high"
        elif "ac3" in ac_low or "dolby digital" in ac_low:
            a_label = f"AC3{ch_part}"
            a_tier = "high"
        elif "aac" in ac_low:
            a_label = f"AAC{ch_part}"
            a_tier = "mid"
        elif "opus" in ac_low:
            a_label = f"Opus{ch_part}"
            a_tier = "mid"
        elif "mp3" in ac_low:
            a_label = f"MP3{ch_part}"
            a_tier = "mid"
        elif ac_low:
            a_label = f"{ac_low.upper()}{ch_part}"
            a_tier = "mid"
        else:
            a_label = ch
            a_tier = "mid"

        if a_label:
            pills.append(_make_pill("audio", a_label, a_tier))

    # 8. Size Pill
    if size_bytes is not None and size_bytes > 0:
        gb = size_bytes / (1024**3)
        if gb >= 1.0:
            size_label = f"{gb:.1f} GB"
        else:
            mb = max(1, int(round(size_bytes / (1024**2))))
            size_label = f"{mb} MB"
        pills.append(_make_pill("size", size_label, "neutral"))

    # 9. Release Group Pill
    if parsed and parsed.release_group:
        grp_display = parsed.release_group
        if parsed.original_title:
            m = re.search(
                re.escape(parsed.release_group),
                parsed.original_title,
                re.IGNORECASE,
            )
            if m:
                grp_display = m.group(0)
        pills.append(_make_pill("group", f"Grp: {grp_display}", "neutral"))

    return pills
