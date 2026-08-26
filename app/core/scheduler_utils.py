"""
Scheduler Utilities
===================
Wall-clock scheduling helpers and interval preset validators.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

VALID_INTERVAL_PRESETS: Final[set[int]] = {
    15,
    30,
    60,
    120,
    240,
    360,
    720,
    1440,
}


def is_interval_due(interval_minutes: int, current_dt: datetime) -> bool:
    """Check if a given interval preset is due at the current wall-clock time.

    Rules:
    - 15m: Every quarter-hour tick (:00, :15, :30, :45) -> True
    - 30m: :00, :30
    - >= 60m: Must be on the full hour (:00), and current hour % (interval_minutes // 60) == 0.
    """
    minute = current_dt.minute
    hour = current_dt.hour

    if interval_minutes <= 15:
        return True

    if interval_minutes == 30:
        return minute in (0, 30)

    # All intervals >= 60 minutes strictly require minute == 0 (top of the hour)
    if minute != 0:
        return False

    if interval_minutes == 60:
        return True

    step_hours = interval_minutes // 60
    return (hour % step_hours) == 0


def get_next_scheduled_time(
    interval_minutes: int, from_dt: datetime | None = None
) -> str:
    """Calculate the next wall-clock execution time formatted as 'HH:MM'.

    Always aligns to the base quarter-hour heartbeat (:00, :15, :30, :45).
    """
    from datetime import timedelta

    if from_dt is None:
        from_dt = datetime.now()

    # Move to next whole minute to ensure strictly upcoming tick
    candidate = from_dt.replace(second=0, microsecond=0) + timedelta(minutes=1)

    # Fast-forward to the next quarter-hour boundary
    while candidate.minute not in (0, 15, 30, 45):
        candidate += timedelta(minutes=1)

    # Search for the next candidate satisfying the interval
    for _ in range(96 * 2):  # Search up to 48 hours
        if is_interval_due(interval_minutes, candidate):
            return candidate.strftime("%H:%M")
        candidate += timedelta(minutes=15)

    return candidate.strftime("%H:%M")
