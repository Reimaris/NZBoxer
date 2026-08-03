"""
TorBox API Client
=================
Integrates with the TorBox API to check cache (if applicable) and send
NZBs for downloading.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.config import settings
from app.core.rate_limiter import RateLimiter, RollingWindowRateLimiter

logger = logging.getLogger(__name__)
_send_limiter = RollingWindowRateLimiter(60, 3600.0) # 60/hour rolling window limit
_poll_limiter = RateLimiter(10.0)

TORBOX_BASE_URL = "https://api.torbox.app/v1"



class TorBoxError(Exception):
    pass


async def send_nzb_link(nzb_url: str) -> dict[str, str | int | None]:
    """Send an NZB URL to TorBox to initiate a Usenet download.

    Args:
        nzb_url: The URL to the NZB file (from Treasure Maps).

    Returns:
        A dictionary with "hash" and "id" if successful, else empty dict.
    """
    if not settings.torbox_api_key:
        logger.warning("TorBox API key missing.")
        return {}
        return {}

    url = f"{TORBOX_BASE_URL}/api/usenet/createusenetdownload"

    headers = {
        "Authorization": f"Bearer {settings.torbox_api_key}"
    }

    data = {
        "link": nzb_url
    }

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
                    return {
                        "hash": resp_data.get("hash"),
                        "id": resp_data.get("usenet_id") or resp_data.get("id")
                    }
                else:
                    logger.error("TorBox API returned error: %s", result.get("detail"))
                    return {}

            except httpx.HTTPError as e:
                is_retriable = True
                if isinstance(e, httpx.HTTPStatusError):
                    # Nur bei 500, 502, 503, 504 oder 429 einen Retry versuchen
                    if e.response.status_code not in (429, 500, 502, 503, 504):
                        is_retriable = False
                
                if not is_retriable:
                    logger.error("Failed to send NZB to TorBox (non-retriable): [%s] %s", type(e).__name__, e)
                    return {}
                
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 3
                    logger.warning("Failed to send NZB to TorBox: [%s] %s. Retrying in %ss...", type(e).__name__, e, wait_time)
                    await asyncio.sleep(wait_time)
                else:
                    logger.error("Failed to send NZB to TorBox after %d attempts: [%s] %s", max_retries, type(e).__name__, e)
                    return {}

    return {}


async def check_download_status(download_id: str | int) -> dict[str, Any]:
    """Check the status of a specific TorBox download.
    
    Args:
        download_id: The TorBox ID for the download.
        
    Returns:
        A dictionary containing "status" and "detail". Status can be:
        "completed", "downloading", "failed", "error", etc.
    """
    if not settings.torbox_api_key:
        return {"status": "error", "detail": "Missing API key"}

    url = f"{TORBOX_BASE_URL}/api/usenet/mylist"
    headers = {
        "Authorization": f"Bearer {settings.torbox_api_key}"
    }

    await _poll_limiter.wait()
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            # We fetch the list and find the specific ID
            # In a real app we might want to paginate, but let's assume it's in the first page
            # TorBox usually returns the whole list or we can filter by id (if their API supports it)
            # We'll just fetch all and find it.
            response = await client.get(url, headers=headers)
            response.raise_for_status()

            result = response.json()
            if result.get("success"):
                downloads = result.get("data", [])
                for d in downloads:
                    if str(d.get("id")) == str(download_id):
                        # download_state is usually what TorBox returns (e.g. downloading, completed, error, paused)
                        return {
                            "status": d.get("download_state", "unknown"),
                            "progress": d.get("progress", 0),
                            "detail": d.get("name", "")
                        }
                return {"status": "not_found", "detail": "Download ID not found in TorBox"}
            else:
                return {"status": "error", "detail": result.get("detail")}

        except httpx.HTTPError as e:
            return {"status": "error", "detail": str(e)}
