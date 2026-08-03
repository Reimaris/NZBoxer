"""
Central Logging Helper
======================
Provides block separator formatting for application background tasks and engines.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone


def log_process_start(logger_obj: logging.Logger, process_name: str) -> None:
    """Log a prominent process start block header."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sep = "=" * 80
    logger_obj.info("\n%s\n[PROCESS START] %s - %s\n%s", sep, process_name, now_str, sep)


def log_process_end(logger_obj: logging.Logger, process_name: str) -> None:
    """Log a prominent process end block footer."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sep = "=" * 80
    logger_obj.info("\n%s\n[PROCESS END] %s - %s\n%s", sep, process_name, now_str, sep)
