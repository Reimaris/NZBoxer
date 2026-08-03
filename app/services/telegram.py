"""
Telegram Notification Service
=============================
Sends notifications to a Telegram chat using a bot token.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


async def send_notification(message: str, token: str, chat_id: str) -> bool:
    """Send a notification message to Telegram.
    
    Args:
        message: The message to send.
        token: Telegram bot token.
        chat_id: Telegram chat ID.
        
    Returns:
        True if successful, False otherwise.
    """

    if not token or not chat_id:
        logger.debug("Telegram credentials not configured. Skipping notification.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            logger.info("Telegram notification sent successfully.")
            return True
        except httpx.HTTPError as e:
            logger.error("Failed to send Telegram notification: %s", e)
            return False
