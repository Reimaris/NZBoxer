"""Unit tests for the scoring engine."""

import pytest

from app.core.default_scoring import DEFAULT_SCORING_CONFIG
from app.core.parser import TIER_CSS_MAP, ParsedRelease, parse_release_name
from app.core.scorer import (
    calculate_bitrate_mbps,
    calculate_dynamic_bitrate_score,
    get_audio_bitrate_offset,
    get_codec_efficiency_factor,
    resolve_duration_seconds,
    score_release,
)


def test_calculate_bitrate_mbps():
    # 5GB file, 100 minutes
    # 5GB = 5,368,709,120 bytes
    size = 5 * 1024 * 1024 * 1024
    mbps = calculate_bitrate_mbps(size, 100)
    # (5,368,709,120 * 8) / 1,000,000 / 6000 = ~7.15 Mbps
    assert mbps is not None
    assert 7.1 < mbps < 7.2


def test_calculate_bitrate_mbps_zero_runtime():
    assert calculate_bitrate_mbps(1000, 0) is None
    assert calculate_bitrate_mbps(0, 100) is None


def test_score_release_perfect_movie():
    parsed = ParsedRelease(
        original_title="Test.Movie.2160p.UHD.BluRay.x265.Atmos.FraMeSToR",
        title="Test Movie",
        year=2023,
        resolution="2160p",
        video_codec="h265",
        audio_codec="atmos",
        audio_channels="7.1",
        source="uhd bluray",
        release_group="framestor",
        hdr="hdr10",
        languages=["en"],
        season=None,
        episode=None,
    )
    score_res = score_release(
        parsed, 50 * 1024 * 1024 * 1024, 120, expected_title="Test Movie"
    )
    assert not score_res.is_rejected
    # 3500 default score for 4k + atmos + bluray etc in config.yaml
    # We just ensure it's > 0
    assert score_res.score > 0


def test_score_release_rejected_size():
    parsed = ParsedRelease(
        original_title="Too.Small",
        title="Too Small",
        year=None,
        resolution=None,
        video_codec=None,
        audio_codec=None,
        audio_channels=None,
        source=None,
        release_group=None,
        hdr=None,
        languages=[],
        season=None,
        episode=None,
    )
    # 5MB is below min_size_mb
    score_res = score_release(parsed, 5 * 1024 * 1024, 120)
    assert score_res.is_rejected
    assert "Too small" in score_res.reject_reason


def test_score_release_blacklisted_group():
    parsed = ParsedRelease(
        original_title="Spam",
        title="Spam",
        year=None,
        resolution=None,
        video_codec=None,
        audio_codec=None,
        audio_channels=None,
        source=None,
        release_group="FUM",
        hdr=None,
        languages=[],
        season=None,
        episode=None,
    )
    result = score_release(parsed, 2000 * 1024 * 1024, 120, age_days=10)
    assert result.is_rejected
    assert "Blacklisted group" in result.reject_reason


@pytest.mark.parametrize(
    ("release_name", "expected_source"),
    [
        ("Movie.Name.2024.1080p.HDCAM.x264-GRP", "cam"),
        ("Movie.Name.2024.CAMRip.AAC-GRP", "cam"),
        ("Movie.Name.2024.CAM.x264-GRP", "cam"),
        ("Movie.Name.2024.1080p.HDTS.x264-GRP", "ts"),
        ("Movie.Name.2024.TELESYNC.x264-GRP", "ts"),
        ("Movie.Name.2024.TS.x264-GRP", "ts"),
        ("Movie.Name.2024.PDVD.x264-GRP", "ts"),
        ("Movie.Name.2024.1080p.BDRip.x264-GRP", "bdrip"),
        ("Movie.Name.2024.720p.BRRip.x264-GRP", "bdrip"),
        ("Movie.Name.2024.2160p.UHD.BluRay.x265-GRP", "uhd bluray"),
        ("Movie.Name.2024.1080p.BluRay.DTS-HD.MA.5.1.x264-GRP", "bluray"),
    ],
)
def test_parser_source_cam_ts_bdrip_and_bluray_split(
    release_name: str, expected_source: str
):
    parsed = parse_release_name(release_name)
    assert parsed.source == expected_source


@pytest.mark.parametrize(
    ("release_name", "expected_hdr_formats", "expected_hdr"),
    [
        ("Show.S01E01.2160p.WEB-DL.HLG.HEVC-GRP", ["HLG"], "hlg"),
        ("Show.S01E01.2160p.WEB-DL.HDR.HLG.HEVC-GRP", ["HDR", "HLG"], "hdr"),
        ("Show.S01E01.2160p.WEB-DL.HDR10.HLG.HEVC-GRP", ["HDR10", "HLG"], "hdr10"),
        ("Show.S01E01.2160p.WEB-DL.DV.HLG.HEVC-GRP", ["DV", "HLG"], "dolby vision"),
        ("Show.S01E01.1080p.BluRay.10bit.x265-GRP", [], "10-bit"),
    ],
)
def test_parser_hlg_and_hdr_priority(
    release_name: str, expected_hdr_formats: list[str], expected_hdr: str
):
    parsed = parse_release_name(release_name)
    assert parsed.hdr_formats == expected_hdr_formats
    assert parsed.hdr == expected_hdr


@pytest.mark.parametrize(
    ("release_name", "expected_audio"),
    [
        ("Movie.2024.1080p.BluRay.DTS-HD.MA.5.1.x264-GRP", "dts-hd ma"),
        ("Movie.2024.1080p.BluRay.DTSHDMA.7.1.x264-GRP", "dts-hd ma"),
        ("Movie.2024.1080p.BluRay.DTS-HD.HRA.5.1.x264-GRP", "dts-hd hra"),
        ("Movie.2024.1080p.BluRay.DTSHDHRA.5.1.x264-GRP", "dts-hd hra"),
        ("Movie.2024.1080p.BluRay.DTS-HD.5.1.x264-GRP", "dts-hd hra"),
        ("Movie.2005.720p.WMV.WMA.2.0-GRP", "wma"),
    ],
)
def test_parser_audio_codec_dtshd_hra_ma_and_wma(
    release_name: str, expected_audio: str
):
    parsed = parse_release_name(release_name)
    assert parsed.audio_codec == expected_audio


@pytest.mark.parametrize(
    ("release_name", "expected_vc"),
    [
        ("Movie.2004.DVDRip.XviD-GRP", "xvid"),
        ("Movie.2003.DVDRip.DivX-GRP", "divx"),
        ("Movie.2008.1080p.BluRay.VC-1-GRP", "vc-1"),
        ("Movie.2008.1080p.BluRay.VC1-GRP", "vc-1"),
        ("Movie.2007.1080i.HDTV.MPEG-2-GRP", "mpeg-2"),
        ("Movie.2007.1080i.HDTV.MPEG2-GRP", "mpeg-2"),
    ],
)
def test_parser_legacy_video_codecs(release_name: str, expected_vc: str):
    parsed = parse_release_name(release_name)
    assert parsed.video_codec == expected_vc


@pytest.mark.parametrize(
    ("release_name", "expected_flag"),
    [
        ("Movie.2024.PROPER.1080p.WEB-DL.x264-GRP", True),
        ("Movie.2024.REPACK.1080p.WEB-DL.x264-GRP", True),
        ("Movie.2024.RERIP.1080p.BluRay.x264-GRP", True),
        ("Movie.2024.1080p.WEB-DL.x264-GRP", False),
    ],
)
def test_parser_proper_repack_detection(release_name: str, expected_flag: bool):
    parsed = parse_release_name(release_name)
    assert parsed.is_proper_or_repack is expected_flag


def test_tier_css_map_keys():
    assert set(TIER_CSS_MAP.keys()) == {"gold", "silver", "bronze", "gray", "red"}


def test_default_scoring_config_rebalanced_static_tables():
    scoring = DEFAULT_SCORING_CONFIG["scoring"]
    # Resolution
    assert scoring["resolution"]["2160p"] == 2500
    assert scoring["resolution"]["1440p"] == 1600
    assert scoring["resolution"]["1080p"] == 1200
    assert scoring["resolution"]["720p"] == 400
    assert scoring["resolution"]["576p"] == 0
    assert scoring["resolution"]["480p"] == 0
    # Source
    assert scoring["source"]["remux"] == 1600
    assert scoring["source"]["bluray remux"] == 1600
    assert scoring["source"]["bluray"] == 1200
    assert scoring["source"]["uhd bluray"] == 1200
    assert scoring["source"]["web-dl"] == 900
    assert scoring["source"]["webrip"] == 500
    assert scoring["source"]["bdrip"] == 500
    assert scoring["source"]["brrip"] == 500
    assert scoring["source"]["hdtv"] == 150
    assert scoring["source"]["dvd"] == 0
    assert scoring["source"]["cam"] == -5000
    assert scoring["source"]["ts"] == -5000
    # HDR
    assert scoring["hdr"]["dolby vision"] == 1200
    assert scoring["hdr"]["hdr10+"] == 900
    assert scoring["hdr"]["hdr10"] == 600
    assert scoring["hdr"]["hdr"] == 600
    assert scoring["hdr"]["hlg"] == 300
    assert scoring["hdr"]["10-bit"] == 150
    # Audio
    assert scoring["audio_codec"]["truehd atmos"] == 1400
    assert scoring["audio_codec"]["dts:x"] == 1400
    assert scoring["audio_codec"]["dts-hd ma"] == 1100
    assert scoring["audio_codec"]["truehd"] == 1100
    assert scoring["audio_codec"]["flac"] == 1100
    assert scoring["audio_codec"]["lpcm"] == 1100
    assert scoring["audio_codec"]["eac3 atmos"] == 800
    assert scoring["audio_codec"]["dts-hd hra"] == 500
    assert scoring["audio_codec"]["eac3"] == 500
    assert scoring["audio_codec"]["dts"] == 500
    assert scoring["audio_codec"]["ac3"] == 500
    assert scoring["audio_codec"]["aac"] == 100
    assert scoring["audio_codec"]["opus"] == 100
    assert scoring["audio_codec"]["mp3"] == 100
    assert scoring["audio_codec"]["wma"] == 100
    # Channels
    assert scoring["audio_channels"]["7.1"] == 300
    assert scoring["audio_channels"]["5.1"] == 200
    assert scoring["audio_channels"]["2.0"] == 0
    # Video codec
    assert scoring["video_codec"]["av1"] == 150
    assert scoring["video_codec"]["hevc"] == 150
    assert scoring["video_codec"]["h264"] == 100
    assert scoring["video_codec"]["xvid"] == 0
    # Proper/Repack bonus
    assert scoring["proper_repack_bonus"] == 250


def test_resolve_duration_seconds_fallbacks_and_season_packs():
    # Explicit runtime for single movie/episode
    assert resolve_duration_seconds(runtime_minutes=120, media_type="movie") == 7200

    # Fallback runtime when runtime_minutes is None or 0
    assert (
        resolve_duration_seconds(runtime_minutes=None, media_type="movie") == 115 * 60
    )
    assert resolve_duration_seconds(runtime_minutes=0, media_type="tv") == 45 * 60
    assert resolve_duration_seconds(runtime_minutes=None, media_type="anime") == 24 * 60

    # Season pack with explicit season_episode_count (expected_season=1, expected_episode=None)
    assert (
        resolve_duration_seconds(
            runtime_minutes=45,
            media_type="tv",
            expected_season=1,
            expected_episode=None,
            season_episode_count=12,
        )
        == 45 * 12 * 60
    )

    # Season pack fallback when season_episode_count is missing (defaults to 10 episodes)
    assert (
        resolve_duration_seconds(
            runtime_minutes=50,
            media_type="tv",
            expected_season=1,
            expected_episode=None,
            season_episode_count=None,
        )
        == 50 * 10 * 60
    )


def test_get_audio_bitrate_offset_tiers():
    assert get_audio_bitrate_offset("truehd atmos") == pytest.approx(4.0)
    assert get_audio_bitrate_offset("dts-hd ma") == pytest.approx(4.0)
    assert get_audio_bitrate_offset("flac") == pytest.approx(4.0)
    assert get_audio_bitrate_offset("dts-hd hra") == pytest.approx(2.0)
    assert get_audio_bitrate_offset("dts") == pytest.approx(1.5)
    assert get_audio_bitrate_offset("eac3 atmos") == pytest.approx(0.8)
    assert get_audio_bitrate_offset("ac3") == pytest.approx(0.4)
    assert get_audio_bitrate_offset("aac") == pytest.approx(0.4)
    assert get_audio_bitrate_offset(None) == pytest.approx(0.4)


def test_get_codec_efficiency_factor_tiers():
    assert get_codec_efficiency_factor("av1") == pytest.approx(1.45)
    assert get_codec_efficiency_factor("hevc") == pytest.approx(1.35)
    assert get_codec_efficiency_factor("h.265") == pytest.approx(1.35)
    assert get_codec_efficiency_factor("h.264") == pytest.approx(1.00)
    assert get_codec_efficiency_factor("vc-1") == pytest.approx(0.75)
    assert get_codec_efficiency_factor("mpeg-2") == pytest.approx(0.75)
    assert get_codec_efficiency_factor("xvid") == pytest.approx(0.75)
    assert get_codec_efficiency_factor(None) == pytest.approx(1.00)


def test_calculate_dynamic_bitrate_score_regions():
    # 2160p step thresholds: <8 -> -1000, <14 -> 200, <25 -> 700, <45 -> 1200, >=45 -> 1500
    assert calculate_dynamic_bitrate_score(5.0, "2160p") == -1000
    assert calculate_dynamic_bitrate_score(10.0, "2160p") == 200
    assert calculate_dynamic_bitrate_score(20.0, "2160p") == 700
    assert calculate_dynamic_bitrate_score(30.0, "2160p") == 1200
    assert calculate_dynamic_bitrate_score(50.0, "2160p") == 1500

    # 1080p step thresholds: <2.5 -> -600, <5 -> 200, <10 -> 500, <18 -> 900, >=18 -> 1100
    assert calculate_dynamic_bitrate_score(1.5, "1080p") == -600
    assert calculate_dynamic_bitrate_score(4.0, "1080p") == 200
    assert calculate_dynamic_bitrate_score(8.0, "1080p") == 500
    assert calculate_dynamic_bitrate_score(14.0, "1080p") == 900
    assert calculate_dynamic_bitrate_score(22.0, "1080p") == 1100


def test_cam_ts_hard_rejection_and_sdr_10bit_guard_and_proper_bonus():
    # CAM hard rejection
    cam_rel = parse_release_name("Movie.2024.1080p.HDCAM.x264-GRP")
    cam_res = score_release(cam_rel, size_bytes=2 * 1024**3, runtime_minutes=120)
    assert cam_res.is_rejected is True
    assert cam_res.score == -5000.0
    assert cam_res.reject_reason == "Disallowed low-quality source: cam/telesync"

    # TS hard rejection
    ts_rel = parse_release_name("Movie.2024.1080p.HDTS.x264-GRP")
    ts_res = score_release(ts_rel, size_bytes=2 * 1024**3, runtime_minutes=120)
    assert ts_res.is_rejected is True
    assert ts_res.score == -5000.0
    assert ts_res.reject_reason == "Disallowed low-quality source: cam/telesync"

    # SDR 10-bit guard: 10-bit adds +150 only when no HDR format is present
    dv_10bit = parse_release_name("Movie.2024.2160p.WEB-DL.DV.10bit.HEVC-GRP")
    dv_no_10bit = parse_release_name("Movie.2024.2160p.WEB-DL.DV.HEVC-GRP")
    res_dv_10bit = score_release(dv_10bit, size_bytes=15 * 1024**3, runtime_minutes=120)
    res_dv_no_10bit = score_release(
        dv_no_10bit, size_bytes=15 * 1024**3, runtime_minutes=120
    )
    assert res_dv_10bit.score == pytest.approx(res_dv_no_10bit.score)

    sdr_10bit = parse_release_name("Movie.2024.1080p.WEB-DL.10bit.HEVC-GRP")
    sdr_8bit = parse_release_name("Movie.2024.1080p.WEB-DL.HEVC-GRP")
    res_sdr_10 = score_release(sdr_10bit, size_bytes=6 * 1024**3, runtime_minutes=120)
    res_sdr_8 = score_release(sdr_8bit, size_bytes=6 * 1024**3, runtime_minutes=120)
    assert res_sdr_10.score - res_sdr_8.score == pytest.approx(150.0)

    # PROPER/REPACK +250 bonus
    proper_rel = parse_release_name("Movie.2024.PROPER.1080p.WEB-DL.HEVC-GRP")
    assert score_release(
        proper_rel, size_bytes=6 * 1024**3, runtime_minutes=120
    ).score - res_sdr_8.score == pytest.approx(250.0)


def test_spec_worked_verification_examples():
    """Verify the 3 worked verification examples from Spec §3.2."""
    # Example 1: Starved 4K Encode (4.5 GB, 120 min) vs 1080p BluRay Remux (28 GB, 120 min)
    starved_4k = parse_release_name(
        "Movie.2024.2160p.WEB-DL.HDR10.DDP5.1.Atmos.HEVC-STARVED"
    )
    remux_1080p = parse_release_name(
        "Movie.2024.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-REMUXGRP"
    )
    res_starved_4k = score_release(
        starved_4k, size_bytes=int(4.5 * 10**9), runtime_minutes=120, media_type="movie"
    )
    res_remux_1080p = score_release(
        remux_1080p,
        size_bytes=int(28.0 * 10**9),
        runtime_minutes=120,
        media_type="movie",
    )
    assert res_remux_1080p.score > res_starved_4k.score

    # Example 2: 1080p HEVC Encode (8 GB, 120 min) vs 1080p x264 Encode (10 GB, 120 min)
    hevc_1080p = parse_release_name("Movie.2024.1080p.BluRay.x265.DTS.5.1-GRP")
    x264_1080p = parse_release_name("Movie.2024.1080p.BluRay.x264.DTS.5.1-GRP")
    res_hevc = score_release(
        hevc_1080p, size_bytes=8 * 10**9, runtime_minutes=120, media_type="movie"
    )
    res_x264 = score_release(
        x264_1080p, size_bytes=10 * 10**9, runtime_minutes=120, media_type="movie"
    )
    assert res_hevc.normalized_bitrate_mbps is not None
    assert res_x264.normalized_bitrate_mbps is not None
    assert res_hevc.normalized_bitrate_mbps > res_x264.normalized_bitrate_mbps
    assert res_hevc.score > res_x264.score

    # Example 3: Season Pack (60 GB, 12 eps, 45 min) vs Single Episode (5 GB, 45 min)
    season_pack = parse_release_name("Show.S01.1080p.WEB-DL.DDP5.1.H265-GRP")
    single_ep = parse_release_name("Show.S01E01.1080p.WEB-DL.DDP5.1.H265-GRP")
    res_pack = score_release(
        season_pack,
        size_bytes=60 * 10**9,
        runtime_minutes=45,
        media_type="tv",
        season_episode_count=12,
    )
    res_ep = score_release(
        single_ep,
        size_bytes=5 * 10**9,
        runtime_minutes=45,
        media_type="tv",
    )
    assert res_pack.bitrate_mbps == pytest.approx(res_ep.bitrate_mbps, rel=1e-3)
    assert res_pack.normalized_bitrate_mbps == pytest.approx(
        res_ep.normalized_bitrate_mbps, rel=1e-3
    )
    assert res_pack.score == pytest.approx(res_ep.score, rel=1e-3)
