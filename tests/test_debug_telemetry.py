import logging
from unittest.mock import AsyncMock, patch

import pytest

from app.core.air_dates import enrich_active_shows_episode_air_dates
from app.core.automation import (
    refresh_due_future_tv_seasons,
    sync_all_providers,
)
from app.db.models import EpisodeStatus, MediaType, SeasonStatus
from tests.conftest import make_series


@pytest.mark.asyncio
async def test_sync_all_providers_emits_phase_milestones(
    db_session, watchlist_provider, caplog
):
    """sync_all_providers logs the Simkl and maintenance milestone phases as INFO summaries with [SYNC] tags."""
    watchlist_provider.client_id = "test_cid"
    watchlist_provider.access_token = "test_tok"
    await db_session.commit()

    caplog.set_level(logging.DEBUG)

    with (
        patch(
            "app.services.watchlists.simkl_adapter.simkl._fetch_simkl_category_with_retry"
        ) as mock_fetch,
        patch(
            "app.services.watchlists.simkl_adapter.simkl.ensure_valid_simkl_token",
            new_callable=AsyncMock,
        ) as mock_tok,
    ):
        mock_tok.return_value = ("test_cid", "test_tok")
        mock_fetch.return_value = ([], "test_cid", "test_tok")

        await sync_all_providers()

    info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
    for marker in (
        "[SYNC] [1/3] Simkl: fetched 0 entries",
        "[SYNC] [2/3] Simkl: ingested 0",
        "[SYNC] [3/3] Simkl: 0 departed entries marked completed",
        "[SYNC] [MAINT] [1/4]",
        "[SYNC] [MAINT] [2/4]",
        "[SYNC] [MAINT] [3/4]",
        "[SYNC] [MAINT] [4/4]",
    ):
        assert any(marker in m for m in info_messages), marker


@pytest.mark.asyncio
async def test_debug_progress_suppressed_at_info_level(
    db_session, watchlist_provider, caplog
):
    """When caplog level is INFO, per-item [SYNC] progress lines are absent."""
    watchlist_provider.client_id = "test_cid"
    watchlist_provider.access_token = "test_tok"
    await db_session.commit()

    caplog.set_level(logging.INFO)

    with (
        patch(
            "app.services.watchlists.simkl_adapter.simkl._fetch_simkl_category_with_retry"
        ) as mock_fetch,
        patch(
            "app.services.watchlists.simkl_adapter.simkl.ensure_valid_simkl_token",
            new_callable=AsyncMock,
        ) as mock_tok,
    ):
        mock_tok.return_value = ("test_cid", "test_tok")
        mock_fetch.return_value = ([], "test_cid", "test_tok")

        await sync_all_providers()

    info_messages = [r.message for r in caplog.records]
    assert not any("[SYNC] [2/3] (" in m or "Ingesting" in m for m in info_messages)


@pytest.mark.asyncio
async def test_enrich_active_shows_paced_counters(
    db_session, watchlist_provider, caplog
):
    """enrich_active_shows_episode_air_dates emits candidate counts, per-item queries, updates, and summary."""
    watchlist_provider.client_id = "test_cid"
    watchlist_provider.access_token = "test_tok"
    await db_session.commit()

    item, seasons, episodes = await make_series(
        db_session,
        title="Frieren: Beyond Journey's End",
        simkl_id=55501,
        provider_id=watchlist_provider.id,
        seasons=[{"season_number": 1, "status": SeasonStatus.FUTURE}],
        episodes_per_season=2,
        episode_statuses=[EpisodeStatus.FUTURE, EpisodeStatus.FUTURE],
    )

    simkl_payload = [
        {"season": 1, "episode": 1, "date": "2026-10-15T20:00:00-04:00"},
        {"season": 1, "episode": 2, "date": "2026-10-22T20:00:00-04:00"},
    ]

    caplog.set_level(logging.DEBUG)

    with patch(
        "app.services.watchlists.simkl.get_episodes",
        new_callable=AsyncMock,
        return_value=simkl_payload,
    ):
        count = await enrich_active_shows_episode_air_dates(db_session)
        assert count == 2

    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]

    assert any(
        "[SYNC] [3/5] Air date refresh: checked=1 skipped(frozen)=0 skipped(paced)=0."
        in m
        for m in debug_messages
    )
    assert any(
        "[SYNC] [3/5] (1/1) Querying Simkl episodes for 'Frieren: Beyond Journey's End' (Simkl ID: 55501)..."
        in m
        for m in debug_messages
    )
    assert any(
        "[SYNC] [3/5] (1/1) 'Frieren: Beyond Journey's End': 2 episode air dates updated."
        in m
        for m in debug_messages
    )
    assert not any("Air date enrichment complete" in m for m in debug_messages)


@pytest.mark.asyncio
async def test_pruning_logging(db_session, watchlist_provider, caplog):
    """prune_inactive_media_items emits the expected [SYNC] debug log."""
    from tests.conftest import ingest_simkl_payloads

    watchlist_provider.client_id = "test_cid"
    watchlist_provider.access_token = "test_tok"
    await db_session.commit()

    caplog.set_level(logging.DEBUG)

    simkl_movies = [
        {
            "movie": {
                "title": "Inception",
                "year": 2010,
                "ids": {"simkl": 12345, "tmdb": "27205"},
            }
        }
    ]

    await ingest_simkl_payloads(
        db_session, watchlist_provider, simkl_movies, MediaType.MOVIE, maintain=False
    )

    # Simkl list departure marks the source completed, then prune removes the item
    from app.services.watchlist_ingest import (
        mark_absent_sources_completed,
        prune_inactive_media_items,
    )

    await mark_absent_sources_completed(db_session, watchlist_provider, set())
    pruned = await prune_inactive_media_items(db_session)
    assert pruned == 1

    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(
        "[SYNC] [MAINT] [4/4] Pruned 'Inception' (no longer in active watchlists, 0 active downloads)."
        in m
        for m in debug_messages
    )


@pytest.mark.asyncio
async def test_refresh_due_future_tv_seasons_logging(
    db_session, watchlist_provider, caplog
):
    """refresh_due_future_tv_seasons emits evaluation count and per-item query progress."""
    item, seasons, episodes = await make_series(
        db_session,
        title="House of the Dragon",
        simkl_id=8888,
        provider_id=watchlist_provider.id,
        seasons=[{"season_number": 3, "status": SeasonStatus.FUTURE}],
        episodes_per_season=0,
    )
    item.tmdb_id = 94997
    item.last_metadata_refreshed_at = None
    seasons[0].air_date = None
    await db_session.commit()

    caplog.set_level(logging.DEBUG)

    with (
        patch(
            "app.services.tmdb.get_show_details", new_callable=AsyncMock
        ) as mock_show,
        patch(
            "app.services.tmdb.get_season_details", new_callable=AsyncMock
        ) as mock_season,
    ):
        mock_show.return_value = {
            "seasons": [
                {"season_number": 1},
                {"season_number": 2},
                {"season_number": 3},
            ],
            "number_of_seasons": 3,
        }
        mock_season.return_value = {
            "season_number": 3,
            "air_date": "2026-06-01",
            "episodes": [],
        }

        await refresh_due_future_tv_seasons(db_session)

    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(
        "[SYNC] [MAINT] [2/4] Evaluating 1 TV show(s) with TBA/FUTURE seasons for TMDB updates..."
        in m
        for m in debug_messages
    )
    assert any(
        "[SYNC] [MAINT] [2/4] (1/1) Checking TMDB season updates for 'House of the Dragon'..."
        in m
        for m in debug_messages
    )


@pytest.mark.asyncio
@pytest.mark.needs_downloader
async def test_search_and_fakedetect_telemetry(db_session, caplog):
    """push_engine and fake_detector emit [SEARCH] and [FAKEDETECT] debug logs."""
    from app.core.fake_detector import (
        is_indexer_metadata_fake,
        is_nzb_content_fake,
        is_transfer_filelist_fake,
    )
    from app.core.push_engine import (
        _dispatch_candidate_list_to_downloader,
        _query_movie_across_indexers,
        _score_and_partition_candidates,
    )
    from tests.conftest import VALID_NZB_BYTES, make_movie

    caplog.set_level(logging.DEBUG)

    # 1. FAKEDETECT L1, L2, L3
    is_indexer_metadata_fake(
        {"title": "Test.Release.2024", "size": 1000},
        parsed_resolution="1080p",
        media_type="movie",
    )
    is_nzb_content_fake(VALID_NZB_BYTES, media_type="movie")
    is_transfer_filelist_fake(
        [{"name": "Test.Release.mkv", "size": 500 * 1024 * 1024}],
        media_type="movie",
        release_name="Test.Release.mkv",
    )

    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(
        "[FAKEDETECT] [L1] Release 'Test.Release.2024' rejected:" in m
        for m in debug_messages
    )
    assert any(
        "[FAKEDETECT] [L2] Inspecting NZB payload -> passed" in m
        for m in debug_messages
    )
    assert any(
        "[FAKEDETECT] [L3] Inspecting filelist for 'Test.Release.mkv' -> verified clean"
        in m
        for m in debug_messages
    )

    # 2. SEARCH in query, scoring, and dispatch
    movie = await make_movie(db_session, title="Searchable Movie")
    with patch(
        "app.services.indexers.newznab.search_movie", new_callable=AsyncMock
    ) as mock_sm:
        mock_sm.return_value = [
            {
                "title": "Searchable.Movie.2024.1080p.WEB-DL-GRP",
                "guid": "g1",
                "size": 2 * 1024**3,
            }
        ]
        results = await _query_movie_across_indexers(db_session, movie)
        assert len(results) == 1

        partitioned = await _score_and_partition_candidates(
            raw_results=results,
            blacklisted_guids=set(),
            blacklisted_titles=set(),
            expected_title=movie.title,
            expected_year=movie.year,
            expected_alt_title=None,
            expected_season=None,
            expected_episode=None,
            expected_season_title=None,
            runtime_minutes=120,
            effective_cfg={
                "primary_language": "en",
                "video_quality_mode": "best",
                "audio_quality_mode": "best",
            },
            media_type="movie",
        )
        assert len(partitioned["all_valid"]) == 1

    with (
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes", new_callable=AsyncMock
        ) as mock_fetch,
        patch(
            "app.services.downloaders.torbox.send_nzb_file", new_callable=AsyncMock
        ) as mock_tb,
    ):
        mock_fetch.return_value = (VALID_NZB_BYTES, "test.nzb")
        mock_tb.return_value = {"id": 12345, "hash": "abc"}
        await _dispatch_candidate_list_to_downloader(
            db_session,
            partitioned["all_valid"],
            item=movie,
            push_mode="auto",
        )

    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(
        "[SEARCH] Searching indexer 'TreasureMaps' for 'Searchable Movie'" in m
        for m in debug_messages
    )
    assert any(
        "[SEARCH] Indexer 'TreasureMaps' returned 1 raw candidates." in m
        for m in debug_messages
    )
    assert any(
        "[SEARCH] Filtered 1 valid candidates (0 rejected by Layer 1, 0 language mismatches)."
        in m
        for m in debug_messages
    )
    assert any(
        "[SEARCH] Top candidate selected: 'Searchable.Movie.2024.1080p.WEB-DL-GRP'" in m
        for m in debug_messages
    )
    assert any(
        "[SEARCH] Dispatching 'Searchable.Movie.2024.1080p.WEB-DL-GRP' to the downloader (Push Mode: auto, Season Pack: False)..."
        in m
        for m in debug_messages
    )
    assert any(
        "[SEARCH] Dispatched successfully to the downloader -> Transfer ID: 12345." in m
        for m in debug_messages
    )


@pytest.mark.asyncio
async def test_poller_telemetry_active_and_idle_silence(db_session, caplog):
    """Poller tick emits [POLLER] debug logs when active, and exactly 0 debug logs when idle."""
    from app.core.transfer_poller import run_transfer_poller_tick, transfer_poller
    from app.db.models import MediaStatus
    from tests.conftest import make_history, make_movie

    caplog.set_level(logging.DEBUG)

    # 1. Idle tick (0 downloading entities, 0 in-flight pushes)
    transfer_poller.in_flight_pushes.clear()
    caplog.clear()
    res = await run_transfer_poller_tick(db_session)
    assert res["active_count"] == 0

    poller_debugs = [
        r.message
        for r in caplog.records
        if r.levelno == logging.DEBUG and "[POLLER]" in r.message
    ]
    assert len(poller_debugs) == 0

    # 2. Active tick with downloading entity
    movie = await make_movie(
        db_session, title="Polled Movie", status=MediaStatus.DOWNLOADING
    )
    await make_history(
        db_session,
        item=movie,
        downloader_transfer_id="777",
        status_detail="downloading",
        is_dismissed=False,
    )
    await db_session.commit()

    raw_downloads = [
        {
            "id": 777,
            "name": "Polled.Movie.2024.1080p.mkv",
            "download_state": "downloading",
            "progress": 42.5,
            "download_speed": 10 * 1024 * 1024,
            "eta": 120,
        }
    ]

    caplog.clear()
    res2 = await run_transfer_poller_tick(
        db_session, pre_fetched_downloads=raw_downloads
    )
    assert res2["active_count"] == 1

    poller_debugs = [
        r.message
        for r in caplog.records
        if r.levelno == logging.DEBUG and "[POLLER]" in r.message
    ]
    assert any(
        "[POLLER] Tick executing: 1 active downloading transfer(s), 0 in-flight dispatch(es)."
        in m
        for m in poller_debugs
    )
    assert any(
        "[POLLER] Transfer #777 ('Test.Release.2024.1080p.WEB-DL.H264-GRP'): 42.5% | Speed: 10.0 MB/s | ETA: 120s | Status: downloading"
        in m
        for m in poller_debugs
    )

    # 3. Active tick with unpack grace window and completion
    unpack_downloads = [
        {
            "id": 777,
            "name": "Polled.Movie.2024.1080p.mkv",
            "download_state": "completed",
            "download_finished": True,
            "files": [{"name": "archive.part01.rar", "size": 1000}],
        }
    ]
    caplog.clear()
    await run_transfer_poller_tick(db_session, pre_fetched_downloads=unpack_downloads)
    poller_debugs = [
        r.message
        for r in caplog.records
        if r.levelno == logging.DEBUG and "[POLLER]" in r.message
    ]
    assert any("unpacking archive:" in m for m in poller_debugs)

    clean_downloads = [
        {
            "id": 777,
            "name": "Polled.Movie.2024.1080p.mkv",
            "download_state": "completed",
            "download_finished": True,
            "files": [
                {"name": "Polled.Movie.2024.1080p.mkv", "size": 800 * 1024 * 1024}
            ],
        }
    ]
    caplog.clear()
    await run_transfer_poller_tick(db_session, pre_fetched_downloads=clean_downloads)
    poller_debugs = [
        r.message
        for r in caplog.records
        if r.levelno == logging.DEBUG and "[POLLER]" in r.message
    ]
    assert any(
        "[POLLER] [FAKEDETECT] Transfer #777 verified Layer 3 clean -> Marked COMPLETED."
        in m
        for m in poller_debugs
    )


@pytest.mark.asyncio
async def test_queue_telemetry(db_session, caplog):
    """rate_limit_queue emits [QUEUE] debug logs upon enqueuing, resume check, and dispatch."""
    from app.core.push_engine import _record_auto_push_failure
    from app.core.rate_limit_queue import resume_rate_limited_pushes
    from tests.conftest import make_movie

    caplog.set_level(logging.DEBUG)

    # 1. Enqueue logging
    movie = await make_movie(db_session, title="Rate Limited Title")
    await _record_auto_push_failure(
        db_session,
        item=movie,
        reason="TorBox rate limit exceeded (cooldown: 45s)",
        rate_limited=True,
    )
    await db_session.commit()

    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(
        "[QUEUE] Enqueued push for 'Rate Limited Title' target 'Movie' -> HTTP 429 from TorBox (Cooldown: 0s)."
        in m
        for m in debug_messages
    )

    # 2. Resume check and dispatch logging
    with patch(
        "app.core.push_engine.execute_auto_push", new_callable=AsyncMock
    ) as mock_push:
        mock_push.return_value = {"pushed": True}
        caplog.clear()
        resumed = await resume_rate_limited_pushes()
        assert resumed == 1

    debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any(
        "[QUEUE] Resume check: 1 queued pushes. Downloader cooldown: 0s, Indexer cooldown: 0s."
        in m
        for m in debug_messages
    )
    assert any(
        "[QUEUE] Cooldown expired -> Dispatched queued push #" in m
        and "for 'Rate Limited Title'." in m
        for m in debug_messages
    )
