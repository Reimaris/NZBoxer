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
    "gold": "bg-amber-400/20 text-amber-300 border-amber-400/40",
    "silver": "bg-slate-300/20 text-slate-200 border-slate-300/40",
    "bronze": "bg-orange-600/20 text-orange-300 border-orange-500/40",
    "gray": "bg-zinc-800/80 text-zinc-400 border-zinc-700/60",
    "red": "bg-red-500/20 text-red-300 border-red-500/40",
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
    has_atmos: bool = False
    is_proper_or_repack: bool = False


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

    # 3. Compound Source & Remux / CAM / TS / BDRip vs BluRay Normalization
    raw_source = _get_str("source") or ""
    has_remux = ("remux" in other_list) or bool(
        re.search(r"(?:^|[\.\s_\-\[])remux(?:$|[\.\s_\-\]])", orig_lower)
    )
    has_bdrip = bool(
        re.search(r"(?:^|[\.\s_\-\[])(bdrip|brrip)(?:$|[\.\s_\-\]])", orig_lower)
    )
    has_bluray = any(b in raw_source for b in ("blu-ray", "bluray")) or bool(
        re.search(
            r"(?:^|[\.\s_\-\[])(bluray|blu-ray|bdremux)(?:$|[\.\s_\-\]])",
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

    # Strip trailing .ts file extension and dts audio tokens before checking CAM/TS
    orig_no_ts_ext = re.sub(r"\.ts$", "", orig_lower)
    orig_no_dts = re.sub(
        r"(?:^|[\.\s_\-\[])dts(?:[\.\s_\-:]?(?:hd|x|ma|hra))*", " ", orig_no_ts_ext
    )
    cam_ts_match = re.search(
        r"(?:^|[\.\s_\-\[])(cam|camrip|hdcam|ts|telesync|hdts|pdvd)(?:$|[\.\s_\-\]])",
        orig_no_dts,
    )

    if cam_ts_match or any(k in raw_source for k in ("cam", "telesync", "hd-telesync")):
        token = cam_ts_match.group(1) if cam_ts_match else raw_source
        if any(c in token for c in ("cam", "camrip", "hdcam")):
            source: str | None = "cam"
        else:
            source = "ts"
    elif has_remux:
        source = "bluray remux"
    elif has_bdrip:
        source = "bdrip"
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
        or bool(
            re.search(
                r"(?:^|[\.\s_\-\[])(hdtv|pdtv|dsr|tvrip|satrip)(?:$|[\.\s_\-\]])",
                orig_lower,
            )
        )
    ):
        source = "hdtv"
    elif "dvd" in raw_source or bool(
        re.search(r"(?:^|[\.\s_\-\[])(dvd|dvdrip|dvdr)(?:$|[\.\s_\-\]])", orig_lower)
    ):
        source = "dvd"
    else:
        source = raw_source or None

    # 4. Multi-Tag HDR / Dolby Vision / HLG & Color Depth
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
    elif any(x in ("hdr",) for x in other_list) or bool(
        re.search(r"(?:^|[\.\s_\-\[])hdr(?:$|[\.\s_\-\]])", orig_lower)
    ):
        hdr_formats.append("HDR")

    if any(x == "hlg" for x in other_list) or bool(
        re.search(r"(?:^|[\.\s_\-\[])hlg(?:$|[\.\s_\-\]])", orig_lower)
    ):
        hdr_formats.append("HLG")

    if "DV" in hdr_formats:
        hdr: str | None = "dolby vision"
    elif "HDR10+" in hdr_formats:
        hdr = "hdr10+"
    elif "HDR10" in hdr_formats:
        hdr = "hdr10"
    elif "HDR" in hdr_formats:
        hdr = "hdr"
    elif "HLG" in hdr_formats:
        hdr = "hlg"
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

    has_dtshd_ma = bool(
        re.search(
            r"(?:^|[\.\s_\-\[])(?:dts[\s\._-]?hd[\s\._-]?ma|dtshdma)(?:$|[\.\s_\-\]])",
            orig_lower,
        )
    ) or (
        ("dts-hd" in ac_joined or "dts" in ac_joined)
        and any("master audio" in x or x == "ma" for x in ap_list)
    )
    has_dtshd_hra = (
        bool(
            re.search(
                r"(?:^|[\.\s_\-\[])(?:dts[\s\._-]?hd(?:[\s\._-]?hra)?|dtshdhra|dtshd)(?:$|[\.\s_\-\]])",
                orig_lower,
            )
        )
        or "dts-hd" in ac_joined
        or (
            "dts" in ac_joined
            and any("high resolution" in x or x == "hra" for x in ap_list)
        )
    )

    if "truehd" in ac_joined or bool(
        re.search(r"(?:^|[\.\s_\-\[])truehd(?:$|[\.\s_\-\]])", orig_lower)
    ):
        audio_codec: str | None = "truehd atmos" if has_atmos else "truehd"
    elif "dts:x" in ac_joined or bool(
        re.search(r"(?:^|[\.\s_\-\[])dts[\s\.:_-]?x(?:$|[\.\s_\-\]])", orig_lower)
    ):
        audio_codec = "dts:x"
    elif has_dtshd_ma:
        audio_codec = "dts-hd ma"
    elif has_dtshd_hra:
        audio_codec = "dts-hd hra"
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
    elif "wma" in ac_joined or bool(
        re.search(r"(?:^|[\.\s_\-\[])wma(?:2\.0|5\.1)?(?:$|[\.\s_\-\]])", orig_lower)
    ):
        audio_codec = "wma"
    else:
        audio_codec = _get_str("audio_codec")

    audio_channels = _get_str("audio_channels")
    if not audio_channels:
        ch_match = re.search(
            r"(?:^|[\.\s_\-\[]|(?:ddp|dd\+|dd|eac3|ac3|aac|dts|truehd|flac|wma))(7\.1|5\.1|2\.0|1\.0)(?:$|[\.\s_\-\]])",
            orig_lower,
        )
        if ch_match:
            audio_channels = ch_match.group(1)

    # 6. Video Codec Normalization (including Legacy codecs)
    video_codec = _get_str("video_codec")
    if re.search(r"(?:^|[\.\s_\-\[])(mpeg-?2)(?:$|[\.\s_\-\]])", orig_lower):
        video_codec = "mpeg-2"
    elif re.search(r"(?:^|[\.\s_\-\[])(xvid)(?:$|[\.\s_\-\]])", orig_lower):
        video_codec = "xvid"
    elif re.search(r"(?:^|[\.\s_\-\[])(divx)(?:$|[\.\s_\-\]])", orig_lower):
        video_codec = "divx"
    elif re.search(r"(?:^|[\.\s_\-\[])(vc-?1)(?:$|[\.\s_\-\]])", orig_lower):
        video_codec = "vc-1"
    elif not video_codec:
        if re.search(r"(?:^|[\.\s_\-\[])av1(?:$|[\.\s_\-\]])", orig_lower):
            video_codec = "av1"
        elif re.search(
            r"(?:^|[\.\s_\-\[])(hevc|x265|h\.?265)(?:$|[\.\s_\-\]])", orig_lower
        ):
            video_codec = "h.265"
        elif re.search(
            r"(?:^|[\.\s_\-\[])(avc|x264|h\.?264)(?:$|[\.\s_\-\]])", orig_lower
        ):
            video_codec = "h.264"
    elif video_codec in ("mpeg2", "mpeg-2"):
        video_codec = "mpeg-2"
    elif video_codec in ("vc1", "vc-1"):
        video_codec = "vc-1"

    # 7. PROPER / REPACK / RERIP Detection
    is_proper_or_repack = any(
        x in ("proper", "repack", "rerip") for x in other_list
    ) or bool(
        re.search(
            r"(?:^|[\.\s_\-\[])(proper|repack|rerip)(?:$|[\.\s_\-\]])", orig_lower
        )
    )

    return ParsedRelease(
        original_title=release_name,
        title=guess.get("title"),
        year=year,
        resolution=_get_str("screen_size"),
        video_codec=video_codec,
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
        has_atmos=has_atmos,
        is_proper_or_repack=is_proper_or_repack,
    )


def _make_pill(category: str, label: str, tier: str) -> dict[str, str]:
    return {
        "category": category,
        "label": label,
        "tier": tier,
        "css": TIER_CSS_MAP.get(tier, TIER_CSS_MAP["gray"]),
    }


def _get_bitrate_pill_tier(norm_mbps: float, res_low: str) -> str:
    """Determine the 5-tier metallic color key for a normalized video bitrate at a given resolution."""
    if res_low in ("2160p", "4k", "uhd"):
        if norm_mbps >= 45.0:
            return "gold"
        if norm_mbps >= 25.0:
            return "silver"
        if norm_mbps >= 14.0:
            return "bronze"
        if norm_mbps >= 8.0:
            return "gray"
        return "red"
    if res_low == "1440p":
        if norm_mbps >= 30.0:
            return "gold"
        if norm_mbps >= 18.0:
            return "silver"
        if norm_mbps >= 10.0:
            return "bronze"
        if norm_mbps >= 5.0:
            return "gray"
        return "red"
    if res_low == "1080p":
        if norm_mbps >= 18.0:
            return "gold"
        if norm_mbps >= 10.0:
            return "silver"
        if norm_mbps >= 5.0:
            return "bronze"
        if norm_mbps >= 2.5:
            return "gray"
        return "red"
    if res_low == "720p":
        if norm_mbps >= 9.0:
            return "gold"
        if norm_mbps >= 5.0:
            return "silver"
        if norm_mbps >= 2.5:
            return "bronze"
        if norm_mbps >= 1.2:
            return "gray"
        return "red"
    if res_low in ("480p", "576p", "sd"):
        if norm_mbps >= 2.5:
            return "silver"
        if norm_mbps >= 1.2:
            return "bronze"
        if norm_mbps >= 0.6:
            return "gray"
        return "red"
    return "gray"


def build_release_feature_pills(
    release_name: str | ParsedRelease | None = None,
    size_bytes: int | None = 0,
    *,
    parsed: ParsedRelease | None = None,
    release_title: str | None = None,
    score: float | int | None = None,
    matched_language: str | None = None,
    api_language: str | None = None,
    is_fallback: bool = False,
    is_mismatch: bool = False,
    bitrate_mbps: float | None = None,
    normalized_bitrate_mbps: float | None = None,
    runtime_minutes: int | None = None,
    media_type: str | None = None,
    season_episode_count: int | None = None,
) -> list[dict[str, str]]:
    """Construct an ordered list of up to 12 atomic 5-Tier Metallic & Semantic Feature Pills."""
    if isinstance(release_name, ParsedRelease):
        parsed = release_name
    elif parsed is None:
        title_to_parse = release_title or (
            release_name if isinstance(release_name, str) else None
        )
        if title_to_parse:
            parsed = parse_release_name(title_to_parse)

    pills: list[dict[str, str]] = []

    # 1. [Score] (category="score")
    if score is not None:
        try:
            score_int = int(round(float(score)))
        except (TypeError, ValueError):
            score_int = None
        if score_int is not None:
            if score_int >= 4000:
                s_tier = "gold"
            elif score_int >= 2800:
                s_tier = "silver"
            elif score_int >= 1500:
                s_tier = "bronze"
            elif score_int >= 500:
                s_tier = "gray"
            else:
                s_tier = "red"
            pills.append(_make_pill("score", f"Score: {score_int}", s_tier))

    # 2. [Language] (category="language")
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
    if is_dl:
        if not lang_codes or lang_codes == ["EN"]:
            lang_codes = ["DE", "EN"]
        elif "EN" not in lang_codes:
            lang_codes.append("EN")
    elif not lang_codes:
        lang_codes.append("EN")

    lang_joined = " + ".join(lang_codes)
    lang_label = f"{lang_joined} (DL)" if is_dl else lang_joined

    if is_mismatch:
        l_tier = "red"
    elif is_fallback:
        l_tier = "silver"
    else:
        l_tier = "gold"
    pills.append(_make_pill("language", lang_label, l_tier))

    # 3. [Resolution] (category="resolution")
    res_low = (parsed.resolution or "").lower() if parsed else ""
    if parsed and parsed.resolution:
        if res_low in ("2160p", "4k", "uhd"):
            pills.append(_make_pill("resolution", "4K", "gold"))
        elif res_low == "1440p":
            pills.append(_make_pill("resolution", "1440p", "silver"))
        elif res_low == "1080p":
            pills.append(_make_pill("resolution", "1080p", "silver"))
        elif res_low == "720p":
            pills.append(_make_pill("resolution", "720p", "bronze"))
        else:
            res_label = "SD" if res_low == "sd" else parsed.resolution
            pills.append(_make_pill("resolution", res_label, "gray"))

    # 4. [Source] (category="source")
    if parsed and parsed.source:
        src_low = parsed.source.lower()
        if "remux" in src_low:
            pills.append(_make_pill("source", "BluRay Remux", "gold"))
        elif "uhd" in src_low or "ultra hd" in src_low:
            pills.append(_make_pill("source", "UHD BluRay", "silver"))
        elif src_low in ("bdrip", "brrip"):
            pills.append(_make_pill("source", "BDRip", "bronze"))
        elif "blu" in src_low:
            pills.append(_make_pill("source", "BluRay", "silver"))
        elif src_low in ("web-dl", "webdl", "web"):
            pills.append(_make_pill("source", "WEB-DL", "silver"))
        elif "webrip" in src_low or "web-rip" in src_low:
            pills.append(_make_pill("source", "WEBRip", "bronze"))
        elif src_low in ("hdtv", "pdtv"):
            pills.append(_make_pill("source", "HDTV", "bronze"))
        elif "dvd" in src_low:
            pills.append(_make_pill("source", "DVD", "gray"))
        elif src_low in ("cam", "ts", "telesync", "hdts", "hdcam"):
            pills.append(_make_pill("source", "CAM/TS", "red"))
        else:
            pills.append(_make_pill("source", parsed.source.upper(), "gray"))

    # 5. [Bitrate] (category="bitrate")
    if (
        size_bytes is not None
        and size_bytes > 0
        and (bitrate_mbps is None or normalized_bitrate_mbps is None)
        and parsed is not None
    ):
        from app.core.scorer import (
            calculate_net_video_bitrate_mbps,
            resolve_duration_seconds,
        )

        duration_sec = resolve_duration_seconds(
            runtime_minutes=runtime_minutes,
            media_type=media_type,
            expected_season=parsed.season,
            expected_episode=parsed.episode,
            season_episode_count=season_episode_count,
        )
        raw_b, norm_b = calculate_net_video_bitrate_mbps(
            size_bytes=size_bytes,
            duration_seconds=duration_sec,
            audio_codec=parsed.audio_codec,
            video_codec=parsed.video_codec,
        )
        if bitrate_mbps is None:
            bitrate_mbps = raw_b
        if normalized_bitrate_mbps is None:
            normalized_bitrate_mbps = norm_b

    if bitrate_mbps is not None and bitrate_mbps > 0:
        if bitrate_mbps < 10.0:
            b_label = f"~{bitrate_mbps:.1f} Mbps"
        else:
            b_label = f"~{round(bitrate_mbps):.0f} Mbps"
        norm_for_tier = (
            normalized_bitrate_mbps
            if normalized_bitrate_mbps is not None
            else bitrate_mbps
        )
        b_tier = _get_bitrate_pill_tier(norm_for_tier, res_low)
        pills.append(_make_pill("bitrate", b_label, b_tier))

    # 6. [HDR] (category="hdr") — De-duplicated single slot
    if parsed:
        hdr_list = parsed.hdr_formats or []
        if "DV" in hdr_list:
            pills.append(_make_pill("hdr", "DV", "gold"))
        elif "HDR10+" in hdr_list:
            pills.append(_make_pill("hdr", "HDR10+", "gold"))
        elif "HDR10" in hdr_list:
            pills.append(_make_pill("hdr", "HDR10", "silver"))
        elif "HDR" in hdr_list:
            pills.append(_make_pill("hdr", "HDR", "silver"))
        elif "HLG" in hdr_list:
            pills.append(_make_pill("hdr", "HLG", "silver"))
        elif not hdr_list and (
            parsed.color_depth in ("10-bit", "10bit")
            or (parsed.hdr and parsed.hdr.lower() in ("10-bit", "10bit"))
        ):
            pills.append(_make_pill("hdr", "10bit", "bronze"))

    # 7. [Audio Codec] (category="audio_codec")
    ac_low = (parsed.audio_codec or "").lower() if parsed else ""
    if parsed and parsed.audio_codec:
        if "truehd" in ac_low:
            pills.append(_make_pill("audio_codec", "TrueHD", "gold"))
        elif "dts:x" in ac_low or "dtsx" in ac_low:
            pills.append(_make_pill("audio_codec", "DTS:X", "gold"))
        elif "dts-hd ma" in ac_low:
            pills.append(_make_pill("audio_codec", "DTS-HD MA", "gold"))
        elif "flac" in ac_low:
            pills.append(_make_pill("audio_codec", "FLAC", "gold"))
        elif "lpcm" in ac_low or "pcm" in ac_low:
            pills.append(_make_pill("audio_codec", "LPCM", "gold"))
        elif "dts-hd hra" in ac_low or "dts-hd" in ac_low:
            pills.append(_make_pill("audio_codec", "DTS-HD HRA", "silver"))
        elif "dts" in ac_low:
            pills.append(_make_pill("audio_codec", "DTS", "silver"))
        elif any(k in ac_low for k in ("eac3", "dd+", "dolby digital plus")):
            pills.append(_make_pill("audio_codec", "DD+", "silver"))
        elif "opus" in ac_low:
            pills.append(_make_pill("audio_codec", "OPUS", "silver"))
        elif "ac3" in ac_low or "dolby digital" in ac_low:
            pills.append(_make_pill("audio_codec", "DD", "bronze"))
        elif "aac" in ac_low:
            pills.append(_make_pill("audio_codec", "AAC", "bronze"))
        elif "mp3" in ac_low:
            pills.append(_make_pill("audio_codec", "MP3", "gray"))
        elif "wma" in ac_low:
            pills.append(_make_pill("audio_codec", "WMA", "gray"))
        else:
            pills.append(_make_pill("audio_codec", ac_low.upper(), "bronze"))

    # 8. [Atmos] (category="atmos")
    has_atmos = bool(parsed and (parsed.has_atmos or "atmos" in ac_low))
    if has_atmos:
        atmos_tier = "gold" if "truehd" in ac_low else "silver"
        pills.append(_make_pill("atmos", "Atmos", atmos_tier))

    # 9. [Channels] (category="audio_channels")
    if parsed and parsed.audio_channels:
        ch = parsed.audio_channels.strip()
        if ch in ("7.1", "5.1"):
            pills.append(_make_pill("audio_channels", ch, "silver"))
        elif ch in ("2.0", "1.0"):
            pills.append(_make_pill("audio_channels", ch, "gray"))

    # 10. [Video Codec] (category="video_codec")
    if parsed and parsed.video_codec:
        vc_low = parsed.video_codec.lower()
        if "av1" in vc_low:
            pills.append(_make_pill("video_codec", "AV1", "gray"))
        elif any(k in vc_low for k in ("hevc", "h.265", "h265", "x265")):
            pills.append(_make_pill("video_codec", "HEVC", "gray"))
        elif any(k in vc_low for k in ("h.264", "h264", "x264", "avc")):
            pills.append(_make_pill("video_codec", "AVC", "gray"))
        elif "mpeg-2" in vc_low or "mpeg2" in vc_low:
            pills.append(_make_pill("video_codec", "MPEG-2", "gray"))
        elif "xvid" in vc_low:
            pills.append(_make_pill("video_codec", "XviD", "gray"))
        elif "divx" in vc_low:
            pills.append(_make_pill("video_codec", "DivX", "gray"))
        elif "vc-1" in vc_low or "vc1" in vc_low:
            pills.append(_make_pill("video_codec", "VC-1", "gray"))
        else:
            pills.append(_make_pill("video_codec", parsed.video_codec.upper(), "gray"))

    # 11. [Size] (category="size")
    if size_bytes is not None and size_bytes > 0:
        gb = size_bytes / (1024**3)
        if gb >= 1.0:
            size_label = f"{gb:.1f} GB"
        else:
            mb = max(1, int(round(size_bytes / (1024**2))))
            size_label = f"{mb} MB"
        pills.append(_make_pill("size", size_label, "gray"))

    # 12. [Group] (category="group")
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
        pills.append(_make_pill("group", f"Grp: {grp_display}", "gray"))

    return pills
