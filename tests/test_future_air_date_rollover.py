"""Unit tests for future episode release detection and air date rollover."""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.transfer_poller import _sync_season_status_from_episodes
from app.db.models import Episode, EpisodeStatus, MediaStatus, Season, SeasonStatus


def test_sync_season_status_future_to_searching():
    """A season initially in SeasonStatus.FUTURE with 1 SEARCHING episode and 2 FUTURE episodes rolls up to SEARCHING."""
    season = Season(
        id=1,
        season_number=1,
        status=SeasonStatus.FUTURE,
        air_date=datetime.now(timezone.utc) + timedelta(days=10),
    )
    season.episodes = [
        Episode(id=1, episode_number=1, status=EpisodeStatus.SEARCHING),
        Episode(id=2, episode_number=2, status=EpisodeStatus.FUTURE),
        Episode(id=3, episode_number=3, status=EpisodeStatus.FUTURE),
    ]
    season.download_history = []

    _sync_season_status_from_episodes(season)

    assert season.status == SeasonStatus.SEARCHING


def test_sync_season_status_all_future_remains_future():
    """A season initially in SeasonStatus.FUTURE with all FUTURE episodes remains SeasonStatus.FUTURE."""
    season = Season(
        id=1,
        season_number=1,
        status=SeasonStatus.FUTURE,
        air_date=datetime.now(timezone.utc) + timedelta(days=10),
    )
    season.episodes = [
        Episode(id=1, episode_number=1, status=EpisodeStatus.FUTURE),
        Episode(id=2, episode_number=2, status=EpisodeStatus.FUTURE),
        Episode(id=3, episode_number=3, status=EpisodeStatus.FUTURE),
    ]
    season.download_history = []

    _sync_season_status_from_episodes(season)

    assert season.status == SeasonStatus.FUTURE


def test_sync_season_status_all_non_future_completed_rolls_up_to_completed():
    """A season with all non-future episodes COMPLETED rolls up to SeasonStatus.COMPLETED."""
    season = Season(
        id=1,
        season_number=1,
        status=SeasonStatus.FUTURE,
        air_date=datetime.now(timezone.utc) + timedelta(days=10),
    )
    season.episodes = [
        Episode(id=1, episode_number=1, status=EpisodeStatus.COMPLETED),
        Episode(id=2, episode_number=2, status=EpisodeStatus.COMPLETED),
        Episode(id=3, episode_number=3, status=EpisodeStatus.FUTURE),
    ]
    season.download_history = []

    _sync_season_status_from_episodes(season)

    assert season.status == SeasonStatus.COMPLETED


def test_sync_season_status_ignored_season_is_skipped():
    """An IGNORED season is never modified."""
    season = Season(
        id=1,
        season_number=1,
        status=SeasonStatus.IGNORED,
    )
    season.episodes = [
        Episode(id=1, episode_number=1, status=EpisodeStatus.SEARCHING),
    ]
    season.download_history = []

    _sync_season_status_from_episodes(season)

    assert season.status == SeasonStatus.IGNORED


@pytest.mark.asyncio
async def test_rollover_due_future_episodes_and_seasons(db_session):
    """Past-due episodes transition to SEARCHING, future remain FUTURE, parent rollups work."""
    from app.core.automation import rollover_due_future_episodes_and_seasons
    from app.db.models import MediaItem, MediaType, Provider

    now = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    past = now - timedelta(days=2)
    future = now + timedelta(days=2)

    # Create provider
    provider = Provider(name="Test", type="simkl", is_active=True)
    db_session.add(provider)
    await db_session.flush()

    # Create MediaItem in FUTURE status
    item = MediaItem(
        title="Test Show",
        media_type=MediaType.SHOW,
        status=MediaStatus.FUTURE,
        year=2026,
        release_date=past,
        provider_id=provider.id,
    )
    db_session.add(item)
    await db_session.flush()

    # Season 1 in FUTURE status, air_date in past
    s1 = Season(
        media_item_id=item.id,
        season_number=1,
        status=SeasonStatus.FUTURE,
        air_date=past,
    )
    db_session.add(s1)
    await db_session.flush()

    # Ep 1: past air date (due)
    ep1 = Episode(
        season_id=s1.id,
        episode_number=1,
        status=EpisodeStatus.FUTURE,
        air_date=past,
    )
    # Ep 2: future air date (not due)
    ep2 = Episode(
        season_id=s1.id,
        episode_number=2,
        status=EpisodeStatus.FUTURE,
        air_date=future,
    )
    db_session.add_all([ep1, ep2])
    await db_session.commit()

    res = await rollover_due_future_episodes_and_seasons(db_session, now=now)
    assert res == {
        "episodes_rolled_over": 1,
        "seasons_rolled_over": 1,
        "items_affected": 1,
    }

    await db_session.refresh(ep1)
    await db_session.refresh(ep2)
    await db_session.refresh(s1)
    await db_session.refresh(item)

    assert ep1.status == EpisodeStatus.SEARCHING
    assert ep2.status == EpisodeStatus.FUTURE
    assert s1.status == SeasonStatus.SEARCHING
    assert item.status == MediaStatus.SEARCHING
    assert item.status_tier == "ready_to_push"


@pytest.mark.asyncio
async def test_rollover_due_future_episodes_and_seasons_noop(db_session):
    """When nothing is due, returns zero counts and performs no mutations."""
    from app.core.automation import rollover_due_future_episodes_and_seasons

    now = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    res = await rollover_due_future_episodes_and_seasons(db_session, now=now)
    assert res == {
        "episodes_rolled_over": 0,
        "seasons_rolled_over": 0,
        "items_affected": 0,
    }


@pytest.mark.asyncio
async def test_refresh_due_future_tv_seasons(db_session, monkeypatch):
    """Paced refresh populates episodes/air dates and triggers rollover/rollup."""
    from unittest.mock import AsyncMock

    from app.core.automation import refresh_due_future_tv_seasons
    from app.db.models import MediaItem, MediaType, Provider

    now = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)

    provider = Provider(name="Test", type="simkl", is_active=True)
    db_session.add(provider)
    await db_session.flush()

    item = MediaItem(
        title="Future Show",
        media_type=MediaType.SHOW,
        status=MediaStatus.FUTURE,
        year=2026,
        tmdb_id=12345,
        provider_id=provider.id,
        last_metadata_refreshed_at=now - timedelta(hours=25),
    )
    db_session.add(item)
    await db_session.flush()

    season = Season(
        media_item_id=item.id,
        season_number=1,
        status=SeasonStatus.FUTURE,
        air_date=None,
    )
    db_session.add(season)
    await db_session.commit()

    # Mock TMDB returning season details with 3 episodes: 2 in past, 1 in future
    mock_season_details = {
        "air_date": "2026-10-01",
        "episodes": [
            {
                "episode_number": 1,
                "air_date": "2026-10-01",
                "name": "Ep 1",
            },
            {
                "episode_number": 2,
                "air_date": "2026-10-08",
                "name": "Ep 2",
            },
            {
                "episode_number": 3,
                "air_date": "2099-01-01",
                "name": "Ep 3",
            },
        ],
    }

    from app.services import tmdb

    monkeypatch.setattr(
        tmdb,
        "get_season_details",
        AsyncMock(return_value=mock_season_details),
    )

    refreshed_count = await refresh_due_future_tv_seasons(db_session, now=now)
    assert refreshed_count == 1

    await db_session.refresh(season)
    await db_session.refresh(item)

    season_air_utc = (
        season.air_date.replace(tzinfo=timezone.utc)
        if season.air_date and season.air_date.tzinfo is None
        else season.air_date
    )
    assert season_air_utc == datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    assert len(season.episodes) == 3

    # Ep 1 and Ep 2 past due -> SEARCHING; Ep 3 future -> FUTURE
    ep1 = next(ep for ep in season.episodes if ep.episode_number == 1)
    ep2 = next(ep for ep in season.episodes if ep.episode_number == 2)
    ep3 = next(ep for ep in season.episodes if ep.episode_number == 3)
    assert ep1.status == EpisodeStatus.SEARCHING
    assert ep2.status == EpisodeStatus.SEARCHING
    assert ep3.status == EpisodeStatus.FUTURE

    # Since at least one episode is SEARCHING, season rolls up to SEARCHING
    assert season.status == SeasonStatus.SEARCHING
    item_refreshed_utc = (
        item.last_metadata_refreshed_at.replace(tzinfo=timezone.utc)
        if item.last_metadata_refreshed_at
        and item.last_metadata_refreshed_at.tzinfo is None
        else item.last_metadata_refreshed_at
    )
    assert item_refreshed_utc == now
    assert item.status == MediaStatus.SEARCHING


@pytest.mark.asyncio
async def test_refresh_due_future_tv_seasons_pacing_skips_recent(
    db_session, monkeypatch
):
    """Seasons refreshed < 24 hours ago are skipped to prevent excessive API queries."""
    from unittest.mock import AsyncMock

    from app.core.automation import refresh_due_future_tv_seasons
    from app.db.models import MediaItem, MediaType, Provider
    from app.services import tmdb

    now = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)

    provider = Provider(name="Test", type="simkl", is_active=True)
    db_session.add(provider)
    await db_session.flush()

    item = MediaItem(
        title="Recent Show",
        media_type=MediaType.SHOW,
        status=MediaStatus.FUTURE,
        year=2026,
        tmdb_id=12345,
        provider_id=provider.id,
        last_metadata_refreshed_at=now - timedelta(hours=2),
    )
    db_session.add(item)
    await db_session.flush()

    season = Season(
        media_item_id=item.id,
        season_number=1,
        status=SeasonStatus.FUTURE,
        air_date=None,
    )
    db_session.add(season)
    await db_session.commit()

    mock_tmdb = AsyncMock()
    monkeypatch.setattr(tmdb, "get_season_details", mock_tmdb)

    refreshed_count = await refresh_due_future_tv_seasons(db_session, now=now)
    assert refreshed_count == 0
    mock_tmdb.assert_not_called()


@pytest.mark.asyncio
async def test_sync_all_providers_future_rollover_integration(db_session, monkeypatch):
    """Simulate a Simkl sync containing Marshals S2 with Ep 1 past air date and Ep 2 future-dated.

    Verify that Season 2 is SEARCHING, Ep 1 is SEARCHING, Ep 2 is FUTURE, and
    the item classifies as Section 2 ('ready_to_push').
    """

    from app.core.automation import classify_v3_status_tier, sync_all_providers
    from app.db.models import MediaItem, Provider
    from app.services import tmdb
    from app.services.watchlists import simkl

    now = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    past_date_str = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    future_date_str = (now + timedelta(days=5)).strftime("%Y-%m-%d")

    # 1. Seed active provider
    provider = Provider(
        name="Simkl Test",
        type="simkl",
        is_active=True,
        client_id="cid_123",
        access_token="tok_123",
        config_json='{"sync_movies": false, "sync_series": true, "sync_anime": false}',
    )
    db_session.add(provider)
    await db_session.commit()

    # 2. Mock Simkl credentials and show list
    async def mock_token(session, prov):
        return prov.client_id, prov.access_token

    monkeypatch.setattr(simkl, "ensure_valid_simkl_token", mock_token)

    simkl_shows = [
        {
            "show": {
                "title": "Marshals",
                "year": 2026,
                "ids": {"simkl": 99999, "tmdb": 88888},
            }
        }
    ]

    async def mock_fetch_category(session, prov, cat, cid, tok):
        return simkl_shows, cid, tok

    from app.services.watchlists import simkl as simkl_mod

    monkeypatch.setattr(
        simkl_mod, "_fetch_simkl_category_with_retry", mock_fetch_category
    )

    # 3. Mock TMDB show details & season details
    async def mock_show_details(tmdb_id):
        return {
            "name": "Marshals",
            "first_air_date": past_date_str,
            "seasons": [
                {
                    "season_number": 2,
                    "air_date": past_date_str,
                    "episode_count": 2,
                }
            ],
        }

    async def mock_season_details(tmdb_id, season_num, session=None):
        return {
            "season_number": 2,
            "air_date": past_date_str,
            "episodes": [
                {
                    "episode_number": 1,
                    "name": "Marshals S02E01",
                    "air_date": past_date_str,
                },
                {
                    "episode_number": 2,
                    "name": "Marshals S02E02",
                    "air_date": future_date_str,
                },
            ],
        }

    monkeypatch.setattr(tmdb, "get_show_details", mock_show_details)
    monkeypatch.setattr(tmdb, "get_season_details", mock_season_details)

    # 4. Run sync_all_providers
    await sync_all_providers()

    # 5. Query and assert states
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    stmt = (
        select(MediaItem)
        .where(MediaItem.tmdb_id == 88888)
        .options(
            selectinload(MediaItem.seasons).selectinload(Season.episodes),
            selectinload(MediaItem.download_history),
        )
    )
    item = (await db_session.execute(stmt)).scalars().first()
    assert item is not None
    assert item.title == "Marshals"
    assert item.status == MediaStatus.SEARCHING
    assert len(item.seasons) == 1

    season2 = item.seasons[0]
    assert season2.season_number == 2
    assert season2.status == SeasonStatus.SEARCHING
    assert len(season2.episodes) == 2

    ep1 = next(ep for ep in season2.episodes if ep.episode_number == 1)
    ep2 = next(ep for ep in season2.episodes if ep.episode_number == 2)
    assert ep1.status == EpisodeStatus.SEARCHING
    assert ep2.status == EpisodeStatus.FUTURE

    # Item appears in Section 2 ("Ready to Push" / wanted & unpushed)
    tier = classify_v3_status_tier(item)
    assert tier == "ready_to_push"


@pytest.mark.asyncio
async def test_run_orchestrator_tick_rolls_over_episodes(db_session, monkeypatch):
    """The 15-minute orchestrator tick rolls over due future episodes automatically."""
    from app.core.automation import run_orchestrator_tick
    from app.db.models import MediaItem, MediaType, Provider

    now = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    due_date = now - timedelta(hours=1)

    provider = Provider(name="Tick Provider", type="simkl", is_active=True)
    db_session.add(provider)
    await db_session.flush()

    item = MediaItem(
        title="Tick Show",
        media_type=MediaType.SHOW,
        status=MediaStatus.FUTURE,
        year=2026,
        provider_id=provider.id,
    )
    db_session.add(item)
    await db_session.flush()

    season = Season(
        media_item_id=item.id,
        season_number=1,
        status=SeasonStatus.FUTURE,
        air_date=due_date,
    )
    db_session.add(season)
    await db_session.flush()

    ep = Episode(
        season_id=season.id,
        episode_number=1,
        status=EpisodeStatus.FUTURE,
        air_date=due_date,
        monitored=True,
    )
    db_session.add(ep)
    await db_session.commit()

    # Prevent periodic Simkl sync from running during tick test
    from app.core import automation as auto_mod

    monkeypatch.setattr(
        auto_mod, "is_watchlist_periodic_sync_due", lambda prov, now=None: False
    )

    await run_orchestrator_tick(now=now)

    await db_session.refresh(ep)
    await db_session.refresh(season)
    await db_session.refresh(item)

    assert ep.status == EpisodeStatus.SEARCHING
    assert season.status == SeasonStatus.SEARCHING
    assert item.status == MediaStatus.SEARCHING


@pytest.mark.asyncio
async def test_get_season_details_404_logs_warning_not_error(monkeypatch):
    """TMDB get_season_details logs 404 as WARNING and does not log ERROR."""
    from unittest.mock import AsyncMock, MagicMock, patch

    import httpx

    from app.services import tmdb

    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.status_code = 404
    mock_req = MagicMock(spec=httpx.Request)
    err = httpx.HTTPStatusError("404 Not Found", request=mock_req, response=mock_resp)

    mock_client = AsyncMock()
    mock_client.get.side_effect = err
    mock_client_cls = MagicMock(return_value=mock_client)
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_cls)

    with (
        patch.object(tmdb.logger, "warning") as mock_warn,
        patch.object(tmdb.logger, "error") as mock_err,
    ):
        res = await tmdb.get_season_details(60625, 10, api_key="dummy_key")

    assert res is None
    # Verify WARNING was logged, and no ERROR was logged
    mock_warn.assert_called_once()
    assert "404" in mock_warn.call_args[0][0]
    mock_err.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_due_future_tv_seasons_boundary_check(db_session, monkeypatch):
    """Season number beyond TMDB show's known seasons is skipped without calling get_season_details."""
    from unittest.mock import AsyncMock

    from app.core.automation import refresh_due_future_tv_seasons
    from app.db.models import MediaItem, MediaType, Provider, Season, SeasonStatus
    from app.services import tmdb

    now = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)

    provider = Provider(name="Test Prov", type="simkl", is_active=True)
    db_session.add(provider)
    await db_session.flush()

    item = MediaItem(
        title="Rick and Morty",
        media_type=MediaType.SHOW,
        status=MediaStatus.FUTURE,
        year=2013,
        tmdb_id=60625,
        provider_id=provider.id,
        last_metadata_refreshed_at=now - timedelta(hours=30),
    )
    db_session.add(item)
    await db_session.flush()

    season = Season(
        media_item_id=item.id,
        season_number=10,
        status=SeasonStatus.FUTURE,
        air_date=None,
    )
    db_session.add(season)
    await db_session.commit()

    # TMDB show details returns seasons up to 9
    mock_show = AsyncMock(
        return_value={
            "number_of_seasons": 9,
            "seasons": [{"season_number": i} for i in range(1, 10)],
        }
    )
    mock_season = AsyncMock()

    monkeypatch.setattr(tmdb, "get_show_details", mock_show)
    monkeypatch.setattr(tmdb, "get_season_details", mock_season)

    refreshed_count = await refresh_due_future_tv_seasons(db_session, now=now)
    assert refreshed_count == 0

    # get_season_details should NOT have been called for S10!
    mock_season.assert_not_called()

    # But item.last_metadata_refreshed_at should be updated to now!
    await db_session.refresh(item)
    refreshed_utc = (
        item.last_metadata_refreshed_at.replace(tzinfo=timezone.utc)
        if item.last_metadata_refreshed_at
        and item.last_metadata_refreshed_at.tzinfo is None
        else item.last_metadata_refreshed_at
    )
    assert refreshed_utc == now


@pytest.mark.asyncio
async def test_refresh_due_future_tv_seasons_updates_timestamp_on_none_details(
    db_session, monkeypatch
):
    """When get_season_details returns None (404), item.last_metadata_refreshed_at is still stamped with now."""
    from unittest.mock import AsyncMock

    from app.core.automation import refresh_due_future_tv_seasons
    from app.db.models import MediaItem, MediaType, Provider, Season, SeasonStatus
    from app.services import tmdb

    now = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)

    provider = Provider(name="Test Prov 2", type="simkl", is_active=True)
    db_session.add(provider)
    await db_session.flush()

    item = MediaItem(
        title="Show 404",
        media_type=MediaType.SHOW,
        status=MediaStatus.FUTURE,
        year=2024,
        tmdb_id=99999,
        provider_id=provider.id,
        last_metadata_refreshed_at=None,
    )
    db_session.add(item)
    await db_session.flush()

    season = Season(
        media_item_id=item.id,
        season_number=2,
        status=SeasonStatus.FUTURE,
        air_date=None,
    )
    db_session.add(season)
    await db_session.commit()

    # get_show_details returns None (fallback to season details)
    mock_show = AsyncMock(return_value=None)
    # get_season_details returns None (404)
    mock_season = AsyncMock(return_value=None)

    monkeypatch.setattr(tmdb, "get_show_details", mock_show)
    monkeypatch.setattr(tmdb, "get_season_details", mock_season)

    refreshed_count = await refresh_due_future_tv_seasons(db_session, now=now)
    assert refreshed_count == 0

    # Verify item.last_metadata_refreshed_at was persisted as now
    await db_session.refresh(item)
    refreshed_utc = (
        item.last_metadata_refreshed_at.replace(tzinfo=timezone.utc)
        if item.last_metadata_refreshed_at
        and item.last_metadata_refreshed_at.tzinfo is None
        else item.last_metadata_refreshed_at
    )
    assert refreshed_utc == now
