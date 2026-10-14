"""
Unit and Integration Tests for Ticket 04:
Preset Custom Scorer & On-Demand Push Engine (`v3.0.0`)
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.parser import parse_release_name
from app.core.scorer import score_release
from app.core.search_lock import item_lock_manager
from app.db.models import (
    DownloadHistory,
    Episode,
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    MediaType,
    Provider,
    ProviderCategory,
    SearchPreset,
    Season,
    SeasonStatus,
)
from app.main import wait_for_background_pushes


def test_score_release_best_vs_custom_constraints() -> None:
    """
    Verify that `score_release` applies 'Best' vs 'Custom' constraints:
    - Resolution filters (`resolutions`)
    - Source tiers (`sources`)
    - Video codecs & HDR (`video_codecs`)
    - Audio formats (`audio_formats`)
    - Min/Max size in GB (`min_size_gb`, `max_size_gb`)
    - Required & excluded keywords (`required_keywords`, `excluded_keywords`)
    - Primary & Fallback language hierarchy
    while preserving underlying global Video Scoring point weights.
    """
    r_4k_remux = parse_release_name(
        "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HDR10.HEVC.TrueHD.7.1.Atmos-GROUP"
    )
    r_1080p_web = parse_release_name(
        "Dune.Part.Two.2024.German.DL.1080p.WEB-DL.DDP5.1.H.264-SCENE"
    )
    r_720p_hdtv = parse_release_name("Dune.Part.Two.2024.720p.HDTV.x264.AAC2.0-LOL")

    gb = 1024 * 1024 * 1024

    # 1. In "best" mode, all valid releases pass even if custom_config has strict rules
    strict_custom = {
        "resolutions": ["1080p"],
        "sources": ["web-dl"],
        "video_codecs": ["h264"],
        "min_size_gb": 2.0,
        "max_size_gb": 15.0,
        "required_keywords": ["scene"],
        "excluded_keywords": ["remux"],
        "audio_formats": ["eac3"],
    }
    best_res = score_release(
        r_4k_remux,
        size_bytes=50 * gb,
        expected_title="Dune: Part Two",
        expected_year=2024,
        video_quality_mode="best",
        audio_quality_mode="best",
        custom_config=strict_custom,
    )
    assert best_res.is_rejected is False
    assert best_res.score > 0

    # 2. In "custom" video mode, 4K Remux is rejected by resolution / size / excluded_keywords
    custom_reject_res = score_release(
        r_4k_remux,
        size_bytes=50 * gb,
        expected_title="Dune: Part Two",
        expected_year=2024,
        video_quality_mode="custom",
        audio_quality_mode="best",
        custom_config={"resolutions": ["1080p"]},
    )
    assert custom_reject_res.is_rejected is True
    assert "resolution" in (custom_reject_res.reject_reason or "").lower()

    # 3. Size limits in GB (min_size_gb and max_size_gb)
    too_large_res = score_release(
        r_1080p_web,
        size_bytes=20 * gb,
        expected_title="Dune: Part Two",
        expected_year=2024,
        video_quality_mode="custom",
        custom_config={"max_size_gb": 12.0},
    )
    assert too_large_res.is_rejected is True
    assert "max size" in (too_large_res.reject_reason or "").lower()

    too_small_res = score_release(
        r_720p_hdtv,
        size_bytes=int(0.8 * gb),
        expected_title="Dune: Part Two",
        expected_year=2024,
        video_quality_mode="custom",
        custom_config={"min_size_gb": 1.5},
    )
    assert too_small_res.is_rejected is True
    assert "min size" in (too_small_res.reject_reason or "").lower()

    # 4. Required & Excluded keywords
    missing_req = score_release(
        r_720p_hdtv,
        size_bytes=2 * gb,
        expected_title="Dune: Part Two",
        expected_year=2024,
        video_quality_mode="custom",
        custom_config={"required_keywords": ["uncut", "imax"]},
    )
    assert missing_req.is_rejected is True
    assert "required keyword" in (missing_req.reject_reason or "").lower()

    excluded_hit = score_release(
        r_720p_hdtv,
        size_bytes=2 * gb,
        expected_title="Dune: Part Two",
        expected_year=2024,
        video_quality_mode="custom",
        custom_config={"excluded_keywords": ["cam", "hdtv"]},
    )
    assert excluded_hit.is_rejected is True
    assert "excluded keyword" in (excluded_hit.reject_reason or "").lower()

    # 5. Audio custom mode filtering
    audio_reject = score_release(
        r_720p_hdtv,
        size_bytes=2 * gb,
        expected_title="Dune: Part Two",
        expected_year=2024,
        video_quality_mode="best",
        audio_quality_mode="custom",
        custom_config={"audio_formats": ["truehd", "dts-hd"]},
    )
    assert audio_reject.is_rejected is True
    assert "audio" in (audio_reject.reject_reason or "").lower()

    audio_pass = score_release(
        r_4k_remux,
        size_bytes=35 * gb,
        expected_title="Dune: Part Two",
        expected_year=2024,
        video_quality_mode="best",
        audio_quality_mode="custom",
        custom_config={"audio_formats": ["truehd", "atmos"]},
    )
    assert audio_pass.is_rejected is False

    # 6. Primary / Fallback language + custom constraints pass on 1080p German DL WEB-DL
    custom_pass = score_release(
        r_1080p_web,
        size_bytes=8 * gb,
        expected_title="Dune: Part Two",
        expected_year=2024,
        primary_language="de",
        fallback_language="en",
        video_quality_mode="custom",
        audio_quality_mode="custom",
        custom_config=strict_custom,
    )
    assert custom_pass.is_rejected is False
    assert custom_pass.is_primary is True
    assert custom_pass.matched_language == "de"
    assert custom_pass.score > 0


@pytest.mark.asyncio
async def test_push_auto_persists_sticky_config_locks_item_and_dispatches_movie(
    db_session,
    async_client: AsyncClient,
) -> None:
    """
    Verify `POST /api/items/{item_id}/push/auto`:
    - Persists sticky search preferences on MediaItem
    - Rejects concurrent requests with HTTP 409 when item is locked
    - Filters candidates using custom preset/modal constraints
    - Dispatches #1 candidate to TorBox with `DownloadHistory.push_mode = "auto"`
    """
    provider = Provider(
        name="Simkl",
        type="simkl",
        category=ProviderCategory.WATCHLIST.value,
        is_active=True,
    )
    indexer = Provider(
        name="PrimaryIndexer",
        type="newznab",
        category=ProviderCategory.INDEXER.value,
        api_url="https://idx.example/api",
        api_key="idx-key",
        priority=1,
        is_active=True,
    )
    db_session.add_all([provider, indexer])
    await db_session.flush()

    preset = SearchPreset(
        name="1080p German",
        is_default=False,
        primary_language="de",
        fallback_language="en",
        video_quality_mode="custom",
        audio_quality_mode="best",
        custom_config_json='{"resolutions": ["1080p"], "max_size_gb": 15}',
    )
    db_session.add(preset)
    await db_session.flush()

    movie = MediaItem(
        provider_id=provider.id,
        simkl_id=7001,
        tmdb_id=7001,
        title="Inception",
        year=2010,
        media_type=MediaType.MOVIE,
        status=MediaStatus.SEARCHING,
        release_date=datetime(2010, 7, 16, tzinfo=timezone.utc),
    )
    db_session.add(movie)
    await db_session.commit()

    gb = 1024 * 1024 * 1024
    mock_results = [
        # 2160p has higher base score, but preset restricts to 1080p and max 15GB!
        {
            "title": "Inception.2010.German.DL.2160p.UHD.BluRay.x265-GRP",
            "size": 25 * gb,
            "guid": "guid-4k-rejected",
        },
        # 1080p German matches preset custom constraints
        {
            "title": "Inception.2010.German.DL.1080p.BluRay.x265-GRP",
            "size": 8 * gb,
            "guid": "guid-1080p-chosen",
        },
    ]

    # 1. Verify lock conflict returns 409 when item is already locked
    assert item_lock_manager.try_acquire(movie.id, owner="background") is True
    try:
        conflict_resp = await async_client.post(
            f"/api/items/{movie.id}/push/auto",
            json={"preset_id": preset.id},
        )
        assert conflict_resp.status_code == 409
    finally:
        item_lock_manager.release(movie.id)

    # 2. Execute Auto-Push Best when unlocked
    with (
        patch(
            "app.services.indexers.newznab.search_movie",
            new=AsyncMock(return_value=mock_results),
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(
                return_value=(
                    b'<nzb><file subject="Inception.mkv"></file></nzb>',
                    "inception.nzb",
                )
            ),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new=AsyncMock(return_value={"id": "tb-auto-101", "hash": "hash-auto-101"}),
        ) as mock_send,
    ):
        resp = await async_client.post(
            f"/api/items/{movie.id}/push/auto",
            json={
                "preset_id": preset.id,
                "prefer_season_packs": False,
                "auto_advance_seasons": True,
            },
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "started"
        assert data["pushed"] is True
        assert data["push_mode"] == "auto"
        await wait_for_background_pushes()

    assert mock_send.await_count == 1

    # Verify sticky config & DownloadHistory in DB
    stmt = (
        select(MediaItem)
        .where(MediaItem.id == movie.id)
        .options(selectinload(MediaItem.download_history))
        .execution_options(populate_existing=True)
    )
    updated_movie = (await db_session.execute(stmt)).scalars().first()
    assert updated_movie is not None
    assert updated_movie.preset_id == preset.id
    assert updated_movie.prefer_season_packs is False
    assert updated_movie.auto_advance_seasons is True
    assert updated_movie.status == MediaStatus.DOWNLOADING
    assert len(updated_movie.download_history) == 1
    dh = updated_movie.download_history[0]
    assert dh.nzb_guid == "guid-1080p-chosen"
    assert dh.push_mode == "auto"
    assert dh.downloader_transfer_id == "tb-auto-101"


@pytest.mark.asyncio
async def test_push_auto_pack_to_episode_fallback_and_franchise_movie_routing(
    db_session,
    async_client: AsyncClient,
) -> None:
    """
    Verify:
    1. When `prefer_season_packs` is True and no valid Season Pack is found for a TV season,
       `Auto-Push Best` automatically falls back to searching and pushing all missing episodes.
    2. For franchise `Movie` entries (`entry_type == "movie"`), `Auto-Push Best` routes queries
       using movie categories (`newznab_client.search_movie`).
    3. When `auto_advance_seasons` is True, pushing Season 1 with Movie Bridge Lookahead
       also advances across Movie 1 and Season 2 if released.
    """
    provider = Provider(
        name="Simkl",
        type="simkl",
        category=ProviderCategory.WATCHLIST.value,
        is_active=True,
    )
    indexer = Provider(
        name="AnimeIdx",
        type="newznab",
        category=ProviderCategory.INDEXER.value,
        api_url="https://idx.example/api",
        api_key="idx-key",
        priority=1,
        is_active=True,
    )
    db_session.add_all([provider, indexer])
    await db_session.flush()

    anime = MediaItem(
        provider_id=provider.id,
        simkl_id=8001,
        anilist_id=101922,
        title="Demon Slayer",
        year=2019,
        media_type=MediaType.ANIME,
        status=MediaStatus.SEARCHING,
        prefer_season_packs=True,
        auto_advance_seasons=False,
    )
    db_session.add(anime)
    await db_session.flush()

    # Season 1 (TV season, 2 episodes)
    s1 = Season(
        media_item_id=anime.id,
        season_number=1,
        watch_order=1,
        type_number=1,
        entry_type="season",
        title="Demon Slayer",
        episode_count=2,
        status=SeasonStatus.SEARCHING,
    )
    # Entry 2: Franchise Movie (entry_type == "movie")
    m1 = Season(
        media_item_id=anime.id,
        season_number=2,
        watch_order=2,
        type_number=1,
        entry_type="movie",
        title="Demon Slayer Mugen Train",
        episode_count=1,
        status=SeasonStatus.PENDING,
    )
    db_session.add_all([s1, m1])
    await db_session.flush()

    ep1 = Episode(
        season_id=s1.id,
        episode_number=1,
        status=EpisodeStatus.SEARCHING,
    )
    ep2 = Episode(
        season_id=s1.id,
        episode_number=2,
        status=EpisodeStatus.SEARCHING,
    )
    db_session.add_all([ep1, ep2])
    await db_session.commit()

    gb = 1024 * 1024 * 1024

    async def fake_search_show(
        tvdb_id=None,
        tmdb_id=None,
        imdb_id=None,
        title=None,
        season=None,
        ep=None,
        category=None,
        **kwargs,
    ):
        # Return empty when searching for whole season pack (ep is None)
        if ep is None:
            return []
        ep_num = int(str(ep))
        return [
            {
                "title": f"Demon.Slayer.S01E{ep_num:02d}.1080p.WEB-DL.x264-SUBS",
                "size": int(1.2 * gb),
                "guid": f"guid-s01e{ep_num:02d}",
            }
        ]

    mock_search_movie = AsyncMock(
        return_value=[
            {
                "title": "Demon.Slayer.Mugen.Train.2020.1080p.BluRay.x265-GRP",
                "size": 6 * gb,
                "guid": "guid-mugen-train-movie",
            }
        ]
    )

    tb_counter = 0

    async def fake_send_nzb(*args, **kwargs):
        nonlocal tb_counter
        tb_counter += 1
        return {"id": f"tb-{tb_counter}", "hash": f"hash-{tb_counter}"}

    with (
        patch(
            "app.services.indexers.newznab.search_show",
            side_effect=fake_search_show,
        ),
        patch(
            "app.services.indexers.newznab.search_movie",
            new=mock_search_movie,
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(
                return_value=(
                    b'<nzb><file subject="video.mkv"></file></nzb>',
                    "video.nzb",
                )
            ),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            side_effect=fake_send_nzb,
        ),
    ):
        resp = await async_client.post(
            f"/api/items/{anime.id}/push/auto",
            json={
                "season_ids": [s1.id, m1.id],
                "prefer_season_packs": True,
                "auto_advance_seasons": False,
            },
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "started"
        assert payload["pushed"] is True
        await wait_for_background_pushes()

    # Verify movie category search was used for m1 (`entry_type == "movie"`)
    assert mock_search_movie.await_count >= 1
    movie_call_kwargs = mock_search_movie.await_args.kwargs
    assert movie_call_kwargs.get("category") == "2000,2040,2045,2050,2060"

    # Verify 3 downloads recorded: S01E01, S01E02 (via pack-to-episode fallback), and Movie 1
    hist_rows = (
        (
            await db_session.execute(
                select(DownloadHistory)
                .where(DownloadHistory.media_item_id == anime.id)
                .order_by(DownloadHistory.id.asc())
            )
        )
        .scalars()
        .all()
    )
    assert len(hist_rows) == 3
    assert [h.nzb_guid for h in hist_rows] == [
        "guid-s01e01",
        "guid-s01e02",
        "guid-mugen-train-movie",
    ]
    assert all(h.push_mode == "auto" for h in hist_rows)
    assert hist_rows[0].episode_id == ep1.id
    assert hist_rows[1].episode_id == ep2.id
    assert hist_rows[2].season_id == m1.id
    assert hist_rows[2].episode_id is None


@pytest.mark.asyncio
async def test_manual_search_and_manual_grab_endpoints(
    db_session,
    async_client: AsyncClient,
) -> None:
    """
    Verify:
    1. `POST /api/items/{item_id}/push/manual-search` returns scored candidates across
       both Season Packs/Movies (`pack_or_movie_releases`) and per-episode accordions
       (`episode_accordions`) partitioned into Primary, Fallback, and Mismatched languages.
    2. `POST /api/items/{item_id}/push/manual-grab` dispatches the chosen candidate to
       TorBox and records `DownloadHistory.push_mode = "manual"`.
    """
    provider = Provider(
        name="Simkl",
        type="simkl",
        category=ProviderCategory.WATCHLIST.value,
        is_active=True,
    )
    indexer = Provider(
        name="SeriesIdx",
        type="newznab",
        category=ProviderCategory.INDEXER.value,
        api_url="https://idx.example/api",
        api_key="idx-key",
        priority=1,
        is_active=True,
    )
    db_session.add_all([provider, indexer])
    await db_session.flush()

    show = MediaItem(
        provider_id=provider.id,
        simkl_id=9001,
        tvdb_id=9001,
        title="Severance",
        year=2022,
        media_type=MediaType.SHOW,
        status=MediaStatus.SEARCHING,
    )
    db_session.add(show)
    await db_session.flush()

    s1 = Season(
        media_item_id=show.id,
        season_number=1,
        watch_order=1,
        type_number=1,
        entry_type="season",
        episode_count=2,
        status=SeasonStatus.SEARCHING,
    )
    db_session.add(s1)
    await db_session.flush()

    ep1 = Episode(
        season_id=s1.id,
        episode_number=1,
        status=EpisodeStatus.SEARCHING,
    )
    db_session.add(ep1)
    await db_session.commit()

    gb = 1024 * 1024 * 1024

    async def fake_search_show(season=None, ep=None, **kwargs):
        if ep is None:
            return [
                {
                    "title": "Severance.S01.German.1080p.WEB-DL.H264-GRP",
                    "size": 12 * gb,
                    "guid": "guid-pack-de",
                },
                {
                    "title": "Severance.S01.1080p.WEB-DL.H264-ENG",
                    "size": 11 * gb,
                    "guid": "guid-pack-en",
                },
                {
                    "title": "Severance.S01.French.1080p.WEB-DL.H264-FR",
                    "size": 10 * gb,
                    "guid": "guid-pack-fr",
                },
            ]
        return [
            {
                "title": "Severance.S01E01.German.1080p.WEB-DL.H264-GRP",
                "size": int(1.5 * gb),
                "guid": "guid-ep1-de",
            }
        ]

    with patch(
        "app.services.indexers.newznab.search_show",
        side_effect=fake_search_show,
    ):
        search_resp = await async_client.post(
            f"/api/items/{show.id}/push/manual-search",
            json={
                "season_ids": [s1.id],
                "episode_ids": [ep1.id],
                "primary_language": "de",
                "fallback_language": "en",
                "video_quality_mode": "best",
                "audio_quality_mode": "best",
                "allow_season_packs": True,
            },
            headers={"Accept": "application/json"},
        )
        assert search_resp.status_code == 200
        search_data = search_resp.json()

    # Check Season Packs 3-tier language partitioning
    packs = search_data["pack_or_movie_releases"]
    assert len(packs["primary"]) == 1
    assert packs["primary"][0]["guid"] == "guid-pack-de"
    assert len(packs["fallback"]) == 1
    assert packs["fallback"][0]["guid"] == "guid-pack-en"
    assert len(packs["mismatched"]) == 1
    assert packs["mismatched"][0]["guid"] == "guid-pack-fr"

    # Check Episode Accordion results
    accordions = search_data["episode_accordions"]
    assert len(accordions) == 1
    assert accordions[0]["episode_id"] == ep1.id
    assert len(accordions[0]["primary"]) == 1
    assert accordions[0]["primary"][0]["guid"] == "guid-ep1-de"

    # Now perform a 1-click manual grab of the Episode release
    with (
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(
                return_value=(
                    b'<nzb><file subject="Severance.S01E01.mkv"></file></nzb>',
                    "severance.nzb",
                )
            ),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new=AsyncMock(return_value={"id": "tb-manual-1", "hash": "hash-manual-1"}),
        ),
    ):
        grab_resp = await async_client.post(
            f"/api/items/{show.id}/push/manual-grab",
            json={
                "guid": "guid-ep1-de",
                "title": "Severance.S01E01.German.1080p.WEB-DL.H264-GRP",
                "size_bytes": int(1.5 * gb),
                "score": 4500.0,
                "season_id": s1.id,
                "episode_id": ep1.id,
                "matched_language": "de",
                "is_fallback": False,
            },
            headers={"Accept": "application/json"},
        )
        assert grab_resp.status_code == 200
        grab_data = grab_resp.json()
        assert grab_data["pushed"] is True
        assert grab_data["push_mode"] == "manual"

    hist_stmt = select(DownloadHistory).where(DownloadHistory.episode_id == ep1.id)
    dh = (await db_session.execute(hist_stmt)).scalars().first()
    assert dh is not None
    assert dh.push_mode == "manual"
    assert dh.nzb_guid == "guid-ep1-de"
    assert dh.downloader_transfer_id == "tb-manual-1"
    assert dh.grabbed_language == "de"

    await db_session.refresh(ep1)
    assert ep1.status == EpisodeStatus.DOWNLOADING


@pytest.mark.asyncio
async def test_push_modal_context_and_movie_bridge_lookahead(
    db_session,
    async_client: AsyncClient,
) -> None:
    """
    Verify:
    1. `GET /api/items/{item_id}/push-modal` returns chronological entries (seasons & movies),
       saved presets, and effective search config.
    2. Pushing Season 1 with `auto_advance_seasons = True` triggers Movie Bridge Lookahead:
       since chronological entry 2 is `Movie 1` (`entry_type == "movie"`), it automatically
       pushes both `Movie 1` AND `Season 2` (`entry_type == "season"`, `type_number == 2`).
    """
    provider = Provider(
        name="Simkl",
        type="simkl",
        category=ProviderCategory.WATCHLIST.value,
        is_active=True,
    )
    indexer = Provider(
        name="Idx",
        type="newznab",
        category=ProviderCategory.INDEXER.value,
        api_url="https://idx.example/api",
        api_key="idx-key",
        priority=1,
        is_active=True,
    )
    db_session.add_all([provider, indexer])
    await db_session.flush()

    anime = MediaItem(
        provider_id=provider.id,
        simkl_id=9500,
        anilist_id=101922,
        title="Demon Slayer",
        year=2019,
        media_type=MediaType.ANIME,
        status=MediaStatus.SEARCHING,
        release_date=datetime(2019, 4, 6, tzinfo=timezone.utc),
    )
    db_session.add(anime)
    await db_session.flush()

    s1 = Season(
        media_item_id=anime.id,
        season_number=1,
        watch_order=1,
        type_number=1,
        entry_type="season",
        title="Demon Slayer",
        episode_count=26,
        air_date=datetime(2019, 4, 6, tzinfo=timezone.utc),
        status=SeasonStatus.SEARCHING,
    )
    m1 = Season(
        media_item_id=anime.id,
        season_number=2,
        watch_order=2,
        type_number=1,
        entry_type="movie",
        title="Demon Slayer Mugen Train",
        episode_count=1,
        air_date=datetime(2020, 10, 16, tzinfo=timezone.utc),
        status=SeasonStatus.PENDING,
    )
    s2 = Season(
        media_item_id=anime.id,
        season_number=3,
        watch_order=3,
        type_number=2,
        entry_type="season",
        title="Demon Slayer Entertainment District",
        episode_count=11,
        air_date=datetime(2021, 12, 5, tzinfo=timezone.utc),
        status=SeasonStatus.PENDING,
    )
    db_session.add_all([s1, m1, s2])
    await db_session.commit()

    gb = 1024 * 1024 * 1024

    modal_resp = await async_client.get(
        f"/api/items/{anime.id}/push-modal",
        headers={"Accept": "application/json"},
    )
    assert modal_resp.status_code == 200
    modal_data = modal_resp.json()
    assert modal_data["item"]["id"] == anime.id
    assert [
        (e["watch_order"], e["entry_type"], e["type_number"])
        for e in modal_data["entries"]
    ] == [
        (1, "season", 1),
        (2, "movie", 1),
        (3, "season", 2),
    ]

    async def fake_search_show(season=None, ep=None, **kwargs):
        return [
            {
                "title": f"Demon.Slayer.S{int(season):02d}.1080p.BluRay.x265-GRP",
                "size": 15 * gb,
                "guid": f"guid-pack-s{int(season):02d}",
            }
        ]

    async def fake_search_movie(**kwargs):
        return [
            {
                "title": "Demon.Slayer.Mugen.Train.2020.1080p.BluRay.x265-GRP",
                "size": 6 * gb,
                "guid": "guid-movie-mugen-train",
            }
        ]

    tb_seq = 0

    async def fake_send_nzb(*args, **kwargs):
        nonlocal tb_seq
        tb_seq += 1
        return {"id": f"tb-bridge-{tb_seq}", "hash": f"hash-bridge-{tb_seq}"}

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.search_movie", side_effect=fake_search_movie
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(
                return_value=(b'<nzb><file subject="video.mkv"></file></nzb>', "v.nzb")
            ),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file", side_effect=fake_send_nzb
        ),
    ):
        push_resp = await async_client.post(
            f"/api/items/{anime.id}/push/auto",
            json={
                "season_ids": [s1.id],
                "allow_season_packs": True,
                "prefer_season_packs": True,
                "auto_advance_seasons": True,
            },
            headers={"Accept": "application/json"},
        )
        assert push_resp.status_code == 200
        push_data = push_resp.json()
        assert push_data["status"] == "started"
        assert push_data["pushed"] is True
        await wait_for_background_pushes()

    hist_rows = (
        (
            await db_session.execute(
                select(DownloadHistory)
                .where(DownloadHistory.media_item_id == anime.id)
                .order_by(DownloadHistory.id.asc())
            )
        )
        .scalars()
        .all()
    )
    assert [h.nzb_guid for h in hist_rows] == [
        "guid-pack-s01",
        "guid-movie-mugen-train",
        "guid-pack-s02",
    ]


@pytest.mark.asyncio
async def test_background_auto_push_holds_lock_registers_in_flight_and_cleans_up(
    db_session,
    async_client: AsyncClient,
) -> None:
    """
    Verify:
    1. `POST /api/items/{item_id}/push/auto` returns `200 {"status": "started", ...}` immediately,
       registers the item in `transfer_poller.in_flight_pushes`, wakes the poller, and retains
       `item_lock_manager` ownership while the background task is running (`409` on concurrent push).
    2. When the background task finishes, `in_flight_pushes` is unregistered and the lock is released.
    """
    import asyncio

    from app.core.transfer_poller import transfer_poller

    provider = Provider(
        name="Simkl",
        type="simkl",
        category=ProviderCategory.WATCHLIST.value,
        is_active=True,
    )
    indexer = Provider(
        name="Idx",
        type="newznab",
        category=ProviderCategory.INDEXER.value,
        api_url="https://idx.example/api",
        api_key="idx-key",
        priority=1,
        is_active=True,
    )
    db_session.add_all([provider, indexer])
    await db_session.flush()

    movie = MediaItem(
        provider_id=provider.id,
        simkl_id=9101,
        title="Blade Runner 2049",
        year=2017,
        media_type=MediaType.MOVIE,
        status=MediaStatus.SEARCHING,
    )
    db_session.add(movie)
    await db_session.commit()

    search_started = asyncio.Event()
    allow_search_finish = asyncio.Event()

    async def slow_search_movie(**kwargs):
        search_started.set()
        await allow_search_finish.wait()
        return [
            {
                "title": "Blade.Runner.2049.2017.1080p.BluRay.x264-GRP",
                "size": 8 * 1024 * 1024 * 1024,
                "guid": "guid-br2049",
            }
        ]

    with (
        patch(
            "app.services.indexers.newznab.search_movie", side_effect=slow_search_movie
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(
                return_value=(
                    b'<nzb><file subject="br2049.mkv"></file></nzb>',
                    "br.nzb",
                )
            ),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new=AsyncMock(return_value={"id": "tb-br-1", "hash": "hash-br-1"}),
        ),
    ):
        resp = await async_client.post(
            f"/api/items/{movie.id}/push/auto",
            json={"prefer_season_packs": True},
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "started"
        assert data["pushed"] is True
        assert data["item_id"] == movie.id
        assert data["title"] == "Blade Runner 2049"

        await asyncio.wait_for(search_started.wait(), timeout=2.0)

        # While background task is in flight: lock is held and in_flight_pushes has movie.id
        assert item_lock_manager.is_locked(movie.id) is True
        assert movie.id in transfer_poller.in_flight_pushes
        assert "Blade Runner 2049" in transfer_poller.in_flight_titles

        # Concurrent push request while background task is running returns 409 Conflict
        conflict_resp = await async_client.post(
            f"/api/items/{movie.id}/push/auto",
            json={},
            headers={"Accept": "application/json"},
        )
        assert conflict_resp.status_code == 409

        allow_search_finish.set()
        await wait_for_background_pushes()

    assert item_lock_manager.is_locked(movie.id) is False
    assert movie.id not in transfer_poller.in_flight_pushes


@pytest.mark.asyncio
async def test_auto_push_zero_results_and_exhausted_candidates_record_visible_failed_history(
    db_session,
    async_client: AsyncClient,
) -> None:
    """
    Verify:
    1. When an Auto-Push target finds 0 raw results on indexers (`no_indexer_results`),
       or all results are rejected by quality/language filters, or all candidate uploads fail,
       `execute_auto_push` records a visible `DownloadHistory` row (`push_mode == "auto"`,
       `torbox_id is None`, `is_dismissed is False`, `status_detail.startswith("failed:")`)
       and transitions the target and parent `MediaItem` to `FAILED`.
    2. Re-running Auto-Push on the same failed target dismisses the prior synthetic failed row
       so duplicate failed rows do not accumulate in Card 5 (`Active Pushes`).
    3. `get_active_pushes(db_session)` surfaces the failed Auto-Push row as `Failed (Auto-Push)`.
    """
    from app.core.push_engine import execute_auto_push
    from app.core.transfer_poller import dismiss_failed_push, get_active_pushes

    provider = Provider(
        name="Simkl",
        type="simkl",
        category=ProviderCategory.WATCHLIST.value,
        is_active=True,
    )
    indexer = Provider(
        name="Idx",
        type="newznab",
        category=ProviderCategory.INDEXER.value,
        api_url="https://idx.example/api",
        api_key="idx-key",
        priority=1,
        is_active=True,
    )
    preset_4k_only = SearchPreset(
        name="Strict 4K Only",
        is_default=False,
        primary_language="en",
        fallback_language="",
        video_quality_mode="custom",
        audio_quality_mode="best",
        custom_config_json='{"resolutions": ["2160p"]}',
    )
    db_session.add_all([provider, indexer, preset_4k_only])
    await db_session.flush()

    # 1. Movie that previously completed, now re-pushed with 0 indexer results
    movie = MediaItem(
        provider_id=provider.id,
        simkl_id=9201,
        title="Rare Indie Film",
        year=2024,
        media_type=MediaType.MOVIE,
        status=MediaStatus.COMPLETED,
    )
    db_session.add(movie)
    await db_session.flush()
    # Add historical completed row to verify failed re-push takes precedence over historical completion
    old_completed = DownloadHistory(
        media_item_id=movie.id,
        nzb_guid="guid-old-completed",
        nzb_title="Rare.Indie.Film.2024.720p",
        downloader_transfer_id="tb-old-completed",
        status_detail="completed",
        push_mode="auto",
        is_dismissed=False,
    )
    db_session.add(old_completed)
    await db_session.commit()

    with patch(
        "app.services.indexers.newznab.search_movie", new=AsyncMock(return_value=[])
    ):
        res1 = await execute_auto_push(db_session, movie.id, {})
    assert res1["pushed"] is False

    await db_session.refresh(movie)
    assert movie.status == MediaStatus.FAILED

    active_rows = await get_active_pushes(db_session)
    movie_failed_rows = [r for r in active_rows if r["media_item_id"] == movie.id]
    assert len(movie_failed_rows) == 1
    assert movie_failed_rows[0]["status"] == "Failed (Auto-Push)"
    assert (
        movie_failed_rows[0]["status_detail"]
        == "failed: No matching releases found on indexers"
    )
    first_failed_history_id = movie_failed_rows[0]["history_id"]

    # 2. Re-push the same movie where indexer returns a 1080p release that is rejected by 4K filter:
    #    Prior synthetic failed row must be auto-dismissed and replaced by a single new row!
    with patch(
        "app.services.indexers.newznab.search_movie",
        new=AsyncMock(
            return_value=[
                {
                    "title": "Rare.Indie.Film.2024.1080p.WEB-DL.x264-GRP",
                    "size": 4 * 1024 * 1024 * 1024,
                    "guid": "guid-1080p-rejected",
                }
            ]
        ),
    ):
        res2 = await execute_auto_push(
            db_session, movie.id, {"preset_id": preset_4k_only.id}
        )
    assert res2["pushed"] is False

    first_row = await db_session.get(DownloadHistory, first_failed_history_id)
    assert first_row is not None
    assert first_row.is_dismissed is True

    active_rows_after = await get_active_pushes(db_session)
    movie_failed_rows_after = [
        r for r in active_rows_after if r["media_item_id"] == movie.id
    ]
    assert len(movie_failed_rows_after) == 1
    assert (
        movie_failed_rows_after[0]["status_detail"]
        == "failed: All indexer results rejected by quality/language filters"
    )

    # 3. Episode Auto-Push where TorBox upload fails for all candidates
    show = MediaItem(
        provider_id=provider.id,
        simkl_id=9202,
        title="Andor",
        year=2022,
        media_type=MediaType.SHOW,
        status=MediaStatus.SEARCHING,
    )
    db_session.add(show)
    await db_session.flush()
    s1 = Season(
        media_item_id=show.id,
        season_number=1,
        watch_order=1,
        type_number=1,
        entry_type="season",
        episode_count=1,
        status=SeasonStatus.SEARCHING,
    )
    db_session.add(s1)
    await db_session.flush()
    ep1 = Episode(
        season_id=s1.id,
        episode_number=1,
        status=EpisodeStatus.SEARCHING,
    )
    db_session.add(ep1)
    await db_session.commit()

    with (
        patch(
            "app.services.indexers.newznab.search_show",
            new=AsyncMock(
                return_value=[
                    {
                        "title": "Andor.S01E01.1080p.WEB-DL.x264-GRP",
                        "size": 2 * 1024 * 1024 * 1024,
                        "guid": "guid-andor-s01e01",
                    }
                ]
            ),
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(
                return_value=(b'<nzb><file subject="andor.mkv"></file></nzb>', "a.nzb")
            ),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new=AsyncMock(return_value=None),
        ),
    ):
        res3 = await execute_auto_push(db_session, show.id, {"episode_ids": [ep1.id]})
    assert res3["pushed"] is False

    await db_session.refresh(show)
    await db_session.refresh(s1)
    await db_session.refresh(ep1)
    assert ep1.status == EpisodeStatus.FAILED
    assert s1.status == SeasonStatus.FAILED
    assert show.status == MediaStatus.FAILED

    active_rows_ep = await get_active_pushes(db_session)
    ep_failed_rows = [r for r in active_rows_ep if r["episode_id"] == ep1.id]
    assert len(ep_failed_rows) == 1
    assert ep_failed_rows[0]["status"] == "Failed (Auto-Push)"
    assert (
        ep_failed_rows[0]["status_detail"]
        == "failed: downloader upload failed — all candidates exhausted"
    )

    # Dismissing the failed episode push restores episode, season, and show to SEARCHING
    dismissed = await dismiss_failed_push(db_session, ep_failed_rows[0]["history_id"])
    assert dismissed is True
    await db_session.refresh(show)
    await db_session.refresh(s1)
    await db_session.refresh(ep1)
    assert ep1.status == EpisodeStatus.SEARCHING
    assert s1.status == SeasonStatus.SEARCHING
    assert show.status == MediaStatus.SEARCHING


# --- m50/02: first_pushed_at stamping & replaces_history_id plumbing ---


async def _dispatch_real(db_session, item, season=None, episode=None, **kw):
    from app.core.push_engine import _dispatch_candidate_list_to_downloader
    from app.services.downloaders import torbox
    from app.services.indexers import newznab as newznab_client
    from tests.conftest import VALID_NZB_BYTES

    cand = {
        "guid": "g-1",
        "title": "Show.S01E01.1080p.WEB-DL.x264-GRP",
        "score": 9000.0,
    }
    with (
        patch.object(
            newznab_client,
            "fetch_nzb_bytes",
            new=AsyncMock(return_value=(VALID_NZB_BYTES, "x.nzb")),
        ),
        patch.object(
            torbox, "send_nzb_file", new=AsyncMock(return_value={"id": 5, "hash": "h"})
        ),
        patch("app.core.transfer_poller.wake_transfer_poller"),
        patch.object(torbox, "get_cooldown_remaining", return_value=0),
        patch("app.core.push_engine.settings.dry_run", False),
    ):
        return await _dispatch_candidate_list_to_downloader(
            db_session, [cand], item, season=season, episode=episode, **kw
        )


@pytest.mark.asyncio
async def test_single_episode_dispatch_stamps_first_pushed_at_once(db_session) -> None:
    from tests.conftest import make_series

    item, seasons, eps = await make_series(db_session, episodes_per_season=1)
    season = seasons[0]
    await _dispatch_real(db_session, item, season, eps[0])
    first = eps[0].first_pushed_at
    assert first is not None
    await _dispatch_real(db_session, item, season, eps[0])
    assert eps[0].first_pushed_at == first


@pytest.mark.asyncio
async def test_season_pack_dispatch_stamps_non_future_episodes(db_session) -> None:
    from tests.conftest import make_series

    item, seasons, eps = await make_series(
        db_session,
        episodes_per_season=2,
        episode_statuses=[EpisodeStatus.SEARCHING, EpisodeStatus.FUTURE],
    )
    season = seasons[0]
    await db_session.refresh(season, ["episodes"])
    await _dispatch_real(db_session, item, season, None)
    assert eps[0].first_pushed_at is not None
    assert eps[1].first_pushed_at is None


@pytest.mark.asyncio
async def test_upgrade_dispatch_records_replaces_id_and_never_restamps(
    db_session,
) -> None:
    from tests.conftest import make_series

    item, seasons, eps = await make_series(db_session, episodes_per_season=1)
    season = seasons[0]
    hist = await _dispatch_real(
        db_session,
        item,
        season,
        eps[0],
        is_upgrade=True,
        replaces_history_id=42,
        reset_fail_count=False,
    )
    assert hist.replaces_history_id == 42
    assert eps[0].first_pushed_at is None


@pytest.mark.asyncio
async def test_rate_limit_queued_push_does_not_stamp(db_session) -> None:
    from app.core.push_engine import _record_auto_push_failure
    from tests.conftest import make_series

    item, seasons, eps = await make_series(db_session, episodes_per_season=1)
    season = seasons[0]
    await _record_auto_push_failure(
        db_session,
        item=item,
        season=season,
        episode=eps[0],
        reason="TorBox rate limit exceeded (cooldown: 60s)",
    )
    await db_session.refresh(eps[0])
    assert eps[0].first_pushed_at is None
