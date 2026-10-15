"""
Central Logging Helper
======================
Provides single-line START/END banner records for application background tasks and engines.
"""

from __future__ import annotations

import logging
from datetime import datetime


def log_process_start(
    logger_obj: logging.Logger, process_name: str, at: str | None = None
) -> None:
    """Log a prefixed single-line process start banner (``at`` defaults to local now)."""
    at = at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger_obj.info("===== [START] %s %s =====", process_name, at)


def log_process_end(
    logger_obj: logging.Logger,
    process_name: str,
    at: str | None = None,
    took_s: float | None = None,
) -> None:
    """Log a prefixed single-line process end banner, optionally with the duration."""
    at = at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    took = f" (took {took_s:.0f}s)" if took_s is not None else ""
    logger_obj.info("===== [END] %s %s%s =====", process_name, at, took)
