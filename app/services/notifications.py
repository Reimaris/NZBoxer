"""
Unified Event & Batch Notification Service (v3.0.0)
===================================================
Exposes `format_batch_target_summary`, `dispatch_batch_notification_event`,
`dispatch_notification_event`, and `format_target_label` for show-level
batch notification rollups across Discord Webhooks and Telegram.
"""

from __future__ import annotations

from app.services.discord import (
    build_discord_embed,
    dispatch_alert_message,
    dispatch_batch_notification_event,
    dispatch_notification_event,
    format_batch_target_summary,
    format_target_label,
    format_telegram_event_message,
    send_discord_webhook,
)

__all__ = [
    "build_discord_embed",
    "dispatch_alert_message",
    "dispatch_batch_notification_event",
    "dispatch_notification_event",
    "format_batch_target_summary",
    "format_target_label",
    "format_telegram_event_message",
    "send_discord_webhook",
]
