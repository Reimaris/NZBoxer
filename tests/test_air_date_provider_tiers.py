"""TMDB date-only tier for TV shows without a Simkl link."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.core.air_dates import (
    enrich_active_shows_episode_air_dates,
    keep_existing_air_date,
)
from app.db.models import EpisodeStatus, MediaType, SeasonStatus
from tests.conftest import make_series, make_watchlist_provider


async def _plex_show(db_session):
    provider = await make_watchlist_provider(db_session, "plex")
    item, _, episodes = await make_series(
        db_session,
        simkl_id=0,
        tmdb_id=4242,
        provider_id=provider.id,
        seasons=[{"season_number": 1, "status": SeasonStatus.FUTURE}],
        episodes_per_season=1,
        episode_statuses=[EpisodeStatus.FUTURE],
    )
    await db_session.commit()
    return item, episodes[0]


@pytest.mark.asyncio
async def test_plex_only_show_gets_date_only_tmdb_dates(db_session) -> None:
    item, ep = await _plex_show(db_session)
    payload = {"episodes": [{"episode_number": 1, "air_date": "2026-10-14"}]}
    with patch(
        "app.core.air_dates.tmdb.get_season_details",
        new_callable=AsyncMock,
        return_value=payload,
    ):
        count = await enrich_active_shows_episode_air_dates(db_session)
    await db_session.refresh(ep)
    await db_session.refresh(item)
    assert count == 1
    assert ep.air_date.replace(tzinfo=timezone.utc) == datetime(
        2026, 10, 14, 0, 0, 0, 1, tzinfo=timezone.utc
    )
    assert item.last_air_dates_refreshed_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "side_effect,return_value",
    [
        (
            httpx.HTTPStatusError(
                "429",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(429),
            ),
            None,
        ),
        (None, None),
    ],
)
async def test_tmdb_failure_stamps_pacing_and_keeps_dates(
    db_session, side_effect, return_value, caplog
) -> None:
    item, ep = await _plex_show(db_session)
    await db_session.refresh(ep)
    before = ep.air_date
    with patch(
        "app.core.air_dates.tmdb.get_season_details",
        new_callable=AsyncMock,
        side_effect=side_effect,
        return_value=return_value,
    ):
        count = await enrich_active_shows_episode_air_dates(db_session)
    await db_session.refresh(item)
    await db_session.refresh(ep)
    assert count == 0
    assert ep.air_date == before
    assert item.last_air_dates_refreshed_at is not None
    assert any(r.levelname == "WARNING" for r in caplog.records)


_EXACT = datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc)
_DATE_ONLY = datetime(2026, 10, 14, 0, 0, 0, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "existing,incoming,expected",
    [
        (_EXACT, _DATE_ONLY, True),
        (_EXACT, _DATE_ONLY + timedelta(days=1), False),
        (None, _DATE_ONLY, False),
        (_EXACT, _EXACT + timedelta(hours=1), False),
    ],
)
def test_keep_existing_air_date(existing, incoming, expected) -> None:
    assert keep_existing_air_date(existing, incoming) is expected


_SIMKL_EXACT = [{"season": 1, "episode": 1, "date": "2026-10-14T20:00:00-04:00"}]
_TMDB_DAY = {"episodes": [{"episode_number": 1, "air_date": "2026-10-15"}]}


async def _simkl_show(db_session):
    provider = await make_watchlist_provider(
        db_session, "simkl", client_id="cid", access_token="tok"
    )
    item, _, episodes = await make_series(
        db_session,
        simkl_id=777,
        tmdb_id=4242,
        provider_id=provider.id,
        seasons=[{"season_number": 1, "status": SeasonStatus.FUTURE}],
        episodes_per_season=1,
        episode_statuses=[EpisodeStatus.FUTURE],
    )
    await db_session.commit()
    return item, episodes[0]


async def _run(db_session, item, simkl_kwargs, tmdb_payload=_TMDB_DAY):
    with (
        patch(
            "app.core.air_dates.simkl.get_episodes",
            new_callable=AsyncMock,
            **simkl_kwargs,
        ),
        patch(
            "app.core.air_dates.tmdb.get_season_details",
            new_callable=AsyncMock,
            return_value=tmdb_payload,
        ) as tmdb_mock,
    ):
        await enrich_active_shows_episode_air_dates(db_session)
    await db_session.refresh(item)
    return tmdb_mock


@pytest.mark.asyncio
async def test_simkl_linked_show_gets_exact_dates(db_session) -> None:
    item, ep = await _simkl_show(db_session)
    tmdb_mock = await _run(db_session, item, {"return_value": _SIMKL_EXACT})
    await db_session.refresh(ep)
    assert ep.air_date.replace(tzinfo=timezone.utc) == datetime(
        2026, 10, 15, 0, 0, 0, 2, tzinfo=timezone.utc
    )
    tmdb_mock.assert_not_called()


@pytest.mark.asyncio
async def test_simkl_error_falls_back_to_tmdb(db_session) -> None:
    from app.services.watchlists.simkl import SimklError

    item, ep = await _simkl_show(db_session)
    await _run(db_session, item, {"side_effect": SimklError("boom")})
    await db_session.refresh(ep)
    assert ep.air_date.replace(tzinfo=timezone.utc) == datetime(
        2026, 10, 15, 0, 0, 0, 1, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_exact_time_survives_tmdb_only_refresh(db_session) -> None:
    item, ep = await _simkl_show(db_session)
    await _run(db_session, item, {"return_value": _SIMKL_EXACT})
    item.last_air_dates_refreshed_at = None
    await db_session.commit()
    await _run(db_session, item, {"side_effect": RuntimeError("down")})
    await db_session.refresh(ep)
    assert ep.air_date.microsecond != 1


_TS5, _TS6 = 1792008000, 1792612800


async def _mal_anime(db_session):
    provider = await make_watchlist_provider(db_session, "mal")
    item, _, episodes = await make_series(
        db_session,
        media_type=MediaType.ANIME,
        simkl_id=0,
        provider_id=provider.id,
        seasons=[
            {"season_number": 1, "status": SeasonStatus.FUTURE, "anilist_id": 154587}
        ],
        episodes_per_season=3,
        episode_statuses=[EpisodeStatus.FUTURE] * 3,
    )
    await db_session.commit()
    return item, episodes


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_empty", [False, True])
async def test_anime_air_dates_from_anilist_schedule(
    db_session, schedule_empty
) -> None:
    item, episodes = await _mal_anime(db_session)
    for e in episodes:
        await db_session.refresh(e)
    before = [e.air_date for e in episodes]
    schedule = (
        []
        if schedule_empty
        else [
            (2, datetime.fromtimestamp(_TS5, tz=timezone.utc)),
            (3, datetime.fromtimestamp(_TS6, tz=timezone.utc)),
        ]
    )
    with patch(
        "app.core.air_dates.anilist.get_anime_airing_schedule",
        new_callable=AsyncMock,
        return_value=schedule,
    ) as mock:
        await enrich_active_shows_episode_air_dates(db_session)
    mock.assert_awaited_once_with(154587)
    await db_session.refresh(item)
    for e in episodes:
        await db_session.refresh(e)
    assert item.last_air_dates_refreshed_at is not None
    if schedule_empty:
        assert [e.air_date for e in episodes] == before
    else:
        assert episodes[0].air_date == before[0]
        assert episodes[1].air_date.replace(tzinfo=timezone.utc) == schedule[0][1]
        assert episodes[2].air_date.replace(tzinfo=timezone.utc) == schedule[1][1]
        assert episodes[1].status == EpisodeStatus.FUTURE
