"""
Daily Grab Tracker
==================
Helper functions to track and enforce the hard daily limit of 400 NZB grabs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DailyGrabCounter

logger = logging.getLogger(__name__)

MAX_DAILY_GRABS = 400


def get_today_str() -> str:
    """Return today's date string in YYYY-MM-DD format (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def get_today_grab_count(session: AsyncSession) -> int:
    """Fetch today's total grab count from the database."""
    today = get_today_str()
    stmt = select(DailyGrabCounter).where(DailyGrabCounter.date_str == today)
    result = await session.execute(stmt)
    counter = result.scalar_one_or_none()
    return counter.count if counter else 0


async def can_grab_today(session: AsyncSession, limit: int = MAX_DAILY_GRABS) -> bool:
    """Return True if today's grab count is below the daily limit."""
    current = await get_today_grab_count(session)
    return current < limit


async def increment_today_grab_count(session: AsyncSession) -> int:
    """Increment today's grab counter and return the new total."""
    today = get_today_str()
    stmt = select(DailyGrabCounter).where(DailyGrabCounter.date_str == today)
    result = await session.execute(stmt)
    counter = result.scalar_one_or_none()

    if counter is None:
        counter = DailyGrabCounter(date_str=today, count=1)
        session.add(counter)
    else:
        counter.count += 1

    await session.commit()
    return counter.count
