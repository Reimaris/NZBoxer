"""Central timezone conversion and formatting utilities."""

from __future__ import annotations

from datetime import datetime, timezone


def to_local_datetime(dt: datetime | None) -> datetime | None:
    """Convert a datetime to the server's local system timezone.

    If the datetime is naive (such as timestamps stored in SQLite), it is
    assumed to be UTC and tagged with timezone.utc before conversion.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def format_local_datetime(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format a UTC or local datetime in the server's local system timezone.

    Returns "—" if dt is None.
    """
    if dt is None:
        return "—"
    local_dt = to_local_datetime(dt)
    if local_dt is None:
        return "—"
    return local_dt.strftime(fmt)
