"""Unit tests for the NZB release name parser."""
from app.core.parser import parse_release_name


def test_parse_movie_standard():
    title = "The.Matrix.1999.2160p.UHD.BluRay.x265.10bit.HDR.TrueHD.7.1.Atmos-FraMeSToR"
    parsed = parse_release_name(title)
    
    assert parsed.title == "The Matrix"
    assert parsed.year == 1999
    assert parsed.resolution == "2160p"
    assert parsed.video_codec == "h.265"
    assert parsed.audio_codec == "dolby truehd"
    assert parsed.audio_channels == "7.1"
    assert parsed.source == "ultra hd blu-ray"
    assert parsed.release_group == "framestor"
    assert parsed.hdr == "10-bit"

def test_parse_tv_show():
    title = "Breaking.Bad.S01E01.1080p.WEB-DL.DD5.1.H.264-NTb"
    parsed = parse_release_name(title)
    
    assert parsed.title == "Breaking Bad"
    assert parsed.resolution == "1080p"
    assert parsed.source == "web"
    assert parsed.video_codec == "h.264"
    assert parsed.release_group == "ntb"

def test_parse_garbage():
    title = "just_some_random_crap"
    parsed = parse_release_name(title)
    
    # Guessit might try its best, but fields should safely handle it
    assert parsed.original_title == title
