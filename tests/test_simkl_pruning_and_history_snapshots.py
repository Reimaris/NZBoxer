"""Tests for Ticket 02: DownloadHistory Snapshots, Active Watchlist Retention & Simkl Completed Auto-Pruning."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.automation import classify_v3_status_tier, sync_all_providers
from app.core.push_engine import (
    advance_season_buffer,
    build_history_target_label,
    execute_auto_push,
    execute_manual_grab,
)
from app.db.database import close_db, init_db
from app.db.models import (
    Base,
    DownloadHistory,
    Episode,
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    MediaType,
    Provider,
    Season,
    SeasonStatus,
    SystemSettings,
)
from tests.conftest import seed_ready_setup


@pytest.fixture
def snapshot_db(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[None, async_sessionmaker[AsyncSession]]:
    """Return the shared session factory for history snapshots and Simkl pruning tests."""
    return None, session_factory


def test_build_history_target_label():
    """Verify target label formatting across Movie, Season Pack, Anime Movie entry, and Episode."""
    movie = MediaItem(
        provider_id=1,
        title="Inception",
        year=2010,
        media_type=MediaType.MOVIE,
        status=MediaStatus.SEARCHING,
    )
    assert build_history_target_label(movie, None, None) == "Movie"

    series = MediaItem(
        provider_id=1,
        title="Severance",
        year=2022,
        media_type=MediaType.SHOW,
        status=MediaStatus.SEARCHING,
    )
    season = Season(
        media_item_id=1,
        season_number=2,
        type_number=2,
        entry_type="season",
        status=SeasonStatus.SEARCHING,
    )
    assert build_history_target_label(series, season, None) == "Season 2 Pack"

    ep = Episode(
        season_id=1,
        episode_number=5,
        status=EpisodeStatus.SEARCHING,
    )
    assert build_history_target_label(series, season, ep) == "S02E05"

    anime = MediaItem(
        provider_id=1,
        title="Demon Slayer",
        year=2019,
        media_type=MediaType.ANIME,
        status=MediaStatus.SEARCHING,
    )
    anime_movie_season = Season(
        media_item_id=2,
        season_number=2,
        type_number=1,
        entry_type="movie",
        title="Mugen Train",
        status=SeasonStatus.SEARCHING,
    )
    assert build_history_target_label(anime, anime_movie_season, None) == "Mugen Train"

    anime_movie_untitled = Season(
        media_item_id=2,
        season_number=3,
        type_number=2,
        entry_type="movie",
        title=None,
        status=SeasonStatus.SEARCHING,
    )
    assert build_history_target_label(anime, anime_movie_untitled, None) == "Movie 2"


@pytest.mark.asyncio
async def test_push_engine_populates_history_snapshots(snapshot_db, monkeypatch):
    """Verify execute_auto_push, advance_season_buffer, and execute_manual_grab populate all snapshot columns."""
    _, maker = snapshot_db
    now = datetime.now(timezone.utc)

    async def mock_fetch_nzb_bytes(guid, **_kwargs):
        xml = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
            b'<file poster="p" date="1700000000" subject="Movie.mkv (1/1)">'
            b"<groups><group>alt.binaries.movies</group></groups>"
            b'<segments><segment bytes="500000000" number="1">msg1@test</segment></segments>'
            b"</file></nzb>"
        )
        return xml, f"{guid}.nzb"

    job_counter = {"n": 0}

    async def mock_send_nzb_file(*_args, **_kwargs):
        job_counter["n"] += 1
        return {"id": f"tb-snap-{job_counter['n']}", "hash": f"hash-{job_counter['n']}"}

    async def mock_search_movie(**kwargs):
        title = kwargs.get("title") or "Movie"
        return [
            {
                "title": f"{title.replace(' ', '.')}.2022.1080p.BluRay.x265",
                "guid": f"guid-{title.lower().replace(' ', '-')}",
                "size": 8_000_000_000,
            }
        ]

    async def mock_search_show(**kwargs):
        title = kwargs.get("title") or "Show"
        s_num = kwargs.get("season") or 1
        ep_str = kwargs.get("ep")
        if ep_str:
            return [
                {
                    "title": f"{title.replace(' ', '.')}.S{int(s_num):02d}E{int(ep_str):02d}.1080p.WEB-DL.x265",
                    "guid": f"guid-s{s_num}e{ep_str}",
                    "size": 1_500_000_000,
                }
            ]
        return [
            {
                "title": f"{title.replace(' ', '.')}.S{int(s_num):02d}.1080p.WEB-DL.x265",
                "guid": f"guid-s{s_num}-pack",
                "size": 10_000_000_000,
            }
        ]

    monkeypatch.setattr(
        "app.services.indexers.newznab.fetch_nzb_bytes", mock_fetch_nzb_bytes
    )
    monkeypatch.setattr(
        "app.services.downloaders.torbox.send_nzb_file", mock_send_nzb_file
    )
    monkeypatch.setattr("app.services.indexers.newznab.search_movie", mock_search_movie)
    monkeypatch.setattr("app.services.indexers.newznab.search_show", mock_search_show)
    monkeypatch.setattr("app.core.transfer_poller.wake_transfer_poller", lambda: None)

    async with maker() as session:
        sys_settings = SystemSettings(id=1)
        provider = Provider(
            name="Simkl", type="simkl", category="watchlist", is_active=True
        )
        session.add_all([sys_settings, provider])
        await session.flush()

        # 1. Standalone Anime Movie for execute_auto_push
        anime_movie = MediaItem(
            provider_id=provider.id,
            simkl_id=5001,
            tmdb_id=6001,
            imdb_id="tt5001",
            anilist_id=7001,
            title="Suzume",
            year=2022,
            poster_url="https://img.test/suzume.jpg",
            media_type=MediaType.MOVIE,
            is_anime_movie=True,
            status=MediaStatus.SEARCHING,
            release_date=now - timedelta(days=100),
        )
        session.add(anime_movie)

        # 2. Series with Season 1 (DOWNLOADED) and Season 2 (SEARCHING) for advance_season_buffer & manual grab
        series = MediaItem(
            provider_id=provider.id,
            simkl_id=5002,
            tmdb_id=6002,
            imdb_id="tt5002",
            title="The Bear",
            year=2022,
            poster_url="https://img.test/bear.jpg",
            media_type=MediaType.SHOW,
            status=MediaStatus.SEARCHING,
            allow_season_packs=True,
            prefer_season_packs=True,
        )
        session.add(series)
        await session.flush()

        s1 = Season(
            media_item_id=series.id,
            season_number=1,
            watch_order=1,
            type_number=1,
            entry_type="season",
            status=SeasonStatus.DOWNLOADED,
            air_date=now - timedelta(days=300),
        )
        s2 = Season(
            media_item_id=series.id,
            season_number=2,
            watch_order=2,
            type_number=2,
            entry_type="season",
            status=SeasonStatus.SEARCHING,
            air_date=now - timedelta(days=100),
        )
        session.add_all([s1, s2])
        await session.flush()

        ep3 = Episode(
            season_id=s2.id,
            episode_number=3,
            status=EpisodeStatus.SEARCHING,
            air_date=now - timedelta(days=90),
        )
        session.add(ep3)
        await session.commit()

        # Execute auto push on standalone anime movie
        res_auto = await execute_auto_push(session, anime_movie.id, {})
        assert res_auto["pushed"] is True

        # Execute season buffer advance on series (advances to Season 2 Pack)
        adv_hists = await advance_season_buffer(session, series, base_watch_order=1)
        assert len(adv_hists) == 1

        # Execute manual grab on Episode 3 of Season 2
        res_manual = await execute_manual_grab(
            session,
            series.id,
            {
                "guid": "guid-bear-s02e03-manual",
                "title": "The.Bear.S02E03.1080p.WEB-DL.x265",
                "size_bytes": 1_500_000_000,
                "score": 120.0,
                "season_id": s2.id,
                "episode_id": ep3.id,
            },
        )
        assert res_manual["pushed"] is True

        histories = (
            (
                await session.execute(
                    select(DownloadHistory).order_by(DownloadHistory.id.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(histories) == 3

        h_movie, h_s2, h_ep = histories

        assert h_movie.media_title == "Suzume"
        assert h_movie.media_year == 2022
        assert h_movie.media_type_label == "anime-movie"
        assert h_movie.target_label == "Movie"
        assert h_movie.poster_url == "https://img.test/suzume.jpg"
        assert h_movie.simkl_id == 5001
        assert h_movie.tmdb_id == 6001
        assert h_movie.imdb_id == "tt5001"
        assert h_movie.anilist_id == 7001

        assert h_s2.media_title == "The Bear"
        assert h_s2.media_year == 2022
        assert h_s2.media_type_label == "series"
        assert h_s2.target_label == "Season 2 Pack"
        assert h_s2.poster_url == "https://img.test/bear.jpg"
        assert h_s2.simkl_id == 5002
        assert h_s2.tmdb_id == 6002
        assert h_s2.imdb_id == "tt5002"

        assert h_ep.media_title == "The Bear"
        assert h_ep.media_year == 2022
        assert h_ep.media_type_label == "series"
        assert h_ep.target_label == "S02E03"
        assert h_ep.poster_url == "https://img.test/bear.jpg"
        assert h_ep.simkl_id == 5002


@pytest.mark.asyncio
async def test_init_db_rebuilds_legacy_download_history_and_backfills_snapshots(
    tmp_path,
):
    """Verify init_db() migrates a legacy download_history table (NOT NULL + CASCADE) and backfills snapshots."""
    db_path = tmp_path / "legacy_v2_history.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    legacy_engine = create_async_engine(db_url)

    # Create base tables first, then replace download_history with legacy NOT NULL + ON DELETE CASCADE schema
    async with legacy_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("DROP TABLE download_history"))
        await conn.execute(
            text(
                """
                CREATE TABLE download_history (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
                    season_id INTEGER REFERENCES seasons(id) ON DELETE CASCADE,
                    episode_id INTEGER REFERENCES episodes(id) ON DELETE CASCADE,
                    nzb_title VARCHAR(1000) NOT NULL,
                    nzb_guid VARCHAR(500),
                    score FLOAT,
                    size_bytes INTEGER,
                    resolution VARCHAR(20),
                    video_codec VARCHAR(50),
                    audio_codec VARCHAR(100),
                    source VARCHAR(100),
                    release_group VARCHAR(100),
                    bitrate_mbps FLOAT,
                    torbox_hash VARCHAR(200),
                    torbox_id VARCHAR(200),
                    torbox_sent_at DATETIME,
                    is_fallback BOOLEAN NOT NULL DEFAULT 0,
                    grabbed_language VARCHAR(50),
                    push_mode VARCHAR(20) NOT NULL DEFAULT 'auto',
                    progress_pct FLOAT NOT NULL DEFAULT 0.0,
                    download_speed_bytes BIGINT NOT NULL DEFAULT 0,
                    eta_seconds INTEGER,
                    status_detail VARCHAR(200),
                    is_dismissed BOOLEAN NOT NULL DEFAULT 0,
                    notification_sent BOOLEAN NOT NULL DEFAULT 0,
                    auto_replaced_count INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        # Seed a provider, media_item, season, and legacy download_history row without snapshot columns
        await conn.execute(
            text(
                "INSERT INTO providers (id, name, type, category, is_active, priority, config_json) "
                "VALUES (1, 'Simkl', 'simkl', 'watchlist', 1, 1, '{}')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO media_items (id, provider_id, simkl_id, tmdb_id, imdb_id, title, year, poster_url, media_type, is_anime_movie, status, created_at, updated_at) "
                "VALUES (10, 1, 8888, 9999, 'tt8888', 'Andor', 2022, 'https://img.test/andor.jpg', 'SHOW', 0, 'COMPLETED', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO seasons (id, media_item_id, season_number, watch_order, type_number, entry_type, monitored, status, created_at, updated_at) "
                "VALUES (20, 10, 1, 1, 1, 'season', 1, 'COMPLETED', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO download_history (id, media_item_id, season_id, episode_id, nzb_title, nzb_guid, torbox_id, status_detail, size_bytes, created_at) "
                "VALUES (30, 10, 20, NULL, 'Andor.S01.1080p.WEB-DL', 'guid-andor-s1', 'tb-andor-1', 'completed', 15000000000, '2026-01-02 00:00:00')"
            )
        )
    await legacy_engine.dispose()

    # Run idempotent init_db(db_url)
    await init_db(db_url)

    from app.db.database import get_engine, get_session_factory

    migrated_engine = get_engine()
    migrated_maker = get_session_factory()

    async with migrated_engine.begin() as conn:
        pragma_rows = (
            await conn.execute(text("PRAGMA table_info(download_history)"))
        ).fetchall()
        col_map = {row[1]: row for row in pragma_rows}
        # media_item_id must be nullable (notnull == 0)
        assert col_map["media_item_id"][3] == 0
        assert "media_title" in col_map
        assert "target_label" in col_map

    async with migrated_maker() as session:
        hist = (
            await session.execute(
                select(DownloadHistory).where(DownloadHistory.id == 30)
            )
        ).scalar_one()
        assert hist.media_title == "Andor"
        assert hist.media_year == 2022
        assert hist.media_type_label == "series"
        assert hist.target_label == "Season 1 Pack"
        assert hist.poster_url == "https://img.test/andor.jpg"
        assert hist.simkl_id == 8888
        assert hist.tmdb_id == 9999
        assert hist.imdb_id == "tt8888"

    await close_db()


@pytest.mark.asyncio
async def test_dashboard_retains_completed_items_in_active_in_progress_tier(
    snapshot_db,
    async_client: AsyncClient,
):
    """Verify COMPLETED Movies, Series, and Anime remain visible on Cards 1-3 in Section 1 (Active / In Progress)."""
    _, maker = snapshot_db
    now = datetime.now(timezone.utc)
    recent_sync = (now - timedelta(minutes=2)).isoformat()

    async with maker() as session:
        sys_settings = SystemSettings(id=1)
        provider = Provider(
            name="Simkl",
            type="simkl",
            category="watchlist",
            is_active=True,
            config_json=f'{{"sync_interval_minutes": 60, "last_synced_at": "{recent_sync}"}}',
        )
        session.add_all([sys_settings, provider])
        await session.flush()
        await seed_ready_setup(session)

        completed_movie = MediaItem(
            provider_id=provider.id,
            simkl_id=1101,
            title="Completed Movie Watchlist",
            year=2023,
            release_date=now - timedelta(days=100),
            media_type=MediaType.MOVIE,
            status=MediaStatus.COMPLETED,
        )
        completed_series = MediaItem(
            provider_id=provider.id,
            simkl_id=1102,
            title="Completed Series Watchlist",
            year=2021,
            media_type=MediaType.SHOW,
            status=MediaStatus.COMPLETED,
        )
        session.add_all([completed_movie, completed_series])
        await session.flush()

        s1 = Season(
            media_item_id=completed_series.id,
            season_number=1,
            type_number=1,
            entry_type="season",
            status=SeasonStatus.COMPLETED,
            episode_count=8,
            air_date=now - timedelta(days=200),
        )
        session.add(s1)
        await session.commit()

        # Verify classify_v3_status_tier classifies both as "in_progress"
        assert classify_v3_status_tier(completed_movie) == "in_progress"
        assert classify_v3_status_tier(completed_series) == "in_progress"

    resp = await async_client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # Both completed items must be rendered in the active watchlist cards with data-tier="in_progress"
    assert "Completed Movie Watchlist" in html
    assert "Completed Series Watchlist" in html
    assert 'data-tier="in_progress"' in html


@pytest.mark.asyncio
async def test_simkl_sync_prunes_completed_or_dropped_items_and_detaches_history(
    snapshot_db, monkeypatch
):
    """Verify sync_all_providers prunes items absent from Simkl plantowatch+watching while preserving DownloadHistory."""
    _, maker = snapshot_db
    now = datetime.now(timezone.utc)

    async with maker() as session:
        sys_settings = SystemSettings(id=1)
        provider = Provider(
            name="Simkl",
            type="simkl",
            category="watchlist",
            is_active=True,
            client_id="cid-1",
            access_token="tok-1",
            config_json='{"access_token": "tok-1", "client_id": "cid-1", "sync_movies": true, "sync_series": true, "sync_anime": true}',
        )
        session.add_all([sys_settings, provider])
        await session.flush()

        # 1. Movie moved to Simkl completed (absent from plantowatch + watching) -> MUST BE PRUNED
        pruned_movie = MediaItem(
            provider_id=provider.id,
            simkl_id=2001,
            tmdb_id=3001,
            imdb_id="tt2001",
            title="Pruned Completed Movie",
            year=2021,
            poster_url="https://img.test/pruned.jpg",
            media_type=MediaType.MOVIE,
            status=MediaStatus.COMPLETED,
        )
        # 2. Movie still in Simkl plantowatch -> MUST BE RETAINED
        kept_movie = MediaItem(
            provider_id=provider.id,
            simkl_id=2002,
            title="Kept Watchlist Movie",
            year=2024,
            media_type=MediaType.MOVIE,
            status=MediaStatus.COMPLETED,
        )
        # 3. Movie absent from Simkl plantowatch, but currently DOWNLOADING -> MUST NOT BE PRUNED
        downloading_movie = MediaItem(
            provider_id=provider.id,
            simkl_id=2003,
            title="Active Downloading Movie",
            year=2024,
            media_type=MediaType.MOVIE,
            status=MediaStatus.DOWNLOADING,
        )
        # 4. Consolidated Anime franchise where parent simkl_id (2004) is absent,
        #    but child Season 2 simkl_id (2005) IS still in watching -> MUST BE RETAINED
        kept_anime = MediaItem(
            provider_id=provider.id,
            simkl_id=2004,
            title="Consolidated Anime Kept",
            year=2020,
            media_type=MediaType.ANIME,
            status=MediaStatus.SEARCHING,
        )
        # 5. Consolidated Anime franchise where neither parent (2006) nor child seasons (2007)
        #    are in Simkl active response -> MUST BE PRUNED
        pruned_anime = MediaItem(
            provider_id=provider.id,
            simkl_id=2006,
            anilist_id=4006,
            title="Consolidated Anime Pruned",
            year=2019,
            media_type=MediaType.ANIME,
            status=MediaStatus.COMPLETED,
        )
        session.add_all(
            [pruned_movie, kept_movie, downloading_movie, kept_anime, pruned_anime]
        )
        await session.flush()

        kept_anime_s1 = Season(
            media_item_id=kept_anime.id,
            simkl_id=2004,
            season_number=1,
            type_number=1,
            entry_type="season",
            status=SeasonStatus.COMPLETED,
        )
        kept_anime_s2 = Season(
            media_item_id=kept_anime.id,
            simkl_id=2005,
            anilist_id=4005,
            season_number=2,
            type_number=2,
            entry_type="season",
            status=SeasonStatus.SEARCHING,
        )
        pruned_anime_s1 = Season(
            media_item_id=pruned_anime.id,
            simkl_id=2007,
            season_number=1,
            type_number=1,
            entry_type="season",
            status=SeasonStatus.COMPLETED,
        )
        session.add_all([kept_anime_s1, kept_anime_s2, pruned_anime_s1])
        await session.flush()
        from app.db.models import MediaItemWatchlistSource

        for src_item in (
            pruned_movie,
            kept_movie,
            downloading_movie,
            kept_anime,
            pruned_anime,
        ):
            session.add(
                MediaItemWatchlistSource(
                    media_item_id=src_item.id,
                    provider_id=provider.id,
                    remote_id=str(src_item.simkl_id),
                    list_status="watching",
                )
            )
        await session.flush()

        # History rows attached to pruned_movie and pruned_anime_s1 (without pre-populated snapshots to test detachment snapshotting)
        hist_movie = DownloadHistory(
            media_item_id=pruned_movie.id,
            nzb_title="Pruned.Completed.Movie.2021.1080p",
            nzb_guid="guid-pruned-movie",
            downloader_transfer_id="tb-pruned-movie",
            status_detail="completed",
            size_bytes=5_000_000_000,
            created_at=now - timedelta(days=2),
        )
        hist_anime = DownloadHistory(
            media_item_id=pruned_anime.id,
            season_id=pruned_anime_s1.id,
            nzb_title="Consolidated.Anime.Pruned.S01.1080p",
            nzb_guid="guid-pruned-anime",
            downloader_transfer_id="tb-pruned-anime",
            status_detail="completed",
            size_bytes=10_000_000_000,
            created_at=now - timedelta(days=1),
        )
        session.add_all([hist_movie, hist_anime])
        await session.commit()

        pruned_movie_id = pruned_movie.id
        pruned_anime_id = pruned_anime.id
        kept_movie_id = kept_movie.id
        downloading_movie_id = downloading_movie.id
        kept_anime_id = kept_anime.id

    # Mock simkl.get_watchlist to return only simkl_id 2002 (movies) and 2005 (anime)
    async def mock_get_watchlist(media_type: str, client_id=None, access_token=None):
        if media_type == "movies":
            return [
                {
                    "movie": {
                        "title": "Kept Watchlist Movie",
                        "year": 2024,
                        "ids": {"simkl": 2002},
                    }
                }
            ]
        if media_type == "shows":
            return []
        if media_type == "anime":
            return [
                {
                    "show": {
                        "title": "Consolidated Anime Kept S2",
                        "year": 2022,
                        "ids": {"simkl": 2005, "anilist": 4005},
                    }
                }
            ]
        return []

    monkeypatch.setattr(
        "app.services.watchlists.simkl.get_watchlist", mock_get_watchlist
    )

    await sync_all_providers()

    async with maker() as session:
        remaining_ids = set(
            (await session.execute(select(MediaItem.id))).scalars().all()
        )
        assert pruned_movie_id not in remaining_ids
        assert pruned_anime_id not in remaining_ids
        assert kept_movie_id in remaining_ids
        assert downloading_movie_id in remaining_ids
        assert kept_anime_id in remaining_ids

        # Verify DownloadHistory rows survived with detached foreign keys and populated snapshots
        histories = (
            (
                await session.execute(
                    select(DownloadHistory).order_by(DownloadHistory.id.asc())
                )
            )
            .scalars()
            .all()
        )
        assert len(histories) == 2
        h1, h2 = histories

        assert h1.media_item_id is None
        assert h1.season_id is None
        assert h1.episode_id is None
        assert h1.media_title == "Pruned Completed Movie"
        assert h1.media_year == 2021
        assert h1.media_type_label == "movie"
        assert h1.target_label == "Movie"
        assert h1.simkl_id == 2001
        assert h1.tmdb_id == 3001
        assert h1.imdb_id == "tt2001"

        assert h2.media_item_id is None
        assert h2.season_id is None
        assert h2.episode_id is None
        assert h2.media_title == "Consolidated Anime Pruned"
        assert h2.media_year == 2019
        assert h2.media_type_label == "anime"
        assert h2.target_label == "Season 1 Pack"
        assert h2.simkl_id == 2007
        assert h2.anilist_id == 4006


@pytest.mark.asyncio
async def test_delete_media_item_endpoint_detaches_and_preserves_download_history(
    snapshot_db,
    async_client: AsyncClient,
):
    """Verify DELETE /items/{item_id} snapshots and detaches DownloadHistory rows before deleting MediaItem."""
    _, maker = snapshot_db

    async with maker() as session:
        sys_settings = SystemSettings(id=1)
        provider = Provider(
            name="Simkl", type="simkl", category="watchlist", is_active=True
        )
        session.add_all([sys_settings, provider])
        await session.flush()

        item = MediaItem(
            provider_id=provider.id,
            simkl_id=7777,
            tmdb_id=8888,
            imdb_id="tt7777",
            title="Manual Delete Movie",
            year=2020,
            poster_url="https://img.test/manual.jpg",
            media_type=MediaType.MOVIE,
            status=MediaStatus.COMPLETED,
        )
        session.add(item)
        await session.flush()

        hist = DownloadHistory(
            media_item_id=item.id,
            nzb_title="Manual.Delete.Movie.2020.1080p",
            nzb_guid="guid-manual-del",
            downloader_transfer_id="tb-manual-del",
            status_detail="completed",
            size_bytes=4_000_000_000,
        )
        session.add(hist)
        await session.commit()
        item_id = item.id
        hist_id = hist.id

    resp = await async_client.delete(f"/items/{item_id}")
    assert resp.status_code == 200

    async with maker() as session:
        deleted_item = (
            await session.execute(select(MediaItem).where(MediaItem.id == item_id))
        ).scalar_one_or_none()
        assert deleted_item is None

        preserved_hist = (
            await session.execute(
                select(DownloadHistory).where(DownloadHistory.id == hist_id)
            )
        ).scalar_one_or_none()
        assert preserved_hist is not None
        assert preserved_hist.media_item_id is None
        assert preserved_hist.media_title == "Manual Delete Movie"
        assert preserved_hist.media_year == 2020
        assert preserved_hist.media_type_label == "movie"
        assert preserved_hist.target_label == "Movie"
        assert preserved_hist.simkl_id == 7777
        assert preserved_hist.tmdb_id == 8888
        assert preserved_hist.imdb_id == "tt7777"


@pytest.mark.asyncio
async def test_simkl_sync_skips_pruning_on_category_fetch_error(
    snapshot_db, monkeypatch
):
    """Verify sync_all_providers does not prune local items when a Simkl category fetch fails."""
    _, maker = snapshot_db

    async with maker() as session:
        sys_settings = SystemSettings(id=1)
        provider = Provider(
            name="Simkl",
            type="simkl",
            category="watchlist",
            is_active=True,
            client_id="cid-err",
            access_token="tok-err",
            config_json='{"access_token": "tok-err", "client_id": "cid-err", "sync_movies": true, "sync_series": true, "sync_anime": true}',
        )
        session.add_all([sys_settings, provider])
        await session.flush()

        item = MediaItem(
            provider_id=provider.id,
            simkl_id=9001,
            title="Transient Error Protected Movie",
            year=2024,
            media_type=MediaType.MOVIE,
            status=MediaStatus.COMPLETED,
        )
        session.add(item)
        await session.commit()
        item_id = item.id

    async def mock_failing_watchlist(
        media_type: str, client_id=None, access_token=None
    ):
        if media_type == "shows":
            raise RuntimeError("Simkl API transient 502 error")
        return []

    monkeypatch.setattr(
        "app.services.watchlists.simkl.get_watchlist", mock_failing_watchlist
    )

    await sync_all_providers()

    async with maker() as session:
        kept_item = (
            await session.execute(select(MediaItem).where(MediaItem.id == item_id))
        ).scalar_one_or_none()
        assert kept_item is not None


@pytest.mark.asyncio
async def test_deduplicate_media_items_and_concurrent_sync_lock(
    snapshot_db, monkeypatch
):
    """Verify deduplicate_media_items merges duplicate rows and _sync_lock prevents concurrent sync duplicates."""
    import asyncio

    from app.core.automation import deduplicate_media_items

    _, maker = snapshot_db

    async with maker() as session:
        sys_settings = SystemSettings(id=1)
        provider = Provider(
            name="Simkl",
            type="simkl",
            category="watchlist",
            is_active=True,
            client_id="cid-dup",
            access_token="tok-dup",
            config_json='{"access_token": "tok-dup", "client_id": "cid-dup", "sync_movies": true, "sync_series": false, "sync_anime": false}',
        )
        session.add_all([sys_settings, provider])
        await session.flush()

        dup1 = MediaItem(
            provider_id=provider.id,
            simkl_id=74006,
            tmdb_id=17654,
            imdb_id="tt1136608",
            title="District 9",
            year=2009,
            media_type=MediaType.MOVIE,
            status=MediaStatus.SEARCHING,
        )
        dup2 = MediaItem(
            provider_id=provider.id,
            simkl_id=74006,
            tmdb_id=17654,
            imdb_id="tt1136608",
            title="District 9",
            alt_title="District 9 DE",
            year=2009,
            media_type=MediaType.MOVIE,
            status=MediaStatus.SEARCHING,
        )
        session.add_all([dup1, dup2])
        await session.flush()

        hist = DownloadHistory(
            media_item_id=dup2.id,
            nzb_title="District.9.2009.1080p.BluRay",
            nzb_guid="guid-d9",
            downloader_transfer_id="tb-d9",
            status_detail="completed",
            size_bytes=8_000_000_000,
        )
        session.add(hist)
        await session.commit()

        dup1_id = dup1.id
        dup2_id = dup2.id
        hist_id = hist.id

    async with maker() as session:
        removed = await deduplicate_media_items(session)
        await session.commit()
        assert removed == 1

        remaining = (
            (
                await session.execute(
                    select(MediaItem).where(MediaItem.simkl_id == 74006)
                )
            )
            .scalars()
            .all()
        )
        assert len(remaining) == 1
        # dup2 had download_history so it ranks higher as canonical, or if equal lowest ID; here dup2 has hist_count=1
        canonical = remaining[0]
        assert canonical.id in (dup1_id, dup2_id)
        assert canonical.alt_title == "District 9 DE"

        rehomed_hist = (
            await session.execute(
                select(DownloadHistory).where(DownloadHistory.id == hist_id)
            )
        ).scalar_one()
        assert rehomed_hist.media_item_id == canonical.id

    # Now test two concurrent sync_all_providers() invocations on a fresh movie
    async def mock_watchlist(media_type: str, client_id=None, access_token=None):
        await asyncio.sleep(0.05)
        if media_type == "movies":
            return [
                {
                    "movie": {
                        "title": "Ender's Game",
                        "year": 2013,
                        "ids": {
                            "simkl": 80001,
                            "tmdb": "80274",
                            "imdb": "tt1731141",
                        },
                    }
                }
            ]
        return []

    async def mock_movie_details(tmdb_id: int):
        await asyncio.sleep(0.05)
        return {"title": "Ender's Game", "release_date": "2013-11-01"}

    async def mock_digital_release(tmdb_id: int):
        return None

    monkeypatch.setattr("app.services.watchlists.simkl.get_watchlist", mock_watchlist)
    monkeypatch.setattr("app.services.tmdb.get_movie_details", mock_movie_details)
    monkeypatch.setattr(
        "app.services.tmdb.get_digital_release_date", mock_digital_release
    )

    await asyncio.gather(sync_all_providers(), sync_all_providers())

    async with maker() as session:
        enders = (
            (await session.execute(select(MediaItem).where(MediaItem.tmdb_id == 80274)))
            .scalars()
            .all()
        )
        assert len(enders) == 1
