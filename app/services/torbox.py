"""
TorBox API Client
=================
Integrates with the TorBox API to check cache (if applicable) and send
NZBs for downloading.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

TORBOX_BASE_URL = "https://api.torbox.app/v1"


class TorBoxError(Exception):
    pass


async def send_nzb_link(nzb_url: str) -> str | None:
    """Send an NZB URL to TorBox to initiate a Usenet download.

    Args:
        nzb_url: The URL to the NZB file (from Treasure Maps).

    Returns:
        The TorBox download ID or hash if successful, else None.
    """
    if not settings.torbox_api_key:
        logger.warning("TorBox API key missing.")
        return None

    # V1 API endpoint for Usenet download creation
    # For file URLs, you often pass `link` to the create API.
    # Note: The exact TorBox Usenet API endpoint might differ; we assume /api/usenet/createusenetdownload
    url = f"{TORBOX_BASE_URL}/api/usenet/createusenetdownload"
    
    headers = {
        "Authorization": f"Bearer {settings.torbox_api_key}"
    }
    
    data = {
        "link": nzb_url
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            # First, check if Torbox has a cache check for Usenet.
            # TorBox primarily caches torrents. We will just send it.
            response = await client.post(url, headers=headers, data=data)
            response.raise_for_status()
            
            result = response.json()
            if result.get("success"):
                return result.get("data", {}).get("hash", "success_no_hash")
            else:
                logger.error("TorBox API returned error: %s", result.get("detail"))
                return None
                
        except httpx.HTTPError as e:
            logger.error("Failed to send NZB to TorBox: %s", e)
            return None
