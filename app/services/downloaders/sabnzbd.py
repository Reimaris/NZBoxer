"""SABnzbd client (m57, ADR-131)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from app.config import DEFAULT_USER_AGENT
from app.services.downloaders.base import (
    BaseDownloader,
    DownloaderFile,
    DownloaderNetworkError,
    DownloaderTransfer,
    DownloaderUploadResult,
    TransferState,
)

if TYPE_CHECKING:
    from app.db.models import Provider

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_UPLOAD_TIMEOUT = httpx.Timeout(120.0, connect=5.0)

_QUEUE_STATES = {
    "queued": TransferState.QUEUED,
    "paused": TransferState.QUEUED,
    "propagating": TransferState.QUEUED,
    "fetching": TransferState.QUEUED,
    "downloading": TransferState.DOWNLOADING,
    "grabbing": TransferState.DOWNLOADING,
    "checking": TransferState.UNPACKING,
    "repairing": TransferState.UNPACKING,
    "verifying": TransferState.UNPACKING,
    "extracting": TransferState.UNPACKING,
}
_HISTORY_POSTPROCESSING = {
    "queued",
    "verifying",
    "repairing",
    "extracting",
    "moving",
    "running",
}


def _client_factory(timeout: httpx.Timeout) -> httpx.AsyncClient:
    """Patch point for tests (inject ``httpx.MockTransport``)."""
    return httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": DEFAULT_USER_AGENT}
    )


def normalize_api_url(api_url: str | None) -> str:
    """Strip whitespace/trailing slashes and ensure the URL ends in ``/api``."""
    url = (api_url or "").strip().rstrip("/")
    if url and not url.endswith("/api"):
        url += "/api"
    return url


def _parse_timeleft(value: Any) -> int | None:
    try:
        parts = [int(p) for p in str(value).split(":")]
    except ValueError:
        return None
    if len(parts) != 3:
        return None
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class SabnzbdDownloader(BaseDownloader):
    provider_type = "sabnzbd"
    display_name = "SABnzbd"
    poll_interval_seconds = 5
    supports_file_listing = True

    def __init__(self, provider: Provider):
        self.provider = provider
        self.api_url = normalize_api_url(provider.api_url)
        self._api_key = provider.api_key or ""

    async def _get(self, **params: Any) -> dict[str, Any]:
        query = {"output": "json", "apikey": self._api_key, **params}
        try:
            async with _client_factory(_TIMEOUT) as client:
                resp = await client.get(self.api_url, params=query)
        except httpx.HTTPError as exc:
            raise DownloaderNetworkError(
                f"SABnzbd unreachable: {type(exc).__name__}"
            ) from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    async def test_connection(self) -> tuple[bool, str]:
        try:
            version = (await self._get(mode="version")).get("version")
            check = await self._get(mode="queue", limit=1)
        except DownloaderNetworkError as exc:
            return False, str(exc)
        if check.get("status") is False or "queue" not in check:
            return False, "SABnzbd API key rejected"
        return True, f"SABnzbd {version}"

    async def upload_nzb(
        self,
        nzb_bytes: bytes,
        filename: str,
        password: str | None = None,
        category: str | None = None,
    ) -> DownloaderUploadResult:
        data = {
            "apikey": self._api_key,
            "nzbname": filename.removesuffix(".nzb"),
        }
        if category:
            data["cat"] = category
        if password:
            data["nzbpassword"] = password
        try:
            async with _client_factory(_UPLOAD_TIMEOUT) as client:
                resp = await client.post(
                    self.api_url,
                    params={"mode": "addfile", "output": "json"},
                    data=data,
                    files={"name": (filename, nzb_bytes, "application/x-nzb")},
                )
            body = resp.json()
        except httpx.HTTPError as exc:
            return DownloaderUploadResult(
                success=False, detail=f"SABnzbd unreachable: {type(exc).__name__}"
            )
        except ValueError:
            return DownloaderUploadResult(
                success=False, detail="SABnzbd returned an invalid response"
            )
        if not isinstance(body, dict) or not body.get("status"):
            detail = (
                str(body.get("error") or "SABnzbd rejected the NZB")
                if isinstance(body, dict)
                else "SABnzbd rejected the NZB"
            )
            return DownloaderUploadResult(
                success=False, detail=detail, raw=body if isinstance(body, dict) else {}
            )
        nzo_ids = body.get("nzo_ids") or []
        if not nzo_ids:
            return DownloaderUploadResult(
                success=False,
                detail="SABnzbd accepted request but returned no nzo_id",
                raw=body,
            )
        return DownloaderUploadResult(
            success=True, transfer_id=str(nzo_ids[0]), raw=body
        )

    async def get_transfers(self) -> list[DownloaderTransfer]:
        queue = (await self._get(mode="queue")).get("queue") or {}
        history = (await self._get(mode="history", limit=200)).get("history") or {}
        speed = int(_to_float(queue.get("kbpersec")) * 1024)

        transfers: dict[str, DownloaderTransfer] = {}
        for slot in queue.get("slots") or []:
            nzo_id = slot.get("nzo_id")
            if not nzo_id:
                continue
            status = str(slot.get("status", "")).lower()
            state = _QUEUE_STATES.get(status, TransferState.QUEUED)
            transfers[nzo_id] = DownloaderTransfer(
                transfer_id=nzo_id,
                name=slot.get("filename", ""),
                state=state,
                progress_pct=_to_float(slot.get("percentage")),
                size_bytes=int(_to_float(slot.get("mb")) * 1024 * 1024),
                speed_bytes=speed if state == TransferState.DOWNLOADING else 0,
                eta_seconds=_parse_timeleft(slot.get("timeleft")),
                raw=slot,
            )
        for slot in history.get("slots") or []:
            nzo_id = slot.get("nzo_id")
            if not nzo_id or nzo_id in transfers:
                continue
            status = str(slot.get("status", ""))
            if status == "Completed":
                state, pct, fail = TransferState.COMPLETED, 100.0, None
            elif status == "Failed":
                state, pct = TransferState.FAILED, 0.0
                fail = slot.get("fail_message") or "Failed"
            elif status.lower() in _HISTORY_POSTPROCESSING:
                state, pct, fail = TransferState.UNPACKING, 100.0, None
            else:
                continue
            transfers[nzo_id] = DownloaderTransfer(
                transfer_id=nzo_id,
                name=slot.get("name", ""),
                state=state,
                progress_pct=pct,
                size_bytes=int(_to_float(slot.get("bytes"))),
                fail_message=fail,
                folder_id=slot.get("storage") or None,
                raw=slot,
            )
        return list(transfers.values())

    async def get_transfer_files(self, transfer_id: str) -> list[DownloaderFile]:
        data = await self._get(mode="get_files", value=transfer_id)
        return [
            DownloaderFile(
                name=f.get("filename", ""), size_bytes=int(_to_float(f.get("bytes")))
            )
            for f in data.get("files") or []
        ]

    async def delete_transfer(
        self, transfer_id: str, delete_files: bool = True
    ) -> bool:
        extra: dict[str, Any] = {"del_files": 1} if delete_files else {}
        for mode in ("queue", "history"):
            data = await self._get(mode=mode, name="delete", value=transfer_id, **extra)
            if data.get("status") is True:
                return True
        return False
