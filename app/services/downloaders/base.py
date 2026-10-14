"""Provider-neutral downloader contract (m57, ADR-131)."""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

DOWNLOADER_DISPLAY_NAMES: dict[str, str] = {
    "torbox": "TorBox",
    "sabnzbd": "SABnzbd",
    "premiumize": "Premiumize",
}


class TransferState(str, enum.Enum):
    QUEUED = "queued"  # accepted, not started
    DOWNLOADING = "downloading"
    UNPACKING = "unpacking"  # post-processing / repair / extract
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class DownloaderUploadResult:
    success: bool
    transfer_id: str | None = None  # None when not dispatched
    detail: str = ""  # human-readable error/status
    is_rate_limited: bool = False
    cooldown_seconds: int | None = None  # set when is_rate_limited
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DownloaderTransfer:
    transfer_id: str
    name: str
    state: TransferState
    progress_pct: float  # 0.0-100.0
    size_bytes: int | None = None
    speed_bytes: int = 0
    eta_seconds: int | None = None
    fail_message: str | None = None
    folder_id: str | None = None  # Premiumize cloud folder / SAB storage path
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DownloaderFile:
    name: str
    size_bytes: int
    path: str | None = None


class DownloaderError(Exception):
    """Base class for downloader client failures."""


class DownloaderNetworkError(DownloaderError):
    """Transport failure; the poller must NOT mark transfers failed."""


class BaseDownloader(ABC):
    provider_type: str  # "torbox" | "sabnzbd" | "premiumize"
    display_name: str  # "TorBox" | "SABnzbd" | "Premiumize"
    poll_interval_seconds: int  # SAB 5, cloud 20
    supports_file_listing: bool

    @abstractmethod
    async def test_connection(self) -> tuple[bool, str]: ...

    @abstractmethod
    async def upload_nzb(
        self,
        nzb_bytes: bytes,
        filename: str,
        password: str | None = None,
        category: str | None = None,
    ) -> DownloaderUploadResult: ...

    @abstractmethod
    async def get_transfers(self) -> list[DownloaderTransfer]: ...

    @abstractmethod
    async def get_transfer_files(self, transfer_id: str) -> list[DownloaderFile]: ...

    @abstractmethod
    async def delete_transfer(
        self, transfer_id: str, delete_files: bool = True
    ) -> bool: ...
