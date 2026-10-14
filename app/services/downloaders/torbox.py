"""TorBox adapter over ``app.services.torbox`` (m57, ADR-131).

Pure delegation: module-level cooldowns, caches and limiters stay in
``app.services.torbox``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from app.config import DEFAULT_USER_AGENT
from app.services import torbox
from app.services.downloaders.base import (
    BaseDownloader,
    DownloaderFile,
    DownloaderTransfer,
    DownloaderUploadResult,
    TransferState,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import Provider

logger = logging.getLogger(__name__)

_UNPACKING_STATES = ("processing", "repairing", "unpacking", "extracting", "verifying")
_COMPLETED_STATES = ("completed", "cached", "paused")


def _map_state(status_lower: str, item: dict[str, Any]) -> TransferState:
    from app.core.transfer_poller import _is_terminal_failure_state

    if _is_terminal_failure_state(status_lower):
        return TransferState.FAILED
    if status_lower in _COMPLETED_STATES or (
        item.get("download_finished") is True
        and status_lower not in ("downloading", "processing", "queued", "unknown")
    ):
        return TransferState.COMPLETED
    if status_lower in _UNPACKING_STATES:
        return TransferState.UNPACKING
    if status_lower in ("queued", "waiting"):
        return TransferState.QUEUED
    return TransferState.DOWNLOADING


class TorBoxDownloader(BaseDownloader):
    provider_type = "torbox"
    display_name = "TorBox"
    poll_interval_seconds = 20
    supports_file_listing = True

    def __init__(self, provider: Provider, session: AsyncSession | None = None):
        self.provider = provider
        self.session = session

    @property
    def _key(self) -> str | None:
        return (self.provider.api_key or "").strip() or None

    async def test_connection(self) -> tuple[bool, str]:
        if not self._key:
            return False, "API Key missing"
        base_url = (self.provider.api_url or torbox.TORBOX_BASE_URL + "/api").rstrip(
            "/"
        )
        endpoint = (
            f"{base_url}/user/me"
            if base_url.endswith("/api")
            else f"{base_url}/api/user/me"
        )
        try:
            async with httpx.AsyncClient(
                timeout=5.0, headers={"User-Agent": DEFAULT_USER_AGENT}
            ) as client:
                r = await client.get(
                    endpoint, headers={"Authorization": f"Bearer {self._key}"}
                )
        except httpx.HTTPError as e:
            return False, f"TorBox unreachable: {e}"
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        detail = "Account verified"
        try:
            data = r.json().get("data") or {}
            if data.get("email"):
                plan = data.get("plan")
                detail = (
                    f"{data['email']} (Plan {plan})"
                    if plan is not None
                    else data["email"]
                )
        except Exception:
            pass
        return True, detail

    async def upload_nzb(
        self,
        nzb_bytes: bytes,
        filename: str,
        password: str | None = None,
        category: str | None = None,
        is_manual: bool = False,
    ) -> DownloaderUploadResult:
        try:
            result = await torbox.send_nzb_file(
                nzb_bytes,
                filename=filename,
                session=self.session,
                explicit_key=self._key,
                is_manual=is_manual,
                password=password,
            )
        except torbox.DownloaderNetworkError as e:
            msg = str(e)
            if "cooldown" in msg.lower() or "rate limit" in msg.lower():
                remaining = torbox.get_cooldown_remaining(is_manual=is_manual)
                return DownloaderUploadResult(
                    success=False,
                    detail=msg,
                    is_rate_limited=True,
                    cooldown_seconds=int(remaining) if remaining > 0 else None,
                )
            raise
        if not result:
            return DownloaderUploadResult(
                success=False, detail="TorBox API key missing"
            )
        if result.get("error"):
            return DownloaderUploadResult(
                success=False, detail=str(result["error"]), raw=dict(result)
            )
        tb_id = result.get("id")
        if not tb_id and not result.get("hash"):
            return DownloaderUploadResult(
                success=False, detail="Failed to send", raw=dict(result)
            )
        return DownloaderUploadResult(
            success=True,
            transfer_id=str(tb_id) if tb_id else None,
            raw=dict(result),
        )

    async def get_transfers(self) -> list[DownloaderTransfer]:
        from app.core.transfer_poller import _normalize_progress_pct

        items = await torbox.get_usenet_downloads(
            session=self.session, explicit_key=self._key
        )
        transfers: list[DownloaderTransfer] = []
        for item in items:
            status = str(item.get("download_state") or "downloading").lower()
            state = _map_state(status, item)
            eta: int | None
            try:
                eta = max(0, int(item["eta"])) if item.get("eta") is not None else None
            except (TypeError, ValueError):
                eta = None
            size = item.get("size")
            transfers.append(
                DownloaderTransfer(
                    transfer_id=str(item.get("id")),
                    name=str(item.get("name") or item.get("title") or ""),
                    state=state,
                    progress_pct=_normalize_progress_pct(item.get("progress"), status),
                    size_bytes=int(size) if isinstance(size, int | float) else None,
                    speed_bytes=int(
                        item.get("download_speed") or item.get("speed") or 0
                    ),
                    eta_seconds=eta,
                    fail_message=status if state is TransferState.FAILED else None,
                    raw=item,
                )
            )
        return transfers

    async def get_transfer_files(self, transfer_id: str) -> list[DownloaderFile]:
        items = await torbox.get_usenet_downloads(
            session=self.session, explicit_key=self._key
        )
        for item in items:
            if str(item.get("id")) != str(transfer_id):
                continue
            raw_files = item.get("files")
            if not isinstance(raw_files, list):
                return []
            return [
                DownloaderFile(
                    name=str(f.get("name") or f.get("short_name") or ""),
                    size_bytes=int(f.get("size") or 0),
                    path=f.get("name"),
                )
                for f in raw_files
                if isinstance(f, dict)
            ]
        return []

    async def delete_transfer(
        self, transfer_id: str, delete_files: bool = True
    ) -> bool:
        try:
            tb_id = int(transfer_id)
        except (TypeError, ValueError):
            return False
        res = await torbox.delete_usenet_download(
            tb_id, api_key=self._key, session=self.session
        )
        return bool(res.get("success"))
