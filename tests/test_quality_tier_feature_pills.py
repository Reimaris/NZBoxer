"""
Tests for Ticket 01: Compound Release Parser, 4-Tier Quality Feature Pills & Manual Picker Redesign.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import parser
from app.core.parser import parse_release_name
from app.core.push_engine import _score_and_partition_candidates
from app.core.scorer import score_release
from app.db.models import (
    EpisodeStatus,
    MediaType,
    Provider,
    SeasonStatus,
)
from app.services import treasure_maps
from tests.conftest import make_history, make_series


@pytest.mark.parametrize(
    ("release_name", "expected_source", "expected_hdr_formats", "expected_color_depth", "expected_audio", "expected_channels", "expected_dl"),
    [
        (
            "Dune.Part.Two.2024.German.DL.2160p.UHD.BluRay.REMUX.DV.HDR10Plus.10bit.HEVC.TrueHD.7.1.Atmos-GROUP",
            "bluray remux",
            ["DV", "HDR10+"],
            "10-bit",
            "truehd atmos",
            "7.1",
            True,
        ),
        (
            "Oppenheimer.2023.2160p.UHD.BluRay.10bit.DoVi.HDR10.x265.DTS-HD.MA.5.1-RELEASE",
            "uhd bluray",
            ["DV", "HDR10"],
            "10-bit",
            "dts-hd ma",
            "5.1",
            False,
        ),
        (
            "The.Boys.S04E01.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX",
            "web-dl",
            [],
            None,
            "eac3 atmos",
            "5.1",
            False,
        ),
        (
            "Show.Name.S01E02.720p.WEBRip.10bit.x265.AAC2.0-GRP",
            "webrip",
            [],
            "10-bit",
            "aac",
            "2.0",
            False,
        ),
        (
            "Action.Film.2022.1080p.BluRay.DTS-X.7.1.x265-SCENE",
            "bluray",
            [],
            None,
            "dts:x",
            "7.1",
            False,
        ),
    ],
)
def test_compound_release_parser_extractions(
    release_name: str,
    expected_source: str,
    expected_hdr_formats: list[str],
    expected_color_depth: str | None,
    expected_audio: str,
    expected_channels: str,
    expected_dl: bool,
):
    """Verify parse_release_name extracts compound source, multi-tag HDR, 10-bit, Atmos/DTS-HD MA, channels, and DL."""
    parsed = parse_release_name(release_name)
    assert parsed.source == expected_source
    assert parsed.hdr_formats == expected_hdr_formats
    assert parsed.color_depth == expected_color_depth
    assert parsed.audio_codec == expected_audio
    assert parsed.audio_channels == expected_channels
    assert parsed.is_dual_language is expected_dl


def test_scorer_multi_tag_hdr_remux_and_atmos_weights():
    """Verify score_release awards full Dolby Vision (+1000), Remux (+1500), and TrueHD Atmos (+3800) points even when 10bit precedes DV."""
    rel_dv_10bit = parse_release_name(
        "Blade.Runner.2049.2017.2160p.UHD.BluRay.Remux.10bit.DV.HDR10+.HEVC.TrueHD.7.1.Atmos-FG"
    )
    rel_sdr_10bit = parse_release_name(
        "Blade.Runner.2049.2017.2160p.UHD.BluRay.Remux.10bit.HEVC.TrueHD.7.1.Atmos-FG"
    )

    res_dv = score_release(rel_dv_10bit, size_bytes=60 * 1024**3)
    res_sdr = score_release(rel_sdr_10bit, size_bytes=60 * 1024**3)

    assert not res_dv.is_rejected
    assert not res_sdr.is_rejected
    # DV (+1000) vs 10-bit SDR (+300) -> +700 difference
    assert res_dv.score - res_sdr.score == 700
    # 2160p (2500) + Remux (1500) + HEVC (400) + DV (1000) + TrueHD Atmos (3800) + 7.1 (200) = 9400
    assert res_dv.score >= 9400


def test_build_release_feature_pills_ordering_and_4_tier_colors():
    """Verify build_release_feature_pills returns exact category ordering and 4-Tier Semantic Quality Palette classes."""
    assert hasattr(parser, "build_release_feature_pills"), "build_release_feature_pills must exist on app.core.parser"

    title = "Dune.Part.Two.2024.German.DL.2160p.UHD.BluRay.REMUX.DV.HDR10Plus.HEVC.TrueHD.7.1.Atmos-AilMWeb"
    parsed = parse_release_name(title)
    pills = parser.build_release_feature_pills(
        parsed=parsed,
        score=9400,
        size_bytes=int(48.5 * (1024**3)),
        is_fallback=False,
        is_mismatch=False,
        matched_language="de",
    )

    categories = [p["category"] for p in pills]
    assert categories == [
        "score",
        "language",
        "resolution",
        "source",
        "video_codec",
        "hdr",
        "audio",
        "size",
        "group",
    ]

    by_cat = {p["category"]: p for p in pills}
    assert by_cat["score"]["label"] == "Score: 9400"
    assert by_cat["score"]["tier"] == "ultra"
    assert "emerald" in by_cat["score"]["css"]

    assert "DL" in by_cat["language"]["label"]
    assert by_cat["language"]["tier"] == "ultra"

    assert by_cat["resolution"]["label"] in ("4K", "2160p")
    assert by_cat["resolution"]["tier"] == "ultra"

    assert by_cat["source"]["label"] == "BluRay Remux"
    assert by_cat["source"]["tier"] == "ultra"

    assert by_cat["video_codec"]["label"] == "HEVC"
    assert by_cat["video_codec"]["tier"] == "ultra"

    assert by_cat["hdr"]["label"] == "DV + HDR10+"
    assert by_cat["hdr"]["tier"] == "ultra"

    assert by_cat["audio"]["label"] == "TrueHD 7.1 Atmos"
    assert by_cat["audio"]["tier"] == "ultra"

    assert by_cat["size"]["label"] == "48.5 GB"
    assert by_cat["size"]["tier"] == "neutral"
    assert "zinc" in by_cat["size"]["css"]

    assert by_cat["group"]["label"] == "Grp: AilMWeb"
    assert by_cat["group"]["tier"] == "neutral"

    # Verify High / Mid / Low tier mappings on a lower-quality mismatched release
    low_parsed = parse_release_name("Movie.2020.French.480p.DVD.x264.AAC.2.0-LOW")
    low_pills = {
        p["category"]: p
        for p in parser.build_release_feature_pills(
            parsed=low_parsed,
            score=900,
            size_bytes=700 * 1024 * 1024,
            is_fallback=False,
            is_mismatch=True,
            matched_language="fr",
        )
    }
    assert low_pills["score"]["tier"] == "low"
    assert "red" in low_pills["score"]["css"]
    assert low_pills["language"]["tier"] == "low"
    assert low_pills["resolution"]["tier"] == "low"
    assert low_pills["source"]["tier"] == "low"
    assert low_pills["video_codec"]["tier"] == "mid"
    assert "amber" in low_pills["video_codec"]["css"]
    assert low_pills["audio"]["tier"] == "mid"
    assert low_pills["size"]["label"] == "700 MB"


def test_score_and_partition_deduplicates_normalized_titles_and_attaches_pills():
    """Verify _score_and_partition_candidates deduplicates identical titles with different GUIDs and attaches feature_pills."""
    raw_results = [
        {
            "title": "Severance.S01E01.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX",
            "guid": "guid-indexer-1",
            "size": 2_500_000_000,
        },
        {
            "title": "  severance.s01e01.1080p.web-dl.ddp5.1.atmos.h.264-flux  ",
            "guid": "guid-indexer-2",
            "size": 2_500_000_000,
        },
        {
            "title": "Severance.S01E01.French.1080p.WEB-DL.H.264-FRGRP",
            "guid": "guid-fr-1",
            "size": 2_000_000_000,
        },
    ]
    part = _score_and_partition_candidates(
        raw_results=raw_results,
        blacklisted_guids=set(),
        blacklisted_titles=set(),
        expected_title="Severance",
        expected_year=None,
        expected_alt_title=None,
        expected_season=1,
        expected_episode=1,
        expected_season_title=None,
        runtime_minutes=50,
        effective_cfg={"primary_language": "en", "fallback_language": "none"},
    )
    assert len(part["primary"]) == 1
    assert len(part["mismatched"]) == 1
    assert "feature_pills" in part["primary"][0]
    assert any(p["category"] == "audio" and "Atmos" in p["label"] for p in part["primary"][0]["feature_pills"])
    assert "feature_pills" in part["mismatched"][0]
    assert any(p["category"] == "language" and p["tier"] == "low" for p in part["mismatched"][0]["feature_pills"])


@pytest.mark.asyncio
async def test_push_modal_ui_polish_and_cross_view_pills(
    db_session: AsyncSession,
    async_client: AsyncClient,
    watchlist_provider: Provider,
    default_search_preset,
    monkeypatch,
):
    """Verify Push Modal status pill filtering, 3-col episode grid, Episode Accordion 3-tier partitioning, and Card 4/5/6 feature pills."""
    # --- Seed: one anime series, Season 1 with 2 episodes ---
    series, (s1,), (ep1, ep2) = await make_series(
        db_session,
        simkl_id=88001,
        title="Andor",
        year=2022,
        media_type=MediaType.ANIME,
        provider_id=watchlist_provider.id,
        seasons=[{"season_number": 1, "watch_order": 1, "type_number": 1, "entry_type": "season",
                  "title": "Season 1", "monitored": True, "status": SeasonStatus.SEARCHING,
                  "episode_count": 2}],
        episodes_per_season=2,
        episode_statuses=[EpisodeStatus.COMPLETED, EpisodeStatus.SEARCHING],
    )

    # ep1 is already COMPLETED; ep2 is SEARCHING → flip ep2 to DOWNLOADING after history insert
    hist_completed = await make_history(
        db_session,
        item=series,
        season=s1,
        episode=ep1,
        nzb_title="Andor.S01E01.German.DL.2160p.UHD.BluRay.Remux.DV.HDR10.HEVC.TrueHD.7.1.Atmos-GRP",
        nzb_guid="guid-hist-1",
        torbox_id="tb-100",
        score=9200.0,
        size_bytes=20 * (1024**3),
        status_detail="completed",
        is_dismissed=True,
    )
    hist_active = await make_history(
        db_session,
        item=series,
        season=s1,
        episode=ep2,
        nzb_title="Andor.S01E02.German.DL.2160p.WEB-DL.DV.HDR10.HEVC.DDP5.1.Atmos-GRP",
        nzb_guid="guid-hist-2",
        torbox_id="tb-101",
        score=5400.0,
        size_bytes=8 * (1024**3),
        status_detail="downloading",
        is_dismissed=False,
    )
    ep2.status = EpisodeStatus.DOWNLOADING
    await db_session.commit()

    # Keep local references for assertions that use hist_completed / hist_active
    _ = hist_completed, hist_active

    # 1. Push Modal HTML checks
    modal_resp = await async_client.get(f"/api/items/{series.id}/push-modal")
    assert modal_resp.status_code == 200
    modal_html = modal_resp.text

    # Actionable-only status pills: SEARCHING and PENDING must not be rendered as status badges
    assert ">searching<" not in modal_html.lower()
    assert ">pending<" not in modal_html.lower()
    assert "DOWNLOADED" in modal_html
    assert "DOWNLOADING" in modal_html
    # 3-column episode grid & compact delete icon button
    assert "md:grid-cols-3" in modal_html
    assert "Delete episode E01 from TorBox" in modal_html
    # Explicit SVG width/height on Reset Metadata / AniList
    assert 'width="16" height="16"' in modal_html
    # Episode Accordion 3-tier language partitioning & single-line feature pills
    assert "showMismatchedEp" in modal_html
    assert "rel.feature_pills" in modal_html

    # 2. Card 4 History Ledger on GET /
    dash_resp = await async_client.get("/")
    assert dash_resp.status_code == 200
    dash_html = dash_resp.text
    assert "BluRay Remux" in dash_html
    assert "Score: 9200" in dash_html
    assert "Remaining:" in dash_html
    # Pre-declared w-3.5 h-3.5 in base.html
    assert "w-3.5 h-3.5" in dash_html

    # 3. Card 5 Active Pushes on GET /api/pushes/active
    active_resp = await async_client.get("/api/pushes/active")
    assert active_resp.status_code == 200
    active_html = active_resp.text
    assert "Score: 5400" in active_html
    assert "WEB-DL" in active_html

    # 4. Card 6 Ad-Hoc Manual Search on POST /api/search/manual
    async def fake_search_raw(**kwargs):
        return [
            {
                "title": "Andor.S01E01.2160p.WEB-DL.DV.HDR10.HEVC.DDP5.1.Atmos-FLUX",
                "guid": "guid-m1",
                "link": "http://idx.local/get?id=1",
                "size": 6_500_000_000,
            },
            {
                "title": "andor.s01e01.2160p.web-dl.dv.hdr10.hevc.ddp5.1.atmos-flux",
                "guid": "guid-m2",
                "link": "http://idx.local/get?id=2",
                "size": 6_500_000_000,
            },
        ]

    monkeypatch.setattr(treasure_maps, "search_raw", fake_search_raw)
    search_resp = await async_client.post(
        "/api/search/manual",
        data={"query": "Andor", "category": "any", "primary_language": "en"},
    )
    assert search_resp.status_code == 200
    search_html = search_resp.text
    assert "Primary Language Releases (1)" in search_html
    assert "w-20 h-20" not in search_html
    assert "DD+ 5.1 Atmos" in search_html
    assert "DV + HDR10" in search_html
