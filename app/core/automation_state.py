"""
Centralized Automation State Manager
====================================
Tracks whether an automation cycle is IDLE, RUNNING_VIDEO, RUNNING_PRINT,
or transitioning through ABORTING. Ensures mutual exclusion between cycles
and provides a safe coordination seam for graceful termination.
"""

from __future__ import annotations

import enum
import threading
from typing import Final


class AutomationStatus(str, enum.Enum):
    IDLE = "idle"
    RUNNING_VIDEO = "running_video"
    RUNNING_PRINT = "running_print"
    ABORTING = "aborting"


class AutomationState:
    """Thread-safe automation execution state tracker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: AutomationStatus = AutomationStatus.IDLE

    def get_state(self) -> str:
        with self._lock:
            return self._state.value

    def is_running(self) -> bool:
        with self._lock:
            return self._state in (
                AutomationStatus.RUNNING_VIDEO,
                AutomationStatus.RUNNING_PRINT,
                AutomationStatus.ABORTING,
            )

    def is_aborting(self) -> bool:
        with self._lock:
            return self._state == AutomationStatus.ABORTING

    def set_running(self, target_state: str | AutomationStatus) -> bool:
        """Attempt to transition from IDLE to a running state.

        Returns True if transition succeeded, False if already running or invalid.
        """
        if isinstance(target_state, str):
            try:
                target_state = AutomationStatus(target_state)
            except ValueError:
                return False

        if target_state not in (
            AutomationStatus.RUNNING_VIDEO,
            AutomationStatus.RUNNING_PRINT,
        ):
            return False

        with self._lock:
            if self._state == target_state:
                return True
            if self._state != AutomationStatus.IDLE:
                return False
            self._state = target_state
            return True

    def request_abort(self) -> bool:
        """Signal that the active run should abort gracefully.

        Returns True if a run was active and state changed to ABORTING, False otherwise.
        """
        with self._lock:
            if self._state in (
                AutomationStatus.RUNNING_VIDEO,
                AutomationStatus.RUNNING_PRINT,
            ):
                self._state = AutomationStatus.ABORTING
                return True
            return False

    def reset(self) -> None:
        """Reset state back to IDLE unconditionally."""
        with self._lock:
            self._state = AutomationStatus.IDLE


# Global singleton instance
automation_state_manager: Final[AutomationState] = AutomationState()
