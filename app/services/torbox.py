"""
TorBox API Client
=================
Integrates with the TorBox API to check cache (if applicable) and send
NZBs for downloading.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

import httpx

from app.config import DEFAULT_USER_AGENT, settings
from app.core.rate_limiter import (
    RateLimiter,
    RateLimitExceeded,
    RollingWindowRateLimiter,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
_auto_send_limiter = RollingWindowRateLimiter(
    50, 3600.0, max_wait_seconds=0.0
)  # 50/hour for background automation
_manual_send_limiter = RollingWindowRateLimiter(
    60, 3600.0, max_wait_seconds=0.0
)  # 60/hour hard ceiling for manual grabs
_send_limiter = _manual_send_limiter  # Backwards compatibility alias
_poll_limiter = RateLimiter(10.0)

_torbox_cooldown_until: float = 0.0

_cached_usenet_downloads: list[dict[str, Any]] = []
_cached_usenet_downloads_at: float = 0.0
_cache_lock = asyncio.Lock()


def get_cooldown_remaining(is_manual: bool = False) -> float:
    """Return remaining cooldown seconds, or 0.0 if not in cooldown.

    Checks the global circuit breaker (e.g. from HTTP 429).
    If is_manual is False, also checks whether the background automation rate
    limiter budget (50/hr) has slots available.
    """
    now = time.monotonic()
    circuit_breaker = max(0.0, _torbox_cooldown_until - now)
    if circuit_breaker > 0:
        return circuit_breaker

    limiter = _manual_send_limiter if is_manual else _auto_send_limiter
    return limiter.get_wait_time()


def set_cooldown(seconds: float) -> None:
    """Activate global TorBox cooldown for the specified duration."""
    global _torbox_cooldown_until
    _torbox_cooldown_until = time.monotonic() + max(seconds, 0.0)


def clear_usenet_cache() -> None:
    """Clear in-memory Usenet download cache."""
    global _cached_usenet_downloads, _cached_usenet_downloads_at
    _cached_usenet_downloads = []
    _cached_usenet_downloads_at = 0.0


TORBOX_BASE_URL = "https://api.torbox.app/v1"


class TorBoxError(Exception):
    pass


class DownloaderNetworkError(TorBoxError):
    pass


def _sanitize_error_body(text: str) -> str:
    if not text:
        return ""
    clean = re.sub(r"<.*?>", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:200]


async def resolve_api_key(
    session: AsyncSession | None = None, explicit_key: str | None = None
) -> str:
    """Resolve active TorBox API key from explicit argument, DB provider, or config settings."""
    if explicit_key:
        return explicit_key

    from app.services.provider_service import get_active_downloader

    if session is not None:
        provider = await get_active_downloader(session)
        if provider and provider.api_key:
            return provider.api_key
    else:
        from app.db.database import async_session_factory

        try:
            async with async_session_factory() as db:
                provider = await get_active_downloader(db)
                if provider and provider.api_key:
                    return provider.api_key
        except Exception:
            pass

    return settings.torbox_api_key or ""


async def send_nzb_link(
    nzb_url: str,
    api_key: str | None = None,
    session: AsyncSession | None = None,
    explicit_key: str | None = None,
    is_manual: bool = False,
) -> dict[str, str | int | None]:
    """Send an NZB URL to TorBox to initiate a Usenet download.

    Args:
        nzb_url: The URL to the NZB file (from Treasure Maps).
        api_key: Optional explicit API key.
        session: Optional DB session to resolve active provider.
        explicit_key: Optional explicit key override.
        is_manual: Whether the upload was triggered by interactive manual action.

    Returns:
        A dictionary with "hash" and "id" if successful, else empty dict.
    """
    key = await resolve_api_key(session=session, explicit_key=explicit_key or api_key)
    if not key:
        logger.warning("TorBox API key missing.")
        return {}

    remaining = get_cooldown_remaining(is_manual=is_manual)
    if remaining > 0:
        scope = "manual" if is_manual else "automation"
        raise DownloaderNetworkError(
            f"TorBox {scope} cooldown active ({int(remaining)}s remaining)"
        )

    limiter = _manual_send_limiter if is_manual else _auto_send_limiter

    url = f"{TORBOX_BASE_URL}/api/usenet/createusenetdownload"

    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": DEFAULT_USER_AGENT,
    }

    data = {"link": nzb_url}

    max_retries = 3
    for attempt in range(max_retries):
        try:
            await limiter.wait()
        except RateLimitExceeded as rle:
            scope = "manual" if is_manual else "automation"
            raise DownloaderNetworkError(
                f"TorBox {scope} rate limit exceeded (cooldown: {int(rle.wait_time)}s)"
            ) from rle
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.post(url, headers=headers, data=data)
                response.raise_for_status()

                result = response.json()
                if result.get("success"):
                    resp_data = result.get("data", {})
                    tb_id = (
                        resp_data.get("usenetdownload_id")
                        or resp_data.get("usenet_id")
                        or resp_data.get("id")
                        or resp_data.get("download_id")
                        or resp_data.get("usenet_download_id")
                    )
                    if not tb_id:
                        logger.warning(
                            "TorBox link upload success but no known ID field found in data: %s",
                            resp_data,
                        )
                    return {
                        "hash": resp_data.get("hash"),
                        "id": tb_id,
                    }
                else:
                    err_msg = result.get("detail") or "TorBox API Error"
                    logger.error("TorBox API returned error: %s", err_msg)
                    return {"error": err_msg}

            except httpx.HTTPError as e:
                is_retriable = True
                err_detail = str(e)
                if isinstance(e, httpx.HTTPStatusError):
                    status = e.response.status_code
                    if status == 429:
                        retry_after_hdr = e.response.headers.get("Retry-After")
                        cooldown_secs = 300.0
                        if retry_after_hdr:
                            try:
                                cooldown_secs = float(retry_after_hdr)
                            except (ValueError, TypeError):
                                cooldown_secs = 300.0
                        set_cooldown(cooldown_secs)
                        logger.warning(
                            "⚠️ TorBox rate limit hit (429). Activating cooldown for %ss.",
                            int(cooldown_secs),
                        )
                        raise DownloaderNetworkError(
                            f"TorBox rate limit exceeded (cooldown: {int(cooldown_secs)}s)"
                        ) from e

                    if not (
                        500 <= status < 600
                        or status in (429, 520, 521, 522, 523, 524, 525, 526, 530)
                    ):
                        is_retriable = False
                    try:
                        err_json = e.response.json()
                        err_detail = (
                            err_json.get("detail")
                            or err_json.get("error")
                            or e.response.text
                        )
                    except Exception:
                        err_detail = e.response.text

                err_detail = _sanitize_error_body(str(err_detail))

                if not is_retriable:
                    logger.error("TorBox API error (non-retriable): %s", err_detail)
                    return {"error": str(err_detail)}

                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 3
                    logger.warning(
                        "Failed to send NZB to TorBox: [%s] %s. Retrying in %ss...",
                        type(e).__name__,
                        e,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                else:
                    err_msg = _sanitize_error_body(str(e))
                    logger.error(
                        "Failed to send NZB to TorBox after %d attempts: [%s] %s",
                        max_retries,
                        type(e).__name__,
                        err_msg,
                    )
                    raise DownloaderNetworkError(
                        f"TorBox network error: {err_msg}"
                    ) from e

    return {}


async def send_nzb_file(
    nzb_bytes: bytes,
    filename: str = "file.nzb",
    api_key: str | None = None,
    session: AsyncSession | None = None,
    explicit_key: str | None = None,
    is_manual: bool = False,
) -> dict[str, str | int | None]:
    """Send an NZB file directly to TorBox.

    Args:
        nzb_bytes: The raw NZB file bytes.
        filename: Optional filename for the upload.
        api_key: Optional explicit API key.
        session: Optional DB session to resolve active provider.
        explicit_key: Optional explicit key override.
        is_manual: Whether the upload was triggered by interactive manual action.

    Returns:
        A dictionary with "hash" and "id" if successful, else error dict.
    """
    key = await resolve_api_key(session=session, explicit_key=explicit_key or api_key)
    if not key:
        logger.warning("TorBox API key missing.")
        return {}

    remaining = get_cooldown_remaining(is_manual=is_manual)
    if remaining > 0:
        scope = "manual" if is_manual else "automation"
        raise DownloaderNetworkError(
            f"TorBox {scope} cooldown active ({int(remaining)}s remaining)"
        )

    limiter = _manual_send_limiter if is_manual else _auto_send_limiter

    url = f"{TORBOX_BASE_URL}/api/usenet/createusenetdownload"
    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    files = {"file": (filename, nzb_bytes, "application/x-nzb")}

    max_retries = 3
    for attempt in range(max_retries):
        try:
            await limiter.wait()
        except RateLimitExceeded as rle:
            scope = "manual" if is_manual else "automation"
            raise DownloaderNetworkError(
                f"TorBox {scope} rate limit exceeded (cooldown: {int(rle.wait_time)}s)"
            ) from rle
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.post(url, headers=headers, files=files)
                response.raise_for_status()

                result = response.json()
                if result.get("success"):
                    resp_data = result.get("data", {})
                    tb_id = (
                        resp_data.get("usenetdownload_id")
                        or resp_data.get("usenet_id")
                        or resp_data.get("id")
                        or resp_data.get("download_id")
                        or resp_data.get("usenet_download_id")
                    )
                    if not tb_id:
                        logger.warning(
                            "TorBox success but no known ID field found in data: %s",
                            resp_data,
                        )
                    return {
                        "hash": resp_data.get("hash"),
                        "id": tb_id,
                    }
                else:
                    err_msg = result.get("detail") or "TorBox API Error"
                    logger.error("TorBox API returned error: %s", err_msg)
                    return {"error": err_msg}

            except httpx.HTTPError as e:
                is_retriable = True
                err_detail = str(e)
                if isinstance(e, httpx.HTTPStatusError):
                    status = e.response.status_code
                    if status == 429:
                        retry_after_hdr = e.response.headers.get("Retry-After")
                        cooldown_secs = 300.0
                        if retry_after_hdr:
                            try:
                                cooldown_secs = float(retry_after_hdr)
                            except (ValueError, TypeError):
                                cooldown_secs = 300.0
                        set_cooldown(cooldown_secs)
                        logger.warning(
                            "⚠️ TorBox rate limit hit (429). Activating cooldown for %ss.",
                            int(cooldown_secs),
                        )
                        raise DownloaderNetworkError(
                            f"TorBox rate limit exceeded (cooldown: {int(cooldown_secs)}s)"
                        ) from e

                    if not (
                        500 <= status < 600
                        or status in (429, 520, 521, 522, 523, 524, 525, 526, 530)
                    ):
                        is_retriable = False
                    try:
                        err_json = e.response.json()
                        err_detail = (
                            err_json.get("detail")
                            or err_json.get("error")
                            or e.response.text
                        )
                    except Exception:
                        err_detail = e.response.text

                err_detail = _sanitize_error_body(str(err_detail))

                if not is_retriable:
                    logger.error("TorBox API error (non-retriable): %s", err_detail)
                    return {"error": str(err_detail)}

                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 3
                    logger.warning(
                        "Failed to send NZB file to TorBox: [%s] %s. Retrying in %ss...",
                        type(e).__name__,
                        e,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                else:
                    err_msg = _sanitize_error_body(str(e))
                    logger.error(
                        "Failed to send NZB file to TorBox after %d attempts: [%s] %s",
                        max_retries,
                        type(e).__name__,
                        err_msg,
                    )
                    raise DownloaderNetworkError(
                        f"TorBox network error: {err_msg}"
                    ) from e

    return {}


async def send_magnet_link(
    magnet_url: str,
    api_key: str | None = None,
    session: AsyncSession | None = None,
    explicit_key: str | None = None,
    is_manual: bool = False,
) -> dict[str, str | int | None]:
    """Send a Magnet/Torrent URL to TorBox.

    Args:
        magnet_url: The magnet URI or torrent URL.
        api_key: Optional explicit API key.
        session: Optional DB session to resolve active provider.
        explicit_key: Optional explicit key override.
        is_manual: Whether the upload was triggered by interactive manual action.

    Returns:
        A dictionary with "hash" and "id" if successful, else empty dict.
    """
    key = await resolve_api_key(session=session, explicit_key=explicit_key or api_key)
    if not key:
        logger.warning("TorBox API key missing.")
        return {}

    remaining = get_cooldown_remaining(is_manual=is_manual)
    if remaining > 0:
        scope = "manual" if is_manual else "automation"
        raise DownloaderNetworkError(
            f"TorBox {scope} cooldown active ({int(remaining)}s remaining)"
        )

    limiter = _manual_send_limiter if is_manual else _auto_send_limiter

    url = f"{TORBOX_BASE_URL}/api/torrents/createtorrent"

    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": DEFAULT_USER_AGENT,
    }

    data = {"magnet": magnet_url}

    max_retries = 3
    for attempt in range(max_retries):
        try:
            await limiter.wait()
        except RateLimitExceeded as rle:
            scope = "manual" if is_manual else "automation"
            raise DownloaderNetworkError(
                f"TorBox {scope} rate limit exceeded (cooldown: {int(rle.wait_time)}s)"
            ) from rle
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.post(url, headers=headers, data=data)
                response.raise_for_status()

                result = response.json()
                if result.get("success"):
                    resp_data = result.get("data", {})
                    tb_id = (
                        resp_data.get("torrentdownload_id")
                        or resp_data.get("torrent_id")
                        or resp_data.get("id")
                        or resp_data.get("download_id")
                        or resp_data.get("torrent_download_id")
                    )
                    if not tb_id:
                        logger.warning(
                            "TorBox magnet upload success but no known ID field found in data: %s",
                            resp_data,
                        )
                    return {
                        "hash": resp_data.get("hash"),
                        "id": tb_id,
                    }
                else:
                    err_msg = result.get("detail") or "TorBox API Error"
                    logger.error("TorBox API returned error: %s", err_msg)
                    return {"error": err_msg}

            except httpx.HTTPError as e:
                is_retriable = True
                err_detail = str(e)
                if isinstance(e, httpx.HTTPStatusError):
                    status = e.response.status_code
                    if status == 429:
                        retry_after_hdr = e.response.headers.get("Retry-After")
                        cooldown_secs = 300.0
                        if retry_after_hdr:
                            try:
                                cooldown_secs = float(retry_after_hdr)
                            except (ValueError, TypeError):
                                cooldown_secs = 300.0
                        set_cooldown(cooldown_secs)
                        logger.warning(
                            "⚠️ TorBox rate limit hit (429). Activating cooldown for %ss.",
                            int(cooldown_secs),
                        )
                        raise DownloaderNetworkError(
                            f"TorBox rate limit exceeded (cooldown: {int(cooldown_secs)}s)"
                        ) from e

                    if not (
                        500 <= status < 600
                        or status in (429, 520, 521, 522, 523, 524, 525, 526, 530)
                    ):
                        is_retriable = False
                    try:
                        err_json = e.response.json()
                        err_detail = (
                            err_json.get("detail")
                            or err_json.get("error")
                            or e.response.text
                        )
                    except Exception:
                        err_detail = e.response.text

                err_detail = _sanitize_error_body(str(err_detail))

                if not is_retriable:
                    logger.error("TorBox API error (non-retriable): %s", err_detail)
                    return {"error": str(err_detail)}

                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 3
                    logger.warning(
                        "Failed to send Magnet to TorBox: [%s] %s. Retrying in %ss...",
                        type(e).__name__,
                        e,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                else:
                    err_msg = _sanitize_error_body(str(e))
                    logger.error(
                        "Failed to send Magnet to TorBox after %d attempts: [%s] %s",
                        max_retries,
                        type(e).__name__,
                        err_msg,
                    )
                    raise DownloaderNetworkError(
                        f"TorBox network error: {err_msg}"
                    ) from e

    return {}


async def check_download_status(
    download_id: str | int,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """Check the status of a specific TorBox download.

    Args:
        download_id: The TorBox ID for the download.
        api_key: Optional explicit API key.
        session: Optional DB session to resolve active provider.

    Returns:
        A dictionary containing "status" and "detail". Status can be:
        "completed", "downloading", "failed", "error", etc.
    """
    key = await resolve_api_key(session=session, explicit_key=api_key)
    if not key:
        return {"status": "error", "detail": "Missing API key"}

    url = f"{TORBOX_BASE_URL}/api/usenet/mylist"
    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": DEFAULT_USER_AGENT,
    }

    await _poll_limiter.wait()
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()

            result = response.json()
            if result.get("success"):
                downloads = result.get("data", [])
                for d in downloads:
                    if str(d.get("id")) == str(download_id):
                        return {
                            "status": d.get("download_state", "unknown"),
                            "progress": d.get("progress", 0),
                            "detail": d.get("name", ""),
                        }
                return {
                    "status": "not_found",
                    "detail": "Download ID not found in TorBox",
                }
            else:
                return {"status": "error", "detail": result.get("detail")}

        except httpx.HTTPError as e:
            return {"status": "error", "detail": str(e)}


async def delete_usenet_download(
    download_id: int,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """Deletes a usenet download from TorBox."""
    key = await resolve_api_key(session=session, explicit_key=api_key)
    if not key:
        return {"success": False, "detail": "No API key configured"}

    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": DEFAULT_USER_AGENT,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(
                "https://api.torbox.app/v1/api/usenet/controlusenetdownload",
                headers=headers,
                json={"usenet_id": download_id, "operation": "delete"},
            )
            response.raise_for_status()
            res: dict[str, Any] = response.json()
            return res
        except httpx.HTTPError as e:
            logger.error(f"Failed to delete TorBox usenet download {download_id}: {e}")
            return {"success": False, "detail": str(e)}


async def delete_torrent_download(
    download_id: int,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """Deletes a torrent download from TorBox."""
    key = await resolve_api_key(session=session, explicit_key=api_key)
    if not key:
        return {"success": False, "detail": "No API key configured"}

    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": DEFAULT_USER_AGENT,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(
                "https://api.torbox.app/v1/api/torrents/controltorrent",
                headers=headers,
                json={"torrent_id": download_id, "operation": "delete"},
            )
            response.raise_for_status()
            res: dict[str, Any] = response.json()
            return res
        except httpx.HTTPError as e:
            logger.error(f"Failed to delete TorBox torrent download {download_id}: {e}")
            return {"success": False, "detail": str(e)}


async def download_file_payload(
    download_id: int,
    file_id: int,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> bytes | None:
    """Directly requests the payload file stream from TorBox."""
    key = await resolve_api_key(session=session, explicit_key=api_key)
    if not key:
        return None

    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": DEFAULT_USER_AGENT,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(
                f"https://api.torbox.app/v1/api/usenet/requestdownload?token={key}&usenet_id={download_id}&file_id={file_id}",
                headers=headers,
            )
            response.raise_for_status()
            res_json = response.json()
            if not res_json.get("success"):
                return None

            dl_url = res_json.get("data")
            if not dl_url:
                return None

            file_response = await client.get(dl_url)
            file_response.raise_for_status()
            return file_response.content
        except httpx.HTTPError as e:
            logger.error(
                f"Failed to fetch file payload {file_id} from {download_id}: {e}"
            )
            return None


async def get_usenet_downloads(
    bypass_cache: bool = False,
    api_key: str | None = None,
    session: AsyncSession | None = None,
    explicit_key: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve list of active Usenet downloads from TorBox."""
    global _cached_usenet_downloads, _cached_usenet_downloads_at

    key = await resolve_api_key(session=session, explicit_key=explicit_key or api_key)
    if not key:
        return []

    now = time.monotonic()
    if (
        not bypass_cache
        and (now - _cached_usenet_downloads_at < 20.0)
        and _cached_usenet_downloads
    ):
        return _cached_usenet_downloads

    url = f"{TORBOX_BASE_URL}/api/usenet/mylist"
    if bypass_cache:
        url += "?bypass_cache=true"

    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": DEFAULT_USER_AGENT,
    }

    await _poll_limiter.wait()
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            result = response.json()
            if result.get("success"):
                data: list[dict[str, Any]] = result.get("data", [])
                async with _cache_lock:
                    _cached_usenet_downloads = data
                    _cached_usenet_downloads_at = time.monotonic()
                return data
            return []
        except httpx.HTTPError as e:
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429:
                retry_after_hdr = e.response.headers.get("Retry-After")
                cooldown_secs = 300.0
                if retry_after_hdr:
                    try:
                        cooldown_secs = float(retry_after_hdr)
                    except (ValueError, TypeError):
                        cooldown_secs = 300.0
                set_cooldown(cooldown_secs)
                logger.warning(
                    "⚠️ TorBox rate limit hit (429) during get_usenet_downloads. Activating cooldown for %ss.",
                    int(cooldown_secs),
                )
            logger.error(f"Failed to get TorBox usenet downloads: {e}")
            return []


async def get_torrent_downloads(
    api_key: str | None = None,
    session: AsyncSession | None = None,
    explicit_key: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve list of active Torrent downloads from TorBox."""
    key = await resolve_api_key(session=session, explicit_key=explicit_key or api_key)
    if not key:
        return []

    url = f"{TORBOX_BASE_URL}/api/torrents/mylist"
    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": DEFAULT_USER_AGENT,
    }

    await _poll_limiter.wait()
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            result = response.json()
            if result.get("success"):
                return result.get("data", []) or []
            return []
        except httpx.HTTPError as e:
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429:
                retry_after_hdr = e.response.headers.get("Retry-After")
                cooldown_secs = 300.0
                if retry_after_hdr:
                    try:
                        cooldown_secs = float(retry_after_hdr)
                    except (ValueError, TypeError):
                        cooldown_secs = 300.0
                set_cooldown(cooldown_secs)
                logger.warning(
                    "⚠️ TorBox rate limit hit (429) during get_torrent_downloads. Activating cooldown for %ss.",
                    int(cooldown_secs),
                )
            logger.error(f"Failed to get TorBox torrent downloads: {e}")
            return []
