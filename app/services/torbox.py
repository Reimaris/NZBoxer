"""
TorBox API Client
=================
Integrates with the TorBox API to check cache (if applicable) and send
NZBs for downloading.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

from app.config import DEFAULT_USER_AGENT, settings
from app.core.rate_limiter import RateLimiter, RollingWindowRateLimiter

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
_send_limiter = RollingWindowRateLimiter(60, 3600.0)  # 60/hour rolling window limit
_poll_limiter = RateLimiter(10.0)

TORBOX_BASE_URL = "https://api.torbox.app/v1"


class TorBoxError(Exception):
    pass


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
) -> dict[str, str | int | None]:
    """Send an NZB URL to TorBox to initiate a Usenet download.

    Args:
        nzb_url: The URL to the NZB file (from Treasure Maps).
        api_key: Optional explicit API key.
        session: Optional DB session to resolve active provider.

    Returns:
        A dictionary with "hash" and "id" if successful, else empty dict.
    """
    key = await resolve_api_key(session=session, explicit_key=api_key)
    if not key:
        logger.warning("TorBox API key missing.")
        return {}

    url = f"{TORBOX_BASE_URL}/api/usenet/createusenetdownload"

    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": DEFAULT_USER_AGENT,
    }

    data = {"link": nzb_url}

    max_retries = 3
    for attempt in range(max_retries):
        await _send_limiter.wait()
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.post(url, headers=headers, data=data)
                response.raise_for_status()

                result = response.json()
                if result.get("success"):
                    resp_data = result.get("data", {})
                    tb_id = (
                        resp_data.get("usenet_id") 
                        or resp_data.get("id") 
                        or resp_data.get("download_id")
                        or resp_data.get("usenet_download_id")
                    )
                    if not tb_id:
                        logger.warning("TorBox link upload success but no known ID field found in data: %s", resp_data)
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
                    if e.response.status_code not in (429, 500, 502, 503, 504):
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
                    logger.error(
                        "Failed to send NZB to TorBox after %d attempts: [%s] %s",
                        max_retries,
                        type(e).__name__,
                        e,
                    )
                    return {}

    return {}


async def send_nzb_file(
    nzb_bytes: bytes,
    filename: str = "file.nzb",
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> dict[str, str | int | None]:
    """Send raw NZB file content bytes to TorBox via multipart/form-data.

    Args:
        nzb_bytes: The raw NZB file bytes.
        filename: Optional filename for the upload.
        api_key: Optional explicit API key.
        session: Optional DB session to resolve active provider.

    Returns:
        A dictionary with "hash" and "id" if successful, else error dict.
    """
    key = await resolve_api_key(session=session, explicit_key=api_key)
    if not key:
        logger.warning("TorBox API key missing.")
        return {}

    url = f"{TORBOX_BASE_URL}/api/usenet/createusenetdownload"
    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    files = {"file": (filename, nzb_bytes, "application/x-nzb")}

    max_retries = 3
    for attempt in range(max_retries):
        await _send_limiter.wait()
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.post(url, headers=headers, files=files)
                response.raise_for_status()

                result = response.json()
                if result.get("success"):
                    resp_data = result.get("data", {})
                    tb_id = (
                        resp_data.get("usenet_id") 
                        or resp_data.get("id") 
                        or resp_data.get("download_id")
                        or resp_data.get("usenet_download_id")
                    )
                    if not tb_id:
                        logger.warning("TorBox success but no known ID field found in data: %s", resp_data)
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
                    if e.response.status_code not in (429, 500, 502, 503, 504):
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
                    logger.error(
                        "Failed to send NZB file to TorBox after %d attempts: [%s] %s",
                        max_retries,
                        type(e).__name__,
                        e,
                    )
                    return {}

    return {}


async def send_magnet_link(
    magnet_url: str,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> dict[str, str | int | None]:
    """Send a Magnet/Torrent URL to TorBox.

    Args:
        magnet_url: The magnet URI or torrent URL.
        api_key: Optional explicit API key.
        session: Optional DB session to resolve active provider.

    Returns:
        A dictionary with "hash" and "id" if successful, else empty dict.
    """
    key = await resolve_api_key(session=session, explicit_key=api_key)
    if not key:
        logger.warning("TorBox API key missing.")
        return {}

    url = f"{TORBOX_BASE_URL}/api/torrents/createtorrent"

    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": DEFAULT_USER_AGENT,
    }

    data = {"magnet": magnet_url}

    max_retries = 3
    for attempt in range(max_retries):
        await _send_limiter.wait()
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.post(url, headers=headers, data=data)
                response.raise_for_status()

                result = response.json()
                if result.get("success"):
                    resp_data = result.get("data", {})
                    tb_id = (
                        resp_data.get("torrent_id") 
                        or resp_data.get("id") 
                        or resp_data.get("download_id")
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
                    if e.response.status_code not in (429, 500, 502, 503, 504):
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
                    logger.error(
                        "Failed to send Magnet to TorBox after %d attempts: [%s] %s",
                        max_retries,
                        type(e).__name__,
                        e,
                    )
                    return {}

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


async def delete_download(
    download_id: int,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """Deletes a download from TorBox."""
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
            logger.error(f"Failed to delete TorBox download {download_id}: {e}")
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
    bypass_cache: bool = True,
    api_key: str | None = None,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """Retrieve list of active Usenet downloads from TorBox."""
    key = await resolve_api_key(session=session, explicit_key=api_key)
    if not key:
        return []

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
                return data
            return []
        except httpx.HTTPError as e:
            logger.error(f"Failed to get TorBox usenet downloads: {e}")
            return []
