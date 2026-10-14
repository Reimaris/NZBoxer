from datetime import datetime, timezone

import pytest

from app.core.air_push_engine import is_episode_air_due
from app.core.automation import is_date_only_air_date
from app.db.models import Episode, EpisodeStatus

UTC = timezone.utc
TMDB_DATE = datetime(2026, 10, 14, 0, 0, 0, 1, tzinfo=UTC)
SIMKL_EXACT = datetime(2026, 10, 14, 23, 30, tzinfo=UTC)  # 2026-10-14T19:30-04:00


def _ep(air_date: datetime) -> Episode:
    return Episode(air_date=air_date, status=EpisodeStatus.SEARCHING)


@pytest.mark.parametrize(
    "dt,expected",
    [
        (TMDB_DATE, True),
        (datetime(2026, 10, 14, tzinfo=UTC), True),
        (datetime(2026, 10, 14, 20, 0, tzinfo=UTC), False),
    ],
)
def test_is_date_only_air_date(dt, expected):
    assert is_date_only_air_date(dt) is expected


@pytest.mark.parametrize(
    "air_date,now,due",
    [
        (TMDB_DATE, datetime(2026, 10, 15, 1, 59, tzinfo=UTC), False),
        (TMDB_DATE, datetime(2026, 10, 15, 2, 0, tzinfo=UTC), True),
        (SIMKL_EXACT, datetime(2026, 10, 15, 1, 29, tzinfo=UTC), False),
        (SIMKL_EXACT, datetime(2026, 10, 15, 1, 30, tzinfo=UTC), True),
    ],
)
def test_air_due_date_only_vs_exact(air_date, now, due):
    assert is_episode_air_due(_ep(air_date), 2, now) is due


def test_max_age_measured_from_due_for_date_only():
    ep = _ep(TMDB_DATE)
    assert is_episode_air_due(
        ep, 2, datetime(2026, 10, 15, 3, 0, tzinfo=UTC), max_age_hours=1
    )
    assert not is_episode_air_due(
        ep, 2, datetime(2026, 10, 15, 3, 1, tzinfo=UTC), max_age_hours=1
    )
