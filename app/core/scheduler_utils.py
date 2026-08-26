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
