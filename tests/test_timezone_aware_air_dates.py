"""Unit tests for Simkl episodes API client, broadcast date parsing, and air date checks."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.core.automation import is_episode_aired, parse_broadcast_air_date
from app.core.phase_stats import WatchlistPeriodicSyncStatus
from app.services.watchlists.simkl import get_episodes
from tests.conftest import make_series

NOW_REFRESH = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)


class TestParseBroadcastAirDate:
    """Tests for parse_broadcast_air_date in app.core.automation."""

    @pytest.mark.parametrize(
        ("date_str", "expected_utc"),
        [
            # Exact ISO string with negative offset: 2026-10-11 20:00:00 -04:00 -> 2026-10-12 00:00:00 UTC
            (
                "2026-10-11T20:00:00-04:00",
                datetime(2026, 10, 12, 0, 0, 0, tzinfo=timezone.utc),
            ),
            # Exact ISO string with UTC 'Z'
            (
                "2026-10-11T15:30:00Z",
                datetime(2026, 10, 11, 15, 30, 0, tzinfo=timezone.utc),
            ),
            # Exact ISO string with positive offset: 2026-10-11 10:00:00 +02:00 -> 2026-10-11 08:00:00 UTC
            (
                "2026-10-11T10:00:00+02:00",
                datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc),
            ),
            # Date-only string -> midnight UTC
            (
                "2026-10-11",
                datetime(2026, 10, 11, 0, 0, 0, tzinfo=timezone.utc),
            ),
        ],
    )
    def test_parse_valid_broadcast_air_dates(
        self, date_str: str, expected_utc: datetime
    ) -> None:
        result = parse_broadcast_air_date(date_str)
        assert result is not None
        assert result.replace(microsecond=0) == expected_utc
        assert result.tzinfo is not None
        if "T" not in date_str and " " not in date_str:
            assert result.microsecond == 1

    @pytest.mark.parametrize(
        "invalid_date_str",
        [
            None,
            "",
            "   ",
            "not-a-date",
            "2026/10/11",
            "2026-13-45T99:99:99",
        ],
    )
    def test_parse_invalid_or_missing_date_returns_none(
        self, invalid_date_str: str | None
    ) -> None:
        assert parse_broadcast_air_date(invalid_date_str) is None


class TestIsEpisodeAired:
    """Tests for is_episode_aired in app.core.automation."""

    def test_midnight_air_date_requires_24h_buffer(self) -> None:
        # Midnight air_date (date-only fallback)
        air_date = datetime(2026, 10, 11, 0, 0, 0, tzinfo=timezone.utc)

        # Before 24h elapsed (e.g. at 20:00 UTC on same date) -> False
        same_day_time = datetime(2026, 10, 11, 20, 0, 0, tzinfo=timezone.utc)
        assert is_episode_aired(air_date, same_day_time) is False

        # Exactly at 23:59:59 UTC -> False
        assert (
            is_episode_aired(
                air_date,
                air_date + timedelta(hours=23, minutes=59, seconds=59),
            )
            is False
        )

        # Exactly at 24:00 (i.e. air_date + 24 hours) -> True
        assert is_episode_aired(air_date, air_date + timedelta(hours=24)) is True

        # After 24h buffer (next day) -> True
        next_day = datetime(2026, 10, 12, 1, 0, 0, tzinfo=timezone.utc)
        assert is_episode_aired(air_date, next_day) is True

    def test_exact_broadcast_time_evaluates_directly(self) -> None:
        # Non-midnight air_date: 2026-10-11 20:00:00 UTC
        air_date = datetime(2026, 10, 11, 20, 0, 0, tzinfo=timezone.utc)

        # 1 minute before -> False
        before = air_date - timedelta(minutes=1)
        assert is_episode_aired(air_date, before) is False

        # Exactly on time -> True
        assert is_episode_aired(air_date, air_date) is True

        # 5 minutes after -> True
        after = air_date + timedelta(minutes=5)
        assert is_episode_aired(air_date, after) is True


class TestSimklGetEpisodes:
    """Tests for get_episodes in app.services.watchlists.simkl."""

    @pytest.mark.asyncio
    async def test_get_episodes_success(self) -> None:
        mock_payload = [
            {
                "title": "Episode 1",
                "season": 1,
                "episode": 1,
                "date": "2026-10-11T20:00:00-04:00",
            },
            {
                "title": "Episode 2",
                "season": 1,
                "episode": 2,
                "date": "2026-10-18T20:00:00-04:00",
            },
        ]
        mock_request = httpx.Request("GET", "https://api.simkl.com/tv/episodes/12345")
        mock_response = httpx.Response(200, json=mock_payload, request=mock_request)

        with (
            patch(
                "app.services.watchlists.simkl._limiter.wait", new_callable=AsyncMock
            ),
            patch("httpx.AsyncClient.get", return_value=mock_response) as mock_get,
        ):
            result = await get_episodes(
                simkl_id=12345, client_id="test_key", media_type="tv"
            )

            assert result == mock_payload
            mock_get.assert_called_once()
            args, kwargs = mock_get.call_args
            assert args[0] == "https://api.simkl.com/tv/episodes/12345"
            assert kwargs["params"] == {"extended": "full"}
            assert kwargs["headers"]["simkl-api-key"] == "test_key"

    @pytest.mark.asyncio
    async def test_get_episodes_handles_error_gracefully(self) -> None:
        with (
            patch(
                "app.services.watchlists.simkl._limiter.wait", new_callable=AsyncMock
            ),
            patch(
                "httpx.AsyncClient.get", side_effect=httpx.HTTPError("Network failure")
            ),
        ):
            result = await get_episodes(
                simkl_id=12345, client_id="test_key", media_type="tv"
            )
            assert result == []


class TestEnrichActiveShowsEpisodeAirDates:
    """Tests for enrich_active_shows_episode_air_dates selective sync gate and precision."""

    @pytest.mark.asyncio
    async def test_enrich_skips_fully_completed_backlog_series(
        self,
        db_session,
        watchlist_provider,
    ) -> None:
        from app.core.air_dates import enrich_active_shows_episode_air_dates
        from app.db.models import EpisodeStatus, SeasonStatus

        # Create a backlog series where all seasons and episodes are COMPLETED
        item, seasons, episodes = await make_series(
            db_session,
            simkl_id=99901,
            provider_id=watchlist_provider.id,
            seasons=[{"season_number": 1, "status": SeasonStatus.COMPLETED}],
            episodes_per_season=2,
            episode_statuses=[EpisodeStatus.COMPLETED, EpisodeStatus.COMPLETED],
        )

        with patch(
            "app.services.watchlists.simkl.get_episodes", new_callable=AsyncMock
        ) as mock_get_episodes:
            count = await enrich_active_shows_episode_air_dates(db_session)
            assert count == 0
            mock_get_episodes.assert_not_called()

    @pytest.mark.asyncio
    async def test_enrich_updates_active_upcoming_series_episodes(
        self,
        db_session,
        watchlist_provider,
    ) -> None:
        from app.core.air_dates import enrich_active_shows_episode_air_dates
        from app.db.models import EpisodeStatus, SeasonStatus

        # Configure provider credentials
        watchlist_provider.client_id = "test_cid"
        watchlist_provider.access_token = "test_token"
        await db_session.commit()

        # Create an active series with FUTURE episodes
        item, seasons, episodes = await make_series(
            db_session,
            simkl_id=99902,
            provider_id=watchlist_provider.id,
            seasons=[{"season_number": 1, "status": SeasonStatus.FUTURE}],
            episodes_per_season=2,
            episode_statuses=[EpisodeStatus.FUTURE, EpisodeStatus.FUTURE],
        )

        simkl_payload = [
            {
                "season": 1,
                "episode": 1,
                "date": "2026-10-11T20:00:00-04:00",  # -> 2026-10-12 00:00:00 UTC
            },
            {
                "season": 1,
                "episode": 2,
                "date": "2026-10-18T20:00:00-04:00",  # -> 2026-10-19 00:00:00 UTC
            },
        ]

        with patch(
            "app.services.watchlists.simkl.get_episodes",
            new_callable=AsyncMock,
            return_value=simkl_payload,
        ) as mock_get_episodes:
            count = await enrich_active_shows_episode_air_dates(db_session)
            assert count == 2
            mock_get_episodes.assert_called_once()

        # Reload episodes
        await db_session.refresh(episodes[0])
        await db_session.refresh(episodes[1])

        ep0_air = (
            episodes[0].air_date.replace(tzinfo=timezone.utc)
            if episodes[0].air_date and episodes[0].air_date.tzinfo is None
            else episodes[0].air_date
        )
        ep1_air = (
            episodes[1].air_date.replace(tzinfo=timezone.utc)
            if episodes[1].air_date and episodes[1].air_date.tzinfo is None
            else episodes[1].air_date
        )

        assert ep0_air.replace(microsecond=0) == datetime(
            2026, 10, 12, 0, 0, 0, tzinfo=timezone.utc
        )
        assert ep1_air.replace(microsecond=0) == datetime(
            2026, 10, 19, 0, 0, 0, tzinfo=timezone.utc
        )

    @pytest.mark.asyncio
    async def test_enrich_skips_searching_backlog_only_show(
        self,
        db_session,
        watchlist_provider,
    ) -> None:
        from app.core.air_dates import enrich_active_shows_episode_air_dates
        from app.db.models import EpisodeStatus, SeasonStatus

        watchlist_provider.client_id = "test_cid"
        watchlist_provider.access_token = "test_token"
        await db_session.commit()

        await make_series(
            db_session,
            simkl_id=99904,
            provider_id=watchlist_provider.id,
            seasons=[{"season_number": 1, "status": SeasonStatus.SEARCHING}],
            episodes_per_season=2,
            episode_statuses=[EpisodeStatus.SEARCHING, EpisodeStatus.SEARCHING],
        )

        with patch(
            "app.services.watchlists.simkl.get_episodes", new_callable=AsyncMock
        ) as mock_get_episodes:
            assert await enrich_active_shows_episode_air_dates(db_session) == 0
            mock_get_episodes.assert_not_called()

    @pytest.mark.asyncio
    async def test_enrich_imminent_future_show_queried_once_and_stamped(
        self,
        db_session,
        watchlist_provider,
    ) -> None:
        from app.core.air_dates import enrich_active_shows_episode_air_dates
        from app.db.models import EpisodeStatus, SeasonStatus

        watchlist_provider.client_id = "test_cid"
        watchlist_provider.access_token = "test_token"
        await db_session.commit()

        item, _, episodes = await make_series(
            db_session,
            simkl_id=99905,
            provider_id=watchlist_provider.id,
            seasons=[{"season_number": 1, "status": SeasonStatus.FUTURE}],
            episodes_per_season=1,
            episode_statuses=[EpisodeStatus.FUTURE],
        )
        episodes[0].air_date = NOW_REFRESH + timedelta(hours=10)
        await db_session.commit()

        with patch(
            "app.services.watchlists.simkl.get_episodes",
            new_callable=AsyncMock,
            return_value=[],  # empty response must still stamp
        ) as mock_get_episodes:
            await enrich_active_shows_episode_air_dates(db_session, now=NOW_REFRESH)
            mock_get_episodes.assert_called_once()

            await enrich_active_shows_episode_air_dates(
                db_session, now=NOW_REFRESH + timedelta(hours=1)
            )
            mock_get_episodes.assert_called_once()  # paced: no second call

        await db_session.refresh(item)
        assert item.last_air_dates_refreshed_at is not None

    @pytest.mark.asyncio
    async def test_enrich_skips_far_future_show(
        self,
        db_session,
        watchlist_provider,
    ) -> None:
        from app.core.air_dates import enrich_active_shows_episode_air_dates
        from app.db.models import EpisodeStatus, SeasonStatus

        watchlist_provider.client_id = "test_cid"
        watchlist_provider.access_token = "test_token"
        await db_session.commit()

        item, _, episodes = await make_series(
            db_session,
            simkl_id=99906,
            provider_id=watchlist_provider.id,
            seasons=[{"season_number": 1, "status": SeasonStatus.FUTURE}],
            episodes_per_season=1,
            episode_statuses=[EpisodeStatus.FUTURE],
        )
        episodes[0].air_date = NOW_REFRESH + timedelta(days=10)
        await db_session.commit()

        with patch(
            "app.services.watchlists.simkl.get_episodes", new_callable=AsyncMock
        ) as mock_get_episodes:
            await enrich_active_shows_episode_air_dates(db_session, now=NOW_REFRESH)
            mock_get_episodes.assert_not_called()

    @pytest.mark.asyncio
    async def test_future_tba_movie_refreshed_via_tmdb_and_paced(
        self, db_session
    ) -> None:
        from app.core.air_dates import enrich_active_shows_episode_air_dates
        from app.db.models import MediaStatus
        from tests.conftest import make_movie

        movie = await make_movie(
            db_session,
            status=MediaStatus.FUTURE,
            release_date=None,
            tmdb_id=555,
            year=2026,
        )
        await db_session.commit()
        new_date = datetime(2026, 11, 1, tzinfo=timezone.utc)

        with patch(
            "app.services.tmdb.get_digital_release_date",
            new_callable=AsyncMock,
            return_value=new_date,
        ) as mock_tmdb:
            await enrich_active_shows_episode_air_dates(db_session, now=NOW_REFRESH)
            mock_tmdb.assert_called_once()
            await enrich_active_shows_episode_air_dates(
                db_session, now=NOW_REFRESH + timedelta(hours=1)
            )
            mock_tmdb.assert_called_once()

        await db_session.refresh(movie)
        assert movie.release_date is not None
        assert movie.release_date.replace(tzinfo=timezone.utc) == new_date
        assert movie.last_air_dates_refreshed_at is not None
        assert movie.status == MediaStatus.FUTURE

    @pytest.mark.asyncio
    async def test_anime_movie_without_tmdb_id_uses_anilist(self, db_session) -> None:
        from app.core.air_dates import enrich_active_shows_episode_air_dates
        from app.db.models import MediaStatus
        from tests.conftest import make_movie

        movie = await make_movie(
            db_session,
            status=MediaStatus.FUTURE,
            release_date=None,
            tmdb_id=None,
            anilist_id=777,
            is_anime_movie=True,
            year=2026,
        )
        await db_session.commit()

        with (
            patch(
                "app.services.tmdb.get_digital_release_date", new_callable=AsyncMock
            ) as mock_tmdb,
            patch(
                "app.services.anilist.get_anime_details_batch_by_ids",
                new_callable=AsyncMock,
                return_value={
                    777: {"startDate": {"year": 2026, "month": 10, "day": 1}}
                },
            ) as mock_anilist,
        ):
            await enrich_active_shows_episode_air_dates(db_session, now=NOW_REFRESH)
            mock_tmdb.assert_not_called()
            mock_anilist.assert_called_once()

        await db_session.refresh(movie)
        assert movie.release_date is not None
        assert movie.status == MediaStatus.SEARCHING  # released date is in the past
        assert movie.last_air_dates_refreshed_at is not None

    @pytest.mark.asyncio
    async def test_future_movie_without_ids_is_stamped_only(self, db_session) -> None:
        from app.core.air_dates import enrich_active_shows_episode_air_dates
        from app.db.models import MediaStatus
        from tests.conftest import make_movie

        movie = await make_movie(
            db_session,
            status=MediaStatus.FUTURE,
            release_date=None,
            tmdb_id=None,
            year=2026,
        )
        await db_session.commit()
        await enrich_active_shows_episode_air_dates(db_session, now=NOW_REFRESH)
        await db_session.refresh(movie)
        assert movie.last_air_dates_refreshed_at is not None
        assert movie.release_date is None


class TestRolloverDueFutureEpisodesPrecision:
    """Tests for rollover precision using is_episode_aired."""

    @pytest.mark.asyncio
    async def test_marshals_s02e02_rollover_precision(
        self,
        db_session,
        watchlist_provider,
    ) -> None:
        from app.core.automation import rollover_due_future_episodes_and_seasons
        from app.db.models import EpisodeStatus, SeasonStatus

        # Marshals S02E02 with air_date = 2026-10-12 00:00:00 UTC (Sunday 8 PM EDT)
        item, seasons, episodes = await make_series(
            db_session,
            simkl_id=99903,
            provider_id=watchlist_provider.id,
            seasons=[{"season_number": 2, "status": SeasonStatus.FUTURE}],
            episodes_per_season=2,
            episode_statuses=[EpisodeStatus.COMPLETED, EpisodeStatus.FUTURE],
        )
        ep2 = episodes[1]
        ep2.air_date = parse_broadcast_air_date("2026-10-11T20:00:00-04:00")
        assert ep2.air_date is not None
        await db_session.commit()

        # Check on Sunday 2026-10-11 22:00:00 UTC (2 hours before broadcast)
        sunday_check = datetime(2026, 10, 11, 22, 0, 0, tzinfo=timezone.utc)
        res_before = await rollover_due_future_episodes_and_seasons(
            db_session, now=sunday_check
        )
        assert res_before["episodes_rolled_over"] == 0

        await db_session.refresh(ep2)
        assert ep2.status == EpisodeStatus.FUTURE

        # Check once now_utc = 2026-10-12 00:01:00 UTC (after broadcast)
        broadcast_time = datetime(2026, 10, 12, 0, 1, 0, tzinfo=timezone.utc)
        res_after = await rollover_due_future_episodes_and_seasons(
            db_session, now=broadcast_time
        )
        assert res_after["episodes_rolled_over"] == 1

        await db_session.refresh(ep2)
        assert ep2.status == EpisodeStatus.SEARCHING

    @pytest.mark.asyncio
    async def test_effective_release_date_mixed_naive_and_aware_seasons(
        self,
        db_session,
        watchlist_provider,
    ) -> None:
        item, seasons, _ = await make_series(
            db_session,
            simkl_id=99904,
            provider_id=watchlist_provider.id,
            seasons=2,
            release_date=None,
        )
        seasons[0].air_date = datetime(2026, 10, 1)  # naive, as loaded from SQLite
        seasons[1].air_date = datetime(2026, 11, 1, tzinfo=timezone.utc)
        await db_session.refresh(item, ["seasons"])
        assert item.effective_release_date == datetime(2026, 10, 1, tzinfo=timezone.utc)


class TestIsAirDateRefreshDue:
    """Policy table for is_air_date_refresh_due (m49)."""

    @pytest.mark.parametrize(
        ("ep_status", "air_date", "stamp_age", "expected"),
        [
            ("SEARCHING", None, None, False),
            ("COMPLETED", None, None, False),
            ("FUTURE", None, None, True),
            ("FUTURE", None, timedelta(hours=2), False),
            ("FUTURE", None, timedelta(hours=25), True),
            ("FUTURE", datetime(2026, 10, 22, tzinfo=timezone.utc), None, False),
            ("FUTURE", datetime(2026, 10, 14, 11, tzinfo=timezone.utc), None, True),
            (
                "FUTURE",
                datetime(2026, 10, 14, 11, tzinfo=timezone.utc),
                timedelta(hours=3),
                False,
            ),
            (
                "FUTURE",
                datetime(2026, 10, 14, 11, tzinfo=timezone.utc),
                timedelta(hours=25),
                True,
            ),
            # Already past but not rolled over yet: not imminent-eligible
            ("FUTURE", datetime(2026, 10, 12, 6, tzinfo=timezone.utc), None, False),
        ],
    )
    @pytest.mark.asyncio
    async def test_episode_policy(
        self, db_session, watchlist_provider, ep_status, air_date, stamp_age, expected
    ) -> None:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.core.automation import is_air_date_refresh_due
        from app.db.models import EpisodeStatus, MediaItem, Season

        item, _, episodes = await make_series(
            db_session,
            provider_id=watchlist_provider.id,
            episodes_per_season=1,
            episode_statuses=[EpisodeStatus[ep_status]],
        )
        episodes[0].air_date = air_date
        item.last_air_dates_refreshed_at = (
            NOW_REFRESH - stamp_age if stamp_age is not None else None
        )
        await db_session.commit()

        loaded = (
            await db_session.execute(
                select(MediaItem)
                .where(MediaItem.id == item.id)
                .options(selectinload(MediaItem.seasons).selectinload(Season.episodes))
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert is_air_date_refresh_due(loaded, NOW_REFRESH) is expected


class TestOrchestratorTickAirDateEnrichment:
    @pytest.mark.asyncio
    async def test_tick_calls_rollover_then_enrichment_in_order(self):
        from app.core import air_dates, automation

        calls: list[str] = []

        async def _rollover(session, now=None):
            calls.append("rollover")
            return {"episodes_rolled_over": 0, "seasons_rolled_over": 0}

        async def _enrich(session, now=None, **kwargs):
            calls.append("enrich")
            return 0

        with (
            patch.object(
                automation, "rollover_due_future_episodes_and_seasons", _rollover
            ),
            patch.object(air_dates, "enrich_active_shows_episode_air_dates", _enrich),
            patch.object(
                automation,
                "evaluate_periodic_watchlist_sync",
                return_value=WatchlistPeriodicSyncStatus("due", 60, None, 0),
            ),
            patch.object(
                automation, "sync_all_providers", new_callable=AsyncMock
            ) as sync_mock,
        ):
            await automation.run_orchestrator_tick(datetime.now(timezone.utc))

        assert calls == ["rollover", "enrich"]
        sync_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tick_enrichment_failure_is_logged_and_sync_still_runs(self, caplog):
        from app.core import air_dates, automation

        with (
            patch.object(
                automation,
                "rollover_due_future_episodes_and_seasons",
                new_callable=AsyncMock,
            ),
            patch.object(
                air_dates,
                "enrich_active_shows_episode_air_dates",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ),
            patch.object(
                automation,
                "evaluate_periodic_watchlist_sync",
                return_value=WatchlistPeriodicSyncStatus("due", 60, None, 0),
            ),
            patch.object(
                automation, "sync_all_providers", new_callable=AsyncMock
            ) as sync_mock,
            caplog.at_level("ERROR"),
        ):
            await automation.run_orchestrator_tick(datetime.now(timezone.utc))

        assert "air date enrichment" in caplog.text
        sync_mock.assert_awaited_once()
