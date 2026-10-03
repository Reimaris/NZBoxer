"""
Discord Webhook Client & Unified Event Notification Dispatcher (v3.0.0)
=======================================================================
Delivers rich, color-coded Discord Embeds alongside HTML Telegram notifications,
governed by the 4 per-event boolean flags on `SystemSettings`:
  - `notify_on_push_initiated` (Default: False)
  - `notify_on_completed`      (Default: True)
  - `notify_on_failure`        (Default: True)
  - `notify_on_auto_advance`   (Default: True)
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select

from app.config import DEFAULT_USER_AGENT, settings
from app.services import telegram

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import DownloadHistory, Episode, MediaItem, Season

logger = logging.getLogger(__name__)

# Color-coded Discord Embed borders by event type (ADR-070)
EVENT_COLORS: dict[str, int] = {
    "completed": 0x10B981,  # Green (#10b981) — Ready on TorBox
    "ready_on_torbox": 0x10B981,
    "failure": 0xEF4444,  # Red (#ef4444) — Push Failed
    "failed": 0xEF4444,
    "auto_replaced": 0xF59E0B,  # Amber (#f59e0b) — Failed & Auto-Replaced
    "auto_advance": 0x9333EA,  # Purple (#9333ea) — Auto-Advance Season Triggered
    "push_initiated": 0xD40060,  # Pink (#d40060) — Push Initiated
    "test": 0x10B981,
}

EVENT_HEADERS: dict[str, str] = {
    "completed": "✅ Ready on TorBox",
    "ready_on_torbox": "✅ Ready on TorBox",
    "failure": "❌ Push Failed",
    "failed": "❌ Push Failed",
    "auto_replaced": "⚠️ Push Failed — Auto-Replaced",
    "auto_advance": "⏭️ Auto-Advance Season Triggered",
    "push_initiated": "🚀 Push Initiated",
    "test": "🔔 NZBoxer Test Notification",
}

# Maps canonical and alias event names to the corresponding SystemSettings flag attribute
EVENT_FLAG_ATTRS: dict[str, str] = {
    "push_initiated": "notify_on_push_initiated",
    "completed": "notify_on_completed",
    "ready_on_torbox": "notify_on_completed",
    "failure": "notify_on_failure",
    "failed": "notify_on_failure",
    "auto_replaced": "notify_on_failure",
    "auto_advance": "notify_on_auto_advance",
}


def format_target_label(
    item: MediaItem | None = None,
    season: Season | None = None,
    episode: Episode | None = None,
) -> str:
    """Format a human-readable target label (e.g. 'Movie', 'Season 1', 'Movie 1 — Mugen Train', 'S01E05')."""
    if episode is not None:
        s_num = season.season_number if season is not None else 1
        return f"S{s_num:02d}E{episode.episode_number:02d}"
    if season is not None:
        entry_type = getattr(season, "entry_type", "season") or "season"
        type_num = int(season.type_number or season.season_number or 1)
        if entry_type == "movie":
            if season.title and (not item or season.title != item.title):
                return f"Movie {type_num} — {season.title}"
            return f"Movie {type_num}"
        if season.title and (
            not item
            or (
                season.title != item.title
                and season.title.lower() != f"season {type_num}"
            )
        ):
            return f"Season {type_num} — {season.title}"
        return f"Season {type_num}"
    return "Movie"


def _format_quality_badges(
    resolution: str | None = None,
    source: str | None = None,
    video_codec: str | None = None,
    audio_codec: str | None = None,
    language: str | None = None,
) -> str:
    badges: list[str] = []
    if resolution:
        badges.append(str(resolution))
    if source:
        badges.append(str(source).upper())
    if video_codec:
        badges.append(str(video_codec).upper())
    if audio_codec:
        badges.append(str(audio_codec).upper())
    if language:
        badges.append(str(language).upper())
    return " • ".join(badges) if badges else "Standard"


def build_discord_embed(
    event_type: str,
    media_title: str,
    media_year: int | None = None,
    target_label: str = "Movie",
    release_name: str | None = None,
    resolution: str | None = None,
    source: str | None = None,
    video_codec: str | None = None,
    audio_codec: str | None = None,
    language: str | None = None,
    status_reason: str | None = None,
    poster_url: str | None = None,
) -> dict[str, Any]:
    """Construct a rich Discord Webhook embed dictionary for the given notification event."""
    norm_event = (event_type or "completed").strip().lower()
    color = EVENT_COLORS.get(norm_event, 0x10B981)
    header = EVENT_HEADERS.get(norm_event, "🔔 NZBoxer Notification")

    title_with_year = (
        f"{media_title} ({media_year})" if media_year else str(media_title)
    )
    embed_title = f"{header} — {title_with_year}"

    quality_str = _format_quality_badges(
        resolution=resolution,
        source=source,
        video_codec=video_codec,
        audio_codec=audio_codec,
        language=language,
    )

    fields: list[dict[str, Any]] = [
        {"name": "Title", "value": title_with_year, "inline": True},
        {"name": "Target", "value": target_label or "Movie", "inline": True},
        {"name": "Quality & Language", "value": quality_str, "inline": True},
    ]

    if release_name:
        fields.append(
            {
                "name": "Release",
                "value": f"`{release_name}`",
                "inline": False,
            }
        )

    if status_reason:
        fields.append(
            {
                "name": "Status",
                "value": str(status_reason),
                "inline": False,
            }
        )

    embed: dict[str, Any] = {
        "title": embed_title,
        "color": color,
        "fields": fields,
        "footer": {"text": "NZBoxer v3.0"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if poster_url and str(poster_url).strip().startswith(("http://", "https://")):
        embed["thumbnail"] = {"url": str(poster_url).strip()}

    return embed


def _extract_discord_retry_after(resp: Any) -> float:
    """Extract retry_after duration in seconds from a Discord HTTP 429 response."""
    retry_val: float | None = None
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("retry_after") is not None:
            retry_val = float(data["retry_after"])
    except Exception:
        pass

    if retry_val is None:
        headers = getattr(resp, "headers", None) or {}
        raw_hdr = headers.get("Retry-After") or headers.get("retry-after")
        if raw_hdr is not None:
            try:
                retry_val = float(raw_hdr)
            except (TypeError, ValueError):
                retry_val = None

    if retry_val is None or retry_val <= 0:
        retry_val = 2.0
    return min(retry_val, 15.0)


async def send_discord_webhook(
    webhook_url: str | None,
    embed: dict[str, Any] | None = None,
    content: str | None = None,
) -> bool:
    """Send a rich embed or message to a Discord Webhook URL with HTTP 429 backoff retry.

    Returns True on HTTP 2xx (200/204), or False on invalid URL / HTTP error.
    """
    if not webhook_url or not str(webhook_url).strip():
        logger.debug("Discord webhook skipped: URL is empty.")
        return False

    clean_url = str(webhook_url).strip()
    payload: dict[str, Any] = {"username": "NZBoxer"}
    if content:
        payload["content"] = content
    if embed:
        payload["embeds"] = [embed]

    if not content and not embed:
        return False

    max_attempts = 3
    try:
        async with httpx.AsyncClient(
            timeout=10.0, headers={"User-Agent": DEFAULT_USER_AGENT}
        ) as client:
            for attempt in range(1, max_attempts + 1):
                resp = await client.post(clean_url, json=payload)
                if getattr(resp, "status_code", None) == 429 and attempt < max_attempts:
                    retry_after = _extract_discord_retry_after(resp)
                    logger.warning(
                        "Discord webhook rate-limited (HTTP 429). Retrying in %.2fs (attempt %d/%d).",
                        retry_after,
                        attempt,
                        max_attempts,
                    )
                    await asyncio.sleep(retry_after)
                    continue
                resp.raise_for_status()
                logger.debug("Discord webhook delivered successfully.")
                return True
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Discord webhook failed with HTTP %d: %s",
            exc.response.status_code,
            exc.response.text,
        )
        return False
    except httpx.RequestError as exc:
        logger.error("Network error while sending Discord webhook: %s", exc)
        return False
    except Exception as exc:
        logger.error("Unexpected error while sending Discord webhook: %s", exc)
        return False
    return False


def format_telegram_event_message(
    event_type: str,
    media_title: str,
    media_year: int | None = None,
    target_label: str = "Movie",
    release_name: str | None = None,
    resolution: str | None = None,
    source: str | None = None,
    video_codec: str | None = None,
    audio_codec: str | None = None,
    language: str | None = None,
    status_reason: str | None = None,
) -> str:
    """Format an HTML notification message for Telegram matching the event details."""
    norm_event = (event_type or "completed").strip().lower()
    header = EVENT_HEADERS.get(norm_event, "🔔 NZBoxer Notification")
    title_with_year = (
        f"{media_title} ({media_year})" if media_year else str(media_title)
    )
    quality_str = _format_quality_badges(
        resolution=resolution,
        source=source,
        video_codec=video_codec,
        audio_codec=audio_codec,
        language=language,
    )

    lines = [
        f"<b>{header}</b>",
        f"🎬 <b>{title_with_year}</b> — {target_label}",
        f"🏷️ <b>Quality:</b> {quality_str}",
    ]
    if release_name:
        lines.append(f"📦 <code>{release_name}</code>")
    if status_reason:
        lines.append(f"ℹ️ {status_reason}")
    return "\n".join(lines)


async def dispatch_notification_event(
    session: AsyncSession | None,
    event_type: str,
    media_title: str,
    media_year: int | None = None,
    target_label: str = "Movie",
    release_name: str | None = None,
    resolution: str | None = None,
    source: str | None = None,
    video_codec: str | None = None,
    audio_codec: str | None = None,
    language: str | None = None,
    status_reason: str | None = None,
    poster_url: str | None = None,
) -> dict[str, Any]:
    """Unified event-driven notification dispatcher across Telegram and Discord Webhooks.

    Checks the 4 per-event boolean flags (`notify_on_push_initiated`,
    `notify_on_completed`, `notify_on_failure`, `notify_on_auto_advance`)
    on `SystemSettings` (or in-memory `settings` fallback) and fans out to
    all enabled channels.
    """
    from app.db.models import NotificationChannel, SystemSettings

    norm_event = (event_type or "").strip().lower()
    flag_attr = EVENT_FLAG_ATTRS.get(norm_event)

    db_settings: SystemSettings | None = None
    if session is not None:
        stmt = select(SystemSettings).where(SystemSettings.id == 1)
        db_settings = (await session.execute(stmt)).scalars().first()

    # Check if the event type is enabled
    if flag_attr:
        if db_settings is not None:
            is_enabled = bool(getattr(db_settings, flag_attr, False))
        else:
            is_enabled = bool(getattr(settings, flag_attr, False))

        if not is_enabled:
            return {
                "dispatched": False,
                "telegram_sent": 0,
                "discord_sent": False,
                "reason": f"Event '{norm_event}' is disabled ({flag_attr}=False)",
            }

    # 1. Fan out to configured Telegram channels
    telegram_sent = 0
    tg_channels: list[NotificationChannel] = []
    if session is not None:
        tg_stmt = select(NotificationChannel).where(
            NotificationChannel.type == "telegram"
        )
        tg_channels = list((await session.execute(tg_stmt)).scalars().all())

    if tg_channels:
        tg_msg = format_telegram_event_message(
            event_type=norm_event,
            media_title=media_title,
            media_year=media_year,
            target_label=target_label,
            release_name=release_name,
            resolution=resolution,
            source=source,
            video_codec=video_codec,
            audio_codec=audio_codec,
            language=language,
            status_reason=status_reason,
        )
        for ch in tg_channels:
            if ch.bot_token and ch.chat_id:
                ok = await telegram.send_notification(tg_msg, ch.bot_token, ch.chat_id)
                if ok:
                    telegram_sent += 1

    # 2. Fan out to Discord Webhook if enabled
    discord_sent = False
    discord_enabled = (
        bool(db_settings.discord_enabled)
        if db_settings is not None
        else bool(settings.discord_enabled)
    )
    discord_url = (
        db_settings.discord_webhook_url
        if db_settings is not None
        else settings.discord_webhook_url
    )

    if discord_enabled and discord_url and str(discord_url).strip():
        embed = build_discord_embed(
            event_type=norm_event,
            media_title=media_title,
            media_year=media_year,
            target_label=target_label,
            release_name=release_name,
            resolution=resolution,
            source=source,
            video_codec=video_codec,
            audio_codec=audio_codec,
            language=language,
            status_reason=status_reason,
            poster_url=poster_url,
        )
        discord_sent = await send_discord_webhook(discord_url, embed=embed)

    return {
        "dispatched": (telegram_sent > 0) or discord_sent,
        "telegram_sent": telegram_sent,
        "discord_sent": discord_sent,
    }


async def dispatch_alert_message(
    session: AsyncSession | None,
    flag_attr: str,
    title: str,
    body: str,
    color: int = EVENT_COLORS["failure"],
) -> dict[str, Any]:
    """Send a plain alert (title + preformatted body) to Telegram and Discord.

    Used for non-media alerts such as forwarded ERROR logs and rate-limit
    notices. Gated by the given `SystemSettings` flag (e.g. `notify_on_errors`).
    """
    import html

    from app.db.models import NotificationChannel, SystemSettings

    db_settings: SystemSettings | None = None
    if session is not None:
        stmt = select(SystemSettings).where(SystemSettings.id == 1)
        db_settings = (await session.execute(stmt)).scalars().first()

    source: Any = db_settings if db_settings is not None else settings
    if not bool(getattr(source, flag_attr, False)):
        return {
            "dispatched": False,
            "telegram_sent": 0,
            "discord_sent": False,
            "reason": f"Alert disabled ({flag_attr}=False)",
        }

    telegram_sent = 0
    if session is not None:
        tg_stmt = select(NotificationChannel).where(
            NotificationChannel.type == "telegram"
        )
        tg_channels = list((await session.execute(tg_stmt)).scalars().all())
        tg_msg = f"<b>{html.escape(title)}</b>\n<pre>{html.escape(body)}</pre>"
        for ch in tg_channels:
            if ch.bot_token and ch.chat_id:
                if await telegram.send_notification(tg_msg, ch.bot_token, ch.chat_id):
                    telegram_sent += 1

    discord_sent = False
    discord_url = getattr(source, "discord_webhook_url", None)
    if bool(getattr(source, "discord_enabled", False)) and discord_url:
        embed = {
            "title": title[:256],
            "description": f"```\n{body}\n```"[:4096],
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "NZBoxer"},
        }
        discord_sent = await send_discord_webhook(discord_url, embed=embed)

    return {
        "dispatched": telegram_sent > 0 or discord_sent,
        "telegram_sent": telegram_sent,
        "discord_sent": discord_sent,
    }


def _contiguous_runs(sorted_nums: list[int]) -> list[tuple[int, int]]:
    """Group a sorted list of unique integers into contiguous (start, end) ranges."""
    if not sorted_nums:
        return []
    runs: list[tuple[int, int]] = []
    start = prev = sorted_nums[0]
    for n in sorted_nums[1:]:
        if n == prev + 1:
            prev = n
        else:
            runs.append((start, prev))
            start = prev = n
    runs.append((start, prev))
    return runs


def format_batch_target_summary(
    histories: Sequence[DownloadHistory],
    item: MediaItem | None = None,
) -> str:
    """Format a compact target range summary for 1 or more DownloadHistory rows of a show."""
    if not histories:
        return "Movie"

    if len(histories) == 1:
        h = histories[0]
        snap_lbl = getattr(h, "target_label", None)
        if snap_lbl:
            return str(snap_lbl)
        return format_target_label(
            item=item or getattr(h, "media_item", None),
            season=getattr(h, "season", None),
            episode=getattr(h, "episode", None),
        )

    eps_by_season: dict[int, set[int]] = {}
    pack_labels: list[str] = []

    for h in histories:
        ep_obj = getattr(h, "episode", None)
        s_obj = getattr(h, "season", None)
        snap_lbl = getattr(h, "target_label", None)

        if ep_obj is not None:
            ep_num = int(ep_obj.episode_number)
            parent_s = s_obj or getattr(ep_obj, "season", None)
            s_num = (
                int(parent_s.type_number or parent_s.season_number)
                if parent_s is not None
                else 1
            )
            eps_by_season.setdefault(s_num, set()).add(ep_num)
            continue

        if snap_lbl:
            m_snap = re.match(r"^S(\d+)E(\d+)$", str(snap_lbl).strip(), re.IGNORECASE)
            if m_snap:
                eps_by_season.setdefault(int(m_snap.group(1)), set()).add(
                    int(m_snap.group(2))
                )
                continue

        if getattr(h, "episode_id", None) is not None:
            m_title = re.search(r"(?i)S(\d+)\s*E(\d+)", str(h.nzb_title or ""))
            if m_title:
                eps_by_season.setdefault(int(m_title.group(1)), set()).add(
                    int(m_title.group(2))
                )
                continue

        lbl = (
            str(snap_lbl)
            if snap_lbl
            else format_target_label(
                item=item or getattr(h, "media_item", None),
                season=s_obj,
                episode=None,
            )
        )
        if lbl not in pack_labels:
            pack_labels.append(lbl)

    season_parts: list[str] = []
    total_eps = 0
    for s_num in sorted(eps_by_season.keys()):
        sorted_eps = sorted(eps_by_season[s_num])
        total_eps += len(sorted_eps)
        runs = _contiguous_runs(sorted_eps)
        run_strs: list[str] = []
        for idx, (r_start, r_end) in enumerate(runs):
            if idx == 0:
                if r_start == r_end:
                    run_strs.append(f"S{s_num:02d}E{r_start:02d}")
                else:
                    run_strs.append(
                        f"S{s_num:02d}E{r_start:02d}–S{s_num:02d}E{r_end:02d}"
                    )
            else:
                if r_start == r_end:
                    run_strs.append(f"E{r_start:02d}")
                else:
                    run_strs.append(f"E{r_start:02d}–E{r_end:02d}")
        season_parts.append(", ".join(run_strs))

    combined_parts = pack_labels + season_parts
    summary = ", ".join(combined_parts) if combined_parts else "Movie"
    if total_eps > 1:
        return f"{summary} ({total_eps} Episodes)"
    return summary


async def dispatch_batch_notification_event(
    session: AsyncSession | None,
    *,
    event_type: str,
    histories: Sequence[DownloadHistory],
    item: MediaItem | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dispatch a single consolidated show-level notification for a batch of DownloadHistory rows."""
    if not histories:
        return {
            "dispatched": False,
            "telegram_sent": 0,
            "discord_sent": False,
            "reason": "Empty batch",
        }

    first = histories[0]
    parent_item = item or getattr(first, "media_item", None)
    media_title = (
        (parent_item.title if parent_item is not None else None)
        or getattr(first, "media_title", None)
        or "Unknown Title"
    )
    media_year = (
        parent_item.year
        if parent_item is not None
        else getattr(first, "media_year", None)
    )
    poster_url = (
        parent_item.poster_url
        if parent_item is not None
        else getattr(first, "poster_url", None)
    )

    target_label = format_batch_target_summary(histories, item=parent_item)
    if len(histories) == 1:
        release_name = first.nzb_title
    else:
        release_name = (
            f"{len(histories)} releases ({first.nzb_title} + {len(histories) - 1} more)"
        )

    norm_event = (event_type or "completed").strip().lower()
    if reason:
        effective_reason = reason
    elif norm_event in ("completed", "ready_on_torbox"):
        effective_reason = "Ready on TorBox (Verified Playable Video)"
    elif norm_event == "auto_advance":
        effective_reason = "Auto-Advance Season Expansion triggered"
    elif norm_event in ("failure", "failed"):
        effective_reason = "One or more transfers failed on TorBox."
    else:
        effective_reason = (
            "Auto-Push Best dispatched to TorBox"
            if (first.push_mode or "auto") == "auto"
            else "Manual Pick dispatched to TorBox"
        )

    total_auto_replaced = sum(
        int(getattr(h, "auto_replaced_count", 0) or 0) for h in histories
    )
    if total_auto_replaced > 0 and "auto-replaced" not in effective_reason.lower():
        effective_reason = f"{effective_reason} • Auto-replaced {total_auto_replaced} failed transfer(s) during acquisition."

    return await dispatch_notification_event(
        session=session,
        event_type=norm_event,
        media_title=media_title,
        media_year=media_year,
        target_label=target_label,
        release_name=release_name,
        resolution=first.resolution,
        source=first.source,
        video_codec=first.video_codec,
        audio_codec=first.audio_codec,
        language=first.grabbed_language,
        status_reason=effective_reason,
        poster_url=poster_url,
    )
