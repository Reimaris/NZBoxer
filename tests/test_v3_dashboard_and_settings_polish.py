"""
Tests for Ticket 03: Dashboard Poster Thumbnails, Vertical Progress Pills,
Smart History Actions & v3 Settings Polish (v3.0.0)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    MediaType,
    Provider,
    Season,
    SeasonStatus,
)
from tests.conftest import (
    make_history,
    make_movie,
    make_series,
    seed_core_prerequisites,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_dynamic_partial_season_progress_pills_and_cutoff_retirement(
    db_session: AsyncSession,
) -> None:
    """MediaItem.progress_pills appends dynamic SXX - E Y/Z pills for partial seasons."""
    item, seasons, episodes = await make_series(
        db_session,
        title="Frieren",
        media_type=MediaType.ANIME,
        year=2023,
        release_date=datetime(2023, 9, 29, tzinfo=timezone.utc),
        seasons=[
            {
                "season_number": 1,
                "watch_order": 1,
                "type_number": 1,
                "entry_type": "season",
                "status": SeasonStatus.COMPLETED,
                "monitored": True,
            },
            {
                "season_number": 2,
                "watch_order": 2,
                "type_number": 2,
                "entry_type": "season",
                "status": SeasonStatus.SEARCHING,
                "monitored": True,
            },
            {
                "season_number": 3,
                "watch_order": 3,
                "type_number": 1,
                "entry_type": "movie",
                "status": SeasonStatus.SEARCHING,
                "monitored": True,
            },
        ],
        episodes_per_season=4,
        episode_statuses=[
            EpisodeStatus.COMPLETED,
            EpisodeStatus.DOWNLOADED,
            EpisodeStatus.SEARCHING,
            EpisodeStatus.SEARCHING,
        ],
    )
    # Mark all 4 episodes of Season 1 as COMPLETED
    for ep in episodes[:4]:
        ep.status = EpisodeStatus.COMPLETED
    await db_session.commit()

    reloaded = (
        (
            await db_session.execute(
                select(MediaItem)
                .where(MediaItem.id == item.id)
                .options(selectinload(MediaItem.seasons).selectinload(Season.episodes))
            )
        )
        .scalars()
        .one()
    )

    assert reloaded.progress_pills == [
        "1/2 Seasons",
        "0/1 Movie",
        "S02 - E 2/4",
    ]


@pytest.mark.asyncio
async def test_dashboard_posters_vertical_pills_simkl_links_and_smart_history(
    db_session: AsyncSession,
    async_client: AsyncClient,
) -> None:
    """Dashboard renders Column 1 posters, vertical pill stacks without WANTED, canonical SIMKL links, 5-col History with Smart Trash, and refreshDashboard listener."""
    await seed_core_prerequisites(db_session)
    # 1. Ready-to-Push Movie with poster
    ready_movie = await make_movie(
        db_session,
        title="Ready Movie",
        year=2024,
        simkl_id=11001,
        tmdb_id=22001,
        imdb_id="tt11001",
        poster_url="https://image.tmdb.org/t/p/w500/ready_movie.jpg",
        release_date=datetime(2024, 3, 1, tzinfo=timezone.utc),
        status=MediaStatus.SEARCHING,
    )

    watchlist = await db_session.get(Provider, ready_movie.provider_id)
    assert watchlist is not None
    watchlist.client_id, watchlist.access_token = "cid", "tok"

    # 2. Partially downloaded Series with Season 1 air_date (release_date=None on parent)
    s1_air = datetime(2022, 1, 15, tzinfo=timezone.utc)
    show, show_seasons, show_eps = await make_series(
        db_session,
        title="Partial Show",
        media_type=MediaType.SHOW,
        year=2022,
        simkl_id=11002,
        tmdb_id=22002,
        poster_url="https://image.tmdb.org/t/p/w500/partial_show.jpg",
        release_date=None,
        status=MediaStatus.SEARCHING,
        seasons=[
            {
                "season_number": 1,
                "watch_order": 1,
                "type_number": 1,
                "air_date": s1_air,
                "status": SeasonStatus.SEARCHING,
                "monitored": True,
            }
        ],
        episodes_per_season=3,
        episode_statuses=[
            EpisodeStatus.COMPLETED,
            EpisodeStatus.SEARCHING,
            EpisodeStatus.SEARCHING,
        ],
    )

    # 3. Upcoming Anime (in Future section -> must render FUTURE badge and NO season/movie progress pills)
    future_air = datetime(2027, 7, 1, tzinfo=timezone.utc)
    future_anime, _, _ = await make_series(
        db_session,
        title="Upcoming Anime 2027",
        media_type=MediaType.ANIME,
        year=2027,
        simkl_id=11003,
        anilist_id=33003,
        poster_url=None,
        release_date=future_air,
        status=MediaStatus.FUTURE,
        seasons=[
            {
                "season_number": 1,
                "watch_order": 1,
                "type_number": 1,
                "air_date": future_air,
                "status": SeasonStatus.FUTURE,
                "monitored": True,
            }
        ],
    )

    # 4. Two History entries: one active on TorBox (Stage 1 Smart Trash) and one deleted (Stage 2 Smart Trash)
    h_live = await make_history(
        db_session,
        item=show,
        season=show_seasons[0],
        episode=show_eps[0],
        nzb_title="Partial.Show.S01E01.2160p.WEB-DL.DV.DDP5.1.Atmos.H.265-GRP",
        downloader_transfer_id="tb-live-ep1",
        status_detail="completed",
    )
    h_deleted = await make_history(
        db_session,
        item=ready_movie,
        nzb_title="Ready.Movie.2024.1080p.BluRay.x264-GRP",
        downloader_transfer_id="tb-del-movie",
        status_detail="deleted",
    )
    await db_session.commit()

    resp = await async_client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # Column 1 poster thumbnails rendered
    assert "w-10 h-14" in html
    assert "https://image.tmdb.org/t/p/w500/ready_movie.jpg" in html
    assert "https://image.tmdb.org/t/p/w500/partial_show.jpg" in html

    # Effective release date rendered for show whose parent release_date is None
    assert "2022-01-15" in html

    # Legacy WANTED badge retired from table rows
    assert ">WANTED<" not in html

    # Dynamic partial episode pill rendered for Partial Show
    assert "0/1 Season" in html
    assert "S01 - E 1/3" in html

    # Canonical SIMKL URLs & rel="noopener noreferrer"
    assert "https://simkl.com/movies/11001" in html
    assert "https://simkl.com/tv/11002" in html
    assert "https://simkl.com/anime/11003" in html
    assert "https://simkl.com/shows/" not in html
    assert 'rel="noopener noreferrer"' in html

    # Card 4 History 5-column layout & two-stage Smart Trash button
    assert 'colspan="5"' in html
    assert f'hx-post="/api/history/{h_live.id}/delete-transfer"' in html
    assert 'title="Delete from TorBox"' in html
    assert f'hx-delete="/api/history/{h_deleted.id}"' in html
    assert 'title="Remove History Entry"' in html
    assert 'title="Re-Push / Open Push Modal"' in html

    # Live HTMX refreshDashboard listener & top-deck card counter IDs
    assert "refreshDashboard" in html
    for counter_id in (
        "deck-card-count-movie",
        "deck-card-count-show",
        "deck-card-count-anime",
        "deck-card-count-history",
        "deck-card-count-active",
    ):
        assert counter_id in html


@pytest.mark.asyncio
async def test_settings_read_only_scoring_and_newznab_indexer_branding(
    db_session: AsyncSession,
    async_client: AsyncClient,
) -> None:
    """Settings Tab 3 is a 6-card read-only reference grid without cutoff inputs, and Provider modal uses Newznab Indexer branding."""
    indexer = Provider(
        name="My Usenet Indexer",
        type="newznab",
        category="indexer",
        api_url="",
        is_active=True,
    )
    db_session.add(indexer)
    await db_session.commit()

    settings_resp = await async_client.get("/settings")
    assert settings_resp.status_code == 200
    s_html = settings_resp.text

    # No legacy cutoff inputs or Save Video Scoring Settings button
    assert 'name="cutoffs_target"' not in s_html
    assert 'name="cutoffs_upgrade"' not in s_html
    assert "Save Video Scoring Settings" not in s_html

    # 6 read-only scoring reference cards present
    for card_heading in (
        "Resolution",
        "Source Quality",
        "Audio Formats",
        "Video Codec",
        "HDR &amp; Dynamic Range",
        "Audio Channels &amp; Bitrate",
    ):
        assert card_heading in s_html

    # Indexer card in Settings Tab 1 uses NZB badge and Newznab API fallback label
    assert ">NZB<" in s_html
    assert "Newznab API" in s_html

    # Provider modal Step 1 & Step 2
    modal_resp = await async_client.get("/settings/provider/new")
    assert modal_resp.status_code == 200
    m_html = modal_resp.text

    assert "Custom Newznab" in m_html
    assert "with PIN authentication" not in m_html
    assert "OAuth 2.0 Device Flow" in m_html
    assert "https://api.your-indexer.com/api" in m_html


def test_dockerfile_gosu_and_readme_quickstart() -> None:
    """Dockerfile uses gosu for /app/config ownership and README.md includes standalone compose.yaml quickstart."""
    dockerfile_text = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "gosu" in dockerfile_text
    assert "mkdir -p /app/config/logs /app/data" in dockerfile_text
    assert "chown -R appuser:appuser /app/config /app/data" in dockerfile_text
    assert "exec gosu appuser uvicorn app.main:app" in dockerfile_text

    readme_text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Unlike traditional automation stacks" not in readme_text
    assert "**zero background indexer polling**" in readme_text
    assert "ghcr.io/reimaris/nzboxer:latest" in readme_text
    assert "docker compose up -d" in readme_text
