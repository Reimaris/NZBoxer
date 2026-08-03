"""Unit tests for rate limiters and daily grab counter."""
import time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limiter import RateLimiter
from app.db.grab_tracker import (
    can_grab_today,
    get_today_grab_count,
    increment_today_grab_count,
)


@pytest.mark.asyncio
async def test_rate_limiter_wait():
    limiter = RateLimiter(0.1)
    start = time.monotonic()
    await limiter.wait()
    await limiter.wait()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.09


@pytest.mark.asyncio
async def test_daily_grab_tracker(db_session: AsyncSession):
    # Initial count should be 0
    initial = await get_today_grab_count(db_session)
    assert initial == 0
    assert await can_grab_today(db_session, limit=400) is True

    # Increment count
    new_count = await increment_today_grab_count(db_session)
    assert new_count == 1
    assert await get_today_grab_count(db_session) == 1

    # Simulate reaching limit of 2 for testing
    await increment_today_grab_count(db_session)
    assert await can_grab_today(db_session, limit=2) is False
