from app.core.parser import parse_release_name
from app.core.scorer import score_release


def test_scorer_primary_de_fallback_en():
    # 1. German release
    parsed_de = parse_release_name("Inception.2010.German.1080p.BluRay.x264-GROUP")
    res_de = score_release(
        parsed_de,
        size_bytes=5_000_000_000,
        primary_language="de",
        fallback_language="en",
    )
    assert not res_de.is_rejected
    assert res_de.is_primary is True
    assert res_de.is_fallback is False
    assert res_de.matched_language == "de"

    # 2. Dual-Language release containing German
    parsed_dl = parse_release_name("Inception.2010.German.DL.1080p.BluRay.x264-GROUP")
    res_dl = score_release(
        parsed_dl,
        size_bytes=5_000_000_000,
        primary_language="de",
        fallback_language="en",
    )
    assert not res_dl.is_rejected
    assert res_dl.is_primary is True
    assert res_dl.is_fallback is False
    assert res_dl.matched_language == "de"

    # 3. Explicit English release
    parsed_en = parse_release_name("Inception.2010.English.1080p.BluRay.x264-GROUP")
    res_en = score_release(
        parsed_en,
        size_bytes=5_000_000_000,
        primary_language="de",
        fallback_language="en",
    )
    assert not res_en.is_rejected
    assert res_en.is_primary is False
    assert res_en.is_fallback is True
    assert res_en.matched_language == "en"

    # 4. Untagged release (inferred as English)
    parsed_untagged = parse_release_name("Inception.2010.1080p.BluRay.x264-GROUP")
    res_untagged = score_release(
        parsed_untagged,
        size_bytes=5_000_000_000,
        primary_language="de",
        fallback_language="en",
    )
    assert not res_untagged.is_rejected
    assert res_untagged.is_primary is False
    assert res_untagged.is_fallback is True
    assert res_untagged.matched_language == "en"

    # 5. French release (mismatched)
    parsed_fr = parse_release_name("Inception.2010.FRENCH.1080p.BluRay.x264-GROUP")
    res_fr = score_release(
        parsed_fr,
        size_bytes=5_000_000_000,
        primary_language="de",
        fallback_language="en",
    )
    assert res_fr.is_rejected is True
    assert "language mismatch" in (res_fr.reject_reason or "").lower()


def test_scorer_primary_de_fallback_none():
    # 1. German release -> Accepted
    parsed_de = parse_release_name("Inception.2010.German.1080p.BluRay.x264-GROUP")
    res_de = score_release(
        parsed_de,
        size_bytes=5_000_000_000,
        primary_language="de",
        fallback_language=None,
    )
    assert not res_de.is_rejected
    assert res_de.is_primary is True
    assert res_de.is_fallback is False

    # 2. English release -> Rejected
    parsed_en = parse_release_name("Inception.2010.English.1080p.BluRay.x264-GROUP")
    res_en = score_release(
        parsed_en,
        size_bytes=5_000_000_000,
        primary_language="de",
        fallback_language=None,
    )
    assert res_en.is_rejected is True

    # 3. Untagged release -> Rejected
    parsed_untagged = parse_release_name("Inception.2010.1080p.BluRay.x264-GROUP")
    res_untagged = score_release(
        parsed_untagged,
        size_bytes=5_000_000_000,
        primary_language="de",
        fallback_language=None,
    )
    assert res_untagged.is_rejected is True


def test_scorer_no_language_configured():
    # Any release accepted
    parsed_untagged = parse_release_name("Inception.2010.1080p.BluRay.x264-GROUP")
    res = score_release(
        parsed_untagged,
        size_bytes=5_000_000_000,
        primary_language=None,
        fallback_language=None,
    )
    assert not res.is_rejected
    assert res.is_primary is True
    assert res.is_fallback is False
