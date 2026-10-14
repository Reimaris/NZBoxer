"""Provider-neutral downloader package (m57, ADR-131)."""

from app.services.downloaders.base import (
    DOWNLOADER_DISPLAY_NAMES,
    BaseDownloader,
    DownloaderError,
    DownloaderFile,
    DownloaderNetworkError,
    DownloaderTransfer,
    DownloaderUploadResult,
    TransferState,
)
from app.services.downloaders.factory import (
    category_for_media,
    get_active_downloader_client,
    get_downloader_client,
    get_downloader_client_for_type,
)

__all__ = [
    "DOWNLOADER_DISPLAY_NAMES",
    "BaseDownloader",
    "DownloaderError",
    "DownloaderFile",
    "DownloaderNetworkError",
    "DownloaderTransfer",
    "DownloaderUploadResult",
    "TransferState",
    "category_for_media",
    "get_active_downloader_client",
    "get_downloader_client",
    "get_downloader_client_for_type",
]
