"""Unit tests for the scoring engine."""
from app.core.parser import ParsedRelease
from app.core.scorer import calculate_bitrate_mbps, score_release


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
        episode=None
    )
    score_res = score_release(parsed, 50 * 1024 * 1024 * 1024, 120, expected_title="Test Movie")
    assert not score_res.is_rejected
    # 3500 default score for 4k + atmos + bluray etc in config.yaml
    # We just ensure it's > 0
    assert score_res.score > 0

def test_score_release_rejected_size():
    parsed = ParsedRelease(
        original_title="Too.Small",
        title="Too Small", year=None, resolution=None, video_codec=None,
        audio_codec=None, audio_channels=None, source=None, release_group=None,
        hdr=None, languages=[], season=None, episode=None
    )
    # 5MB is below min_size_mb
    score_res = score_release(parsed, 5 * 1024 * 1024, 120)
    assert score_res.is_rejected
    assert "Too small" in score_res.reject_reason

def test_score_release_blacklisted_group():
    parsed = ParsedRelease(
        original_title="Spam",
        title="Spam", year=None, resolution=None, video_codec=None,
        audio_codec=None, audio_channels=None, source=None, release_group="FUM",
        hdr=None, languages=[], season=None, episode=None
    )
    result = score_release(parsed, 2000 * 1024 * 1024, 120, age_days=10)
    assert result.is_rejected
    assert "Blacklisted group" in result.reject_reason
