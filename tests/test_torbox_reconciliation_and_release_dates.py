"""
Tests for Ticket 02: Strict TorBox Transfer Reconciliation, Episode-to-Season Rollup
& Anime Release Date Backfill (v3.0.0)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import app.core.transfer_poller as transfer_poller_module
from app.core.automation import (
    _parse_anilist_start_date,
    enrich_anime_metadata,
    reset_anime_metadata,
    sync_all_providers,
)
from app.core.push_engine import (
    detach_item_download_history,
    populate_history_snapshot,
)
from app.core.transfer_poller import (
    get_push_history_ledger,
    manual_delete_transfer,
    reconcile_all_items_transfer_truth,
    run_transfer_poller_tick,
)
from app.db.models import (
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    MediaType,
    Provider,
    Season,
    SeasonStatus,
)
from tests.conftest import make_history, make_movie, make_series


def test_strict_anilist_start_date_parsing() -> None:
    """_parse_anilist_start_date requires year + month + day for current/future years."""
    now = datetime(2026, 5, 10, tzinfo=timezone.utc)

    # Current or future year missing day or month -> None (TBA)
    assert (
        _parse_anilist_start_date({"year": 2026, "month": None, "day": None}, now)
        is None
    )
    assert (
        _parse_anilist_start_date({"year": 2026, "month": 10, "day": None}, now) is None
    )
    assert (
        _parse_anilist_start_date({"year": 2027, "month": 4, "day": None}, now) is None
    )

    # Complete year + month + day in current/future year -> valid UTC datetime
    assert _parse_anilist_start_date(
        {"year": 2026, "month": 10, "day": 15}, now
    ) == datetime(2026, 10, 15, tzinfo=timezone.utc)

    # Past year with month but missing day -> safe default day=1
    assert _parse_anilist_start_date(
        {"year": 2021, "month": 4, "day": None}, now
    ) == datetime(2021, 4, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_parent_release_date_backfill_and_effective_release_date(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parent MediaItem.release_date and effective_release_date resolve from earliest Season.air_date."""
    s1_air = datetime(2020, 10, 3, tzinfo=timezone.utc)
    s2_air = datetime(2023, 7, 6, tzinfo=timezone.utc)

    item, seasons, _ = await make_series(
        db_session,
        title="Jujutsu Kaisen",
        media_type=MediaType.ANIME,
        year=2020,
        release_date=None,
        seasons=[
            {"season_number": 1, "air_date": s1_air, "status": SeasonStatus.SEARCHING},
            {"season_number": 2, "air_date": s2_air, "status": SeasonStatus.SEARCHING},
        ],
    )
    await db_session.commit()
    item = (
        (
            await db_session.execute(
                select(MediaItem)
                .where(MediaItem.id == item.id)
                .options(selectinload(MediaItem.seasons))
            )
        )
        .scalars()
        .one()
    )

    assert item.release_date is None
    assert item.effective_release_date == s1_air
    assert item.is_tba is False

    # Test enrich_anime_metadata backfills item.release_date when root startDate is missing
    # or from earliest season air_date
    fake_hierarchy = {
        "root": {
            "id": 113415,
            "idMal": 40748,
            "title": {"english": "Jujutsu Kaisen", "romaji": "Jujutsu Kaisen"},
            "startDate": {"year": None, "month": None, "day": None},
        },
        "hierarchy": [
            {
                "id": 113415,
                "format": "TV",
                "entry_type": "season",
                "watch_order": 1,
                "type_number": 1,
                "episodes": 24,
                "startDate": {"year": 2020, "month": 10, "day": 3},
                "title": {"english": "Jujutsu Kaisen"},
            },
            {
                "id": 145064,
                "format": "TV",
                "entry_type": "season",
                "watch_order": 2,
                "type_number": 2,
                "episodes": 23,
                "startDate": {"year": 2023, "month": 7, "day": 6},
                "title": {"english": "Jujutsu Kaisen Season 2"},
            },
        ],
    }
    monkeypatch.setattr(
        "app.services.anilist.get_anime_root_and_hierarchy",
        AsyncMock(return_value=fake_hierarchy),
    )

    anime_item, _, _ = await make_series(
        db_session,
        title="JJK Unbackfilled",
        media_type=MediaType.ANIME,
        simkl_id=55501,
        anilist_id=113415,
        year=2020,
        release_date=None,
        seasons=[],
    )
    await enrich_anime_metadata(db_session, anime_item)
    await db_session.commit()
    await db_session.refresh(anime_item, ["seasons"])

    assert anime_item.release_date == s1_air
    assert anime_item.is_tba is False
    # Both Season 1 and Season 2 are released and must be monitored=True & SEARCHING
    for s in anime_item.seasons:
        assert s.monitored is True
        assert s.status == SeasonStatus.SEARCHING
        for ep in s.episodes:
            assert ep.monitored is True
            assert ep.status == EpisodeStatus.SEARCHING

    # Also test reset_anime_metadata backfills release_date if cleared
    anime_id = anime_item.id
    anime_item.release_date = None
    await db_session.commit()
    await reset_anime_metadata(db_session, anime_id)
    refreshed = await db_session.get(MediaItem, anime_id)
    assert refreshed is not None and refreshed.release_date is not None
    assert refreshed.release_date.replace(tzinfo=timezone.utc) == s1_air


@pytest.mark.asyncio
async def test_simkl_sync_disables_auto_adoption_and_sets_all_released_seasons_searching(
    db_session: AsyncSession,
    watchlist_provider: Provider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background Simkl sync sets all released seasons to SEARCHING without legacy auto-adoption."""
    watchlist_provider.client_id = "cid-test"
    watchlist_provider.access_token = "tok-test"
    watchlist_provider.config_json = '{"refresh_token": "ref-test", "sync_movies": false, "sync_series": true, "sync_anime": false}'
    await db_session.commit()

    simkl_shows = [
        {
            "show": {
                "title": "Severance",
                "year": 2022,
                "ids": {"simkl": 77001, "tmdb": "95396", "imdb": "tt11280740"},
            }
        }
    ]
    tmdb_details = {
        "name": "Severance",
        "first_air_date": "2022-02-17",
        "seasons": [
            {"season_number": 1, "episode_count": 2, "air_date": "2022-02-17"},
            {"season_number": 2, "episode_count": 2, "air_date": "2025-01-17"},
        ],
    }
    tmdb_s1_eps = {
        "episodes": [
            {"episode_number": 1, "air_date": "2022-02-17"},
            {"episode_number": 2, "air_date": "2022-02-25"},
        ]
    }
    tmdb_s2_eps = {
        "episodes": [
            {"episode_number": 1, "air_date": "2025-01-17"},
            {"episode_number": 2, "air_date": "2025-01-24"},
        ]
    }

    monkeypatch.setattr(
        "app.services.watchlists.simkl.ensure_valid_simkl_token",
        AsyncMock(return_value=("cid-test", "tok-test")),
    )
    monkeypatch.setattr(
        "app.services.watchlists.simkl.get_watchlist",
        AsyncMock(return_value=simkl_shows),
    )
    monkeypatch.setattr(
        "app.services.tmdb.get_show_details",
        AsyncMock(return_value=tmdb_details),
    )
    monkeypatch.setattr(
        "app.services.tmdb.get_season_details",
        AsyncMock(
            side_effect=lambda _tid, s_num: tmdb_s1_eps if s_num == 1 else tmdb_s2_eps
        ),
    )

    await sync_all_providers()

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    stmt = (
        select(MediaItem)
        .where(MediaItem.tmdb_id == 95396)
        .options(selectinload(MediaItem.seasons).selectinload(Season.episodes))
    )
    show = (await db_session.execute(stmt)).scalars().first()
    assert show is not None
    assert show.status == MediaStatus.SEARCHING
    assert len(show.seasons) == 2
    for s in show.seasons:
        assert s.monitored is True
        assert s.status == SeasonStatus.SEARCHING
        for ep in s.episodes:
            assert ep.monitored is True
            assert ep.status == EpisodeStatus.SEARCHING


@pytest.mark.asyncio
async def test_episode_to_season_rollup_and_cascading_season_torbox_delete(
    db_session: AsyncSession,
    async_client: AsyncClient,
    default_search_preset: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Episodes pushed in Episodes Only mode roll up to SeasonStatus.COMPLETED and cascade delete from TorBox."""
    item, seasons, episodes = await make_series(
        db_session,
        title="Solo Leveling",
        media_type=MediaType.ANIME,
        year=2024,
        release_date=datetime(2024, 1, 6, tzinfo=timezone.utc),
        seasons=[
            {"season_number": 1, "status": SeasonStatus.SEARCHING, "monitored": True}
        ],
        episodes_per_season=2,
        episode_statuses=[EpisodeStatus.DOWNLOADING, EpisodeStatus.DOWNLOADING],
    )
    s1 = seasons[0]
    ep1, ep2 = episodes[0], episodes[1]

    h1 = await make_history(
        db_session,
        item=item,
        season=s1,
        episode=ep1,
        downloader_transfer_id="tb-ep1",
        nzb_title="Solo.Leveling.S01E01.1080p.WEB-DL.mkv",
        status_detail="downloading",
        is_dismissed=False,
    )
    h2 = await make_history(
        db_session,
        item=item,
        season=s1,
        episode=ep2,
        downloader_transfer_id="tb-ep2",
        nzb_title="Solo.Leveling.S01E02.1080p.WEB-DL.mkv",
        status_detail="downloading",
        is_dismissed=False,
    )
    await db_session.commit()

    fake_tb_downloads = [
        {
            "id": "tb-ep1",
            "name": "Solo.Leveling.S01E01.1080p.WEB-DL.mkv",
            "download_state": "completed",
            "progress": 1.0,
            "files": [
                {"name": "Solo.Leveling.S01E01.1080p.WEB-DL.mkv", "size": 1_500_000_000}
            ],
        },
        {
            "id": "tb-ep2",
            "name": "Solo.Leveling.S01E02.1080p.WEB-DL.mkv",
            "download_state": "completed",
            "progress": 1.0,
            "files": [
                {"name": "Solo.Leveling.S01E02.1080p.WEB-DL.mkv", "size": 1_500_000_000}
            ],
        },
    ]

    await run_transfer_poller_tick(db_session, pre_fetched_downloads=fake_tb_downloads)
    await db_session.refresh(item)
    await db_session.refresh(s1)
    await db_session.refresh(ep1)
    await db_session.refresh(ep2)

    assert ep1.status == EpisodeStatus.COMPLETED
    assert ep2.status == EpisodeStatus.COMPLETED
    # Season 1 must roll up to COMPLETED even though it started as SEARCHING!
    assert s1.status == SeasonStatus.COMPLETED
    assert item.status == MediaStatus.COMPLETED

    # Verify Push Modal exposes Season-level [Delete from TorBox] button
    modal_resp = await async_client.get(f"/api/items/{item.id}/push-modal")
    assert modal_resp.status_code == 200
    assert f"season_id={s1.id}" in modal_resp.text
    assert "Delete from TorBox" in modal_resp.text

    # Now trigger Season-level manual_delete_transfer -> must cascade to both episode transfers!
    deleted_tb_ids: list[str] = []

    async def fake_delete_purge(
        session: AsyncSession,
        tb_id: str | None,
        tb_item: dict[str, Any] | None = None,
        downloader_type: str | None = "torbox",
    ) -> None:
        if tb_id:
            deleted_tb_ids.append(str(tb_id))

    monkeypatch.setattr(
        transfer_poller_module,
        "_delete_and_purge_transfer",
        fake_delete_purge,
    )

    res = await manual_delete_transfer(
        db_session, item_id=item.id, season_id=s1.id, episode_id=None
    )
    assert res["status"] == "ok"
    assert set(deleted_tb_ids) == {"tb-ep1", "tb-ep2"}

    await db_session.refresh(item, ["seasons"])
    await db_session.refresh(s1, ["episodes"])
    await db_session.refresh(ep1)
    await db_session.refresh(ep2)
    await db_session.refresh(h1)
    await db_session.refresh(h2)

    assert h1.status_detail == "deleted"
    assert h2.status_detail == "deleted"
    assert s1.status == SeasonStatus.SEARCHING
    assert ep1.status == EpisodeStatus.SEARCHING
    assert ep2.status == EpisodeStatus.SEARCHING
    assert item.status == MediaStatus.SEARCHING
    assert item.status_tier == "ready_to_push"


@pytest.mark.asyncio
async def test_delete_history_entry_and_startup_reconciliation_reset_stuck_entities(
    db_session: AsyncSession,
    async_client: AsyncClient,
) -> None:
    """Deleting a history row or running startup reconciliation resets COMPLETED entities with 0 active/completed transfers to SEARCHING."""
    movie = await make_movie(
        db_session,
        title="Civil War",
        year=2024,
        release_date=datetime(2024, 5, 24, tzinfo=timezone.utc),
        status=MediaStatus.COMPLETED,
    )
    hist = await make_history(
        db_session,
        item=movie,
        downloader_transfer_id="tb-civilwar",
        status_detail="completed",
    )
    await db_session.commit()

    # Delete the history row via API
    del_resp = await async_client.delete(
        f"/api/history/{hist.id}", headers={"HX-Request": "true"}
    )
    assert del_resp.status_code == 200
    assert del_resp.headers.get("HX-Trigger") == "refreshDashboard"

    await db_session.refresh(movie)
    assert movie.status == MediaStatus.SEARCHING
    assert movie.status_tier == "ready_to_push"

    # Now create a stuck "COMPLETED" Series with Season 1 DOWNLOADED and Episode 1 DOWNLOADED but zero DownloadHistory rows
    stuck_show, stuck_seasons, stuck_eps = await make_series(
        db_session,
        title="Stuck Anime",
        media_type=MediaType.ANIME,
        year=2023,
        release_date=datetime(2023, 4, 1, tzinfo=timezone.utc),
        status=MediaStatus.COMPLETED,
        seasons=[{"season_number": 1, "status": SeasonStatus.DOWNLOADED}],
        episodes_per_season=1,
        episode_statuses=[EpisodeStatus.DOWNLOADED],
    )
    await db_session.commit()

    await reconcile_all_items_transfer_truth(db_session)
    await db_session.commit()

    await db_session.refresh(stuck_show)
    await db_session.refresh(stuck_seasons[0])
    await db_session.refresh(stuck_eps[0])

    assert stuck_eps[0].status == EpisodeStatus.SEARCHING
    assert stuck_seasons[0].status == SeasonStatus.SEARCHING
    assert stuck_show.status == MediaStatus.SEARCHING
    assert stuck_show.status_tier == "ready_to_push"


@pytest.mark.asyncio
async def test_sequel_season_simkl_id_prioritized_in_history(
    db_session: AsyncSession,
) -> None:
    """Consolidated Anime sequel Season.simkl_id takes priority over parent MediaItem.simkl_id in History."""
    anime, seasons, _ = await make_series(
        db_session,
        title="Attack on Titan",
        media_type=MediaType.ANIME,
        simkl_id=1001,
        anilist_id=16498,
        seasons=[
            {
                "season_number": 1,
                "simkl_id": 1001,
                "anilist_id": 16498,
                "title": "Season 1",
            },
            {
                "season_number": 2,
                "simkl_id": 2002,
                "anilist_id": 20958,
                "title": "Season 2",
            },
        ],
    )
    s2 = seasons[1]
    hist = await make_history(
        db_session,
        item=anime,
        season=s2,
        nzb_title="Attack.on.Titan.S02.1080p.BluRay.x265-GRP",
        status_detail="completed",
    )
    populate_history_snapshot(hist, anime, season=s2)
    await db_session.commit()

    assert hist.simkl_id == 2002
    assert hist.anilist_id == 20958

    ledger = await get_push_history_ledger(db_session)
    s2_row = next(r for r in ledger if r["id"] == hist.id)
    assert s2_row["simkl_id"] == 2002
    assert s2_row["anilist_id"] == 20958

    # Also verify after detaching item download history
    await detach_item_download_history(db_session, anime)
    await db_session.commit()
    await db_session.refresh(hist)
    assert hist.simkl_id == 2002
    assert hist.anilist_id == 20958
