"""
NZBoxer Main Application Entrypoint
====================================
FastAPI application with lifespan for database initialization and
APScheduler background jobs. Contains HTML routes for the dashboard
and API endpoints for HTMX interactions.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import reload_settings_from_db, settings
from app.db.database import async_session_factory, close_db, init_db

# Configure logging
config_dir = Path("/app/config")
if not config_dir.exists():
    config_dir = Path("config")  # Fallback for local testing
log_dir = config_dir / "logs"
log_file = log_dir / "nzboxer.log"

formatter = logging.Formatter(
    "%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

try:
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        filename=log_file, when="midnight", interval=1, backupCount=5, encoding="utf-8"
    )
    file_handler.suffix = "%Y-%m-%d.log"
    file_handler.setFormatter(formatter)
except PermissionError:
    file_handler = None
    import sys
    print(f"WARNING: Permission denied creating log file {log_file}. Falling back to console logging.", file=sys.stderr)

root_logger = logging.getLogger()
root_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
if root_logger.hasHandlers():
    root_logger.handlers.clear()
root_logger.addHandler(console_handler)
if file_handler:
    root_logger.addHandler(file_handler)

logger = logging.getLogger(__name__)

# Silence noisy loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# Scheduler instance
scheduler = AsyncIOScheduler()

# Templates setup
_ROOT = Path(__file__).parent.parent
templates_dir = _ROOT / "templates"
static_dir = _ROOT / "static"

# Ensure directories exist
templates_dir.mkdir(exist_ok=True)
static_dir.mkdir(exist_ok=True)

templates = Jinja2Templates(directory=str(templates_dir))


def relative_date(dt: datetime | None) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        now = datetime.now()
    else:
        now = datetime.now(dt.tzinfo)
    delta = dt - now
    days = delta.days
    if days > 0:
        return f"in {days} Tagen"
    elif days < 0:
        return f"vor {abs(days)} Tagen"
    else:
        return "Heute"


templates.env.filters["relative_date"] = relative_date
templates.env.globals["settings"] = settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifecycle manager."""
    # 1. Initialize Database
    await init_db(settings.database_url)

    # 1.5 Load settings from DB into memory
    async with async_session_factory() as session:
        await reload_settings_from_db(session)

    # 2. Setup and Start APScheduler
    from app.core.automation import run_automation_cycle
    from app.core.self_healing import run_self_healing_cycle

    # State to keep track of intervals
    state = {"cycle_count": 0}

    async def _orchestrator_job():
        if settings.automation_state == "disabled":
            logger.info("Automation is DISABLED. Skipping orchestrator cycle.")
            return

        if settings.automation_state == "paused":
            logger.info("Automation is PAUSED. Skipping orchestrator cycle.")
            return

        # Always run self-healing first
        await run_self_healing_cycle()

        # Check if we should run the full scan
        if state["cycle_count"] % settings.scan_interval_multiplier == 0:
            logger.info(
                "Running full automation cycle (Interval multiplier: %d)",
                settings.scan_interval_multiplier,
            )
            await run_automation_cycle()
        else:
            logger.info(
                "Skipping full automation cycle (Interval multiplier: %d, Current cycle: %d)",
                settings.scan_interval_multiplier,
                state["cycle_count"],
            )

        state["cycle_count"] += 1

    interval_minutes = 15  # Base interval is always 15 minutes as requested

    scheduler.add_job(
        _orchestrator_job,
        "interval",
        minutes=interval_minutes,
        id="orchestrator_job",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "APScheduler started. Base interval set to %d minutes.", interval_minutes
    )

    # Yield control to the FastAPI application
    yield

    # 3. Shutdown gracefully
    scheduler.shutdown(wait=False)
    logger.info("APScheduler shut down.")
    await close_db()


# FastAPI App
app = FastAPI(
    title="NZBoxer",
    description="The definitive self-hosted automation solution for German-language NZB releases.",
    version="1.0.0",
    lifespan=lifespan,
)

# Mount static files
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ---------------------------------------------------------------------------
# Routes (HTML & HTMX)
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard displaying the watchlist."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.database import async_session_factory
    from app.db.models import MediaItem, MediaType

    async with async_session_factory() as session:
        # Fetch items
        stmt = (
            select(MediaItem)
            .order_by(MediaItem.created_at.desc())
            .options(
                selectinload(MediaItem.seasons),
                selectinload(MediaItem.download_history),
                selectinload(MediaItem.provider),
            )
        )
        result = await session.execute(stmt)
        items = result.scalars().all()

        # Detailed stats calculations
        movie_items = [i for i in items if i.media_type == MediaType.MOVIE]
        series_items = [i for i in items if i.media_type == MediaType.SHOW]
        anime_items = [i for i in items if i.media_type == MediaType.ANIME]

        movie_stats = {
            "total": len(movie_items),
            "wanted": sum(
                1 for i in movie_items if i.status in ("searching", "pending")
            ),
            "completed": sum(
                1 for i in movie_items if i.status in ("completed", "downloaded")
            ),
            "ignored": sum(
                1 for i in movie_items if i.status in ("ignored", "canceled")
            ),
        }
        series_stats = {
            "total": len(series_items),
            "wanted": sum(
                1 for i in series_items if i.status in ("searching", "pending")
            ),
            "completed": sum(
                1 for i in series_items if i.status in ("completed", "downloaded")
            ),
            "ignored": sum(
                1 for i in series_items if i.status in ("ignored", "canceled")
            ),
        }
        anime_stats = {
            "total": len(anime_items),
            "wanted": sum(
                1 for i in anime_items if i.status in ("searching", "pending")
            ),
            "completed": sum(
                1 for i in anime_items if i.status in ("completed", "downloaded")
            ),
            "ignored": sum(
                1 for i in anime_items if i.status in ("ignored", "canceled")
            ),
        }

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "items": items,
            "movie_stats": movie_stats,
            "series_stats": series_stats,
            "anime_stats": anime_stats,
            "movies_wanted": movie_stats["wanted"],
            "series_wanted": series_stats["wanted"],
            "anime_wanted": anime_stats["wanted"],
        },
    )


@app.get("/manual-search", response_class=HTMLResponse)
async def manual_search_page(
    request: Request,
    query: str = "",
    imdb_id: str = "",
    tmdb_id: str = "",
    tvdb_id: str = "",
    category: str = "",
    season: str = "",
    episode: str = "",
):
    """Dedicated dashboard for manual searching with advanced filters."""
    from sqlalchemy import select

    from app.db.database import async_session_factory
    from app.db.models import SystemSettings

    async with async_session_factory() as session:
        stmt = select(SystemSettings).where(SystemSettings.id == 1)
        db_settings = (await session.execute(stmt)).scalar_one_or_none()

        defaults = {}
        if db_settings and db_settings.scoring_settings:
            defaults = db_settings.scoring_settings.get("manual_search_defaults", {})

        # Override with query parameters if present
        if query:
            defaults["query"] = query
        if imdb_id:
            defaults["imdb_id"] = imdb_id
        if tmdb_id:
            defaults["tmdb_id"] = tmdb_id
        if tvdb_id:
            defaults["tvdb_id"] = tvdb_id
        if category:
            defaults["category"] = category
        if season:
            defaults["season"] = season
        if episode:
            defaults["episode"] = episode

    return templates.TemplateResponse(
        request=request, name="manual_search.html", context={"defaults": defaults}
    )


@app.post("/api/search/manual", response_class=HTMLResponse)
async def manual_search(
    request: Request,
    query: str = Form(""),
    category: str = Form("any"),
    language: str = Form("any"),
    resolution: str = Form("any"),
    source: str = Form("any"),
    hdr: str = Form("any"),
    video_codec: str = Form("any"),
    audio_tier: str = Form("any"),
    audio_channels: str = Form("any"),
    limit: int = Form(10),
    save_defaults: bool = Form(False),
    season: str = Form(""),
    episode: str = Form(""),
    imdb_id: str = Form(""),
    tmdb_id: str = Form(""),
    tvdb_id: str = Form(""),
):
    import copy

    from sqlalchemy import select

    from app.core.parser import parse_release_name
    from app.core.scorer import score_release
    from app.db.database import async_session_factory
    from app.db.models import SystemSettings
    from app.services import treasure_maps

    # Save defaults
    if save_defaults:
        async with async_session_factory() as session:
            stmt = select(SystemSettings).where(SystemSettings.id == 1)
            db_settings = (await session.execute(stmt)).scalar_one_or_none()
            if db_settings:
                new_defaults = {
                    "category": category,
                    "language": language,
                    "resolution": resolution,
                    "source": source,
                    "hdr": hdr,
                    "video_codec": video_codec,
                    "audio_tier": audio_tier,
                    "audio_channels": audio_channels,
                    "limit": limit,
                }
                current = (
                    copy.deepcopy(db_settings.scoring_settings)
                    if db_settings.scoring_settings
                    else {}
                )
                current["manual_search_defaults"] = new_defaults
                db_settings.scoring_settings = current
                await session.commit()

    cat_id = None
    if category == "movie":
        cat_id = 2000
    elif category == "series":
        cat_id = 5000
    elif category == "anime":
        cat_id = 5070

    # Convert numeric fields
    season_val = int(season) if season and season.isdigit() else None
    ep_val = None
    if episode:
        ep_val = int(episode) if episode.isdigit() else episode
    tmdb_val = int(tmdb_id) if tmdb_id and tmdb_id.isdigit() else None
    tvdb_val = int(tvdb_id) if tvdb_id and tvdb_id.isdigit() else None
    imdb_val = imdb_id if imdb_id else None

    # Call the right function based on inputs
    try:
        if category == "movie":
            raw_results = await treasure_maps.search_movie(
                title=query, category=cat_id, tmdb_id=tmdb_val, imdb_id=imdb_val
            )
        elif category == "series":
            raw_results = await treasure_maps.search_show(
                title=query,
                category=cat_id,
                season=season_val,
                ep=ep_val,
                tvdb_id=tvdb_val,
                tmdb_id=tmdb_val,
                imdb_id=imdb_val,
            )
        else:
            raw_results = await treasure_maps.search_raw(
                query=query,
                category=cat_id,
                season=season_val,
                ep=ep_val,
                imdb_id=imdb_val,
                tmdb_id=tmdb_val,
                tvdb_id=tvdb_val,
            )
    except treasure_maps.IndexerError as e:
        return templates.TemplateResponse(
            request=request,
            name="partials/search_results.html",
            context={"results": [], "error_message": str(e)},
        )

    valid_results = []

    def is_match(filter_val: str, parsed_val: str | None) -> bool:
        if filter_val == "any":
            return True
        if not parsed_val:
            return False
        return filter_val.lower() in parsed_val.lower()

    def check_audio_tier(filter_val: str, parsed_codec: str | None) -> bool:
        if filter_val == "any":
            return True
        if not parsed_codec:
            return False
        pc = parsed_codec.lower()
        if filter_val == "tier1" and any(x in pc for x in ["truehd", "dts:x", "auro"]):
            return True
        if filter_val == "tier2" and any(x in pc for x in ["dts-hd", "lpcm", "flac"]):
            return True
        if filter_val == "tier3" and "atmos" in pc and ("eac3" in pc or "dd+" in pc):
            return True
        if filter_val == "tier4" and any(
            x in pc for x in ["eac3", "dts", "ac3", "dolby digital"]
        ):
            return True
        if filter_val == "tier5" and any(x in pc for x in ["aac", "opus", "mp3"]):
            return True
        return filter_val == "tier1" and "truehd atmos" in pc

    for item in raw_results:
        title = item.get("title", "")
        if not title:
            continue

        parsed = parse_release_name(title)

        # If user specified a season but NO episode, exclude individual episodes
        if season_val is not None and ep_val is None:
            if parsed.episode is not None:
                continue

        if not is_match(resolution, parsed.resolution):
            continue
        if not is_match(source, parsed.source):
            continue
        if not is_match(hdr, parsed.hdr):
            continue
        if not is_match(video_codec, parsed.video_codec):
            continue
        if not is_match(audio_channels, parsed.audio_channels):
            continue
        if not check_audio_tier(audio_tier, parsed.audio_codec):
            continue

        req_lang = language if language != "any" else None

        sr = score_release(
            parsed,
            size_bytes=item.get("size", 0),
            age_days=0,
            required_language=req_lang,
            api_language=item.get("api_language"),
        )

        if not sr.is_rejected:
            item["score"] = sr.score
            item["parsed"] = parsed
            valid_results.append(item)

    valid_results.sort(key=lambda x: x["score"], reverse=True)
    valid_results = valid_results[:limit]

    if not valid_results:
        return HTMLResponse(
            content='<div class="p-6 bg-[#2a2a32] border border-[#3f3f46] rounded-xl text-center text-[#a1a1aa] shadow-lg"><div class="text-4xl mb-4">🛸</div><h3 class="text-xl text-white font-semibold mb-2">No items found</h3><p>Try loosening your search filters.</p></div>'
        )

    return templates.TemplateResponse(
        request=request,
        name="partials/search_results.html",
        context={"results": valid_results},
    )


@app.get("/items/{item_id}", response_class=HTMLResponse)
async def item_detail(request: Request, item_id: int):
    """Detailed view for a single item (shows seasons if it's a series)."""
    from sqlalchemy.orm import selectinload

    from app.db.database import async_session_factory
    from app.db.models import MediaItem, Season

    async with async_session_factory() as session:
        item = await session.get(
            MediaItem,
            item_id,
            options=[
                selectinload(MediaItem.seasons).selectinload(Season.episodes),
                selectinload(MediaItem.download_history),
            ],
        )

    if not item:
        return HTMLResponse(content="Item not found", status_code=404)

    return templates.TemplateResponse(
        request=request, name="item_detail.html", context={"item": item}
    )


@app.post("/items/{item_id}/seasons/{season_number}/toggle")
async def toggle_season(item_id: int, season_number: int):
    """HTMX endpoint to toggle season monitoring status."""
    from sqlalchemy import select

    from app.db.database import async_session_factory
    from app.db.models import Season

    async with async_session_factory() as session:
        stmt = select(Season).where(
            Season.media_item_id == item_id, Season.season_number == season_number
        )
        result = await session.execute(stmt)
        season = result.scalar_one_or_none()

        if season:
            season.monitored = not season.monitored
            # Return updated button html
            is_monitored = season.monitored
            await session.commit()

            color = "bg-[#d40060]" if is_monitored else "bg-gray-600"
            text = "Monitored" if is_monitored else "Ignored"
            return HTMLResponse(
                content=f'<button hx-post="/items/{item_id}/seasons/{season_number}/toggle" hx-swap="outerHTML" class="{color} text-white px-3 py-1 rounded text-sm">{text}</button>'
            )

    return HTMLResponse(content="Error", status_code=400)


@app.post("/episodes/{episode_id}/search")
async def manual_search_episode_route(episode_id: int):
    """Manually search and download a single episode synchronously."""
    from app.core.automation import manual_search_episode
    from app.db.database import async_session_factory

    async with async_session_factory() as session:
        await manual_search_episode(session, episode_id)
        # We reload the page in both cases so the user sees the updated status (completed/downloaded or failed + fail_count)
        return HTMLResponse(content="<script>window.location.reload();</script>")


@app.post("/items/{item_id}/search")
async def manual_search_movie_route(item_id: int):
    """Manually search and download a single movie synchronously."""
    from app.core.automation import manual_search_movie
    from app.db.database import async_session_factory

    async with async_session_factory() as session:
        await manual_search_movie(session, item_id)
        return HTMLResponse(content="<script>window.location.reload();</script>")


@app.post("/items/{item_id}/retry")
async def retry_item(item_id: int):
    """Reset item and season status to pending and delete blacklisted releases for this item."""
    from sqlalchemy import select

    from app.db.database import async_session_factory
    from app.db.models import (
        BlacklistedRelease,
        MediaItem,
        MediaStatus,
        Season,
        SeasonStatus,
    )

    async with async_session_factory() as session:
        item = await session.get(MediaItem, item_id)
        if item:
            item.status = MediaStatus.PENDING

            # Reset seasons
            stmt = select(Season).where(Season.media_item_id == item_id)
            result = await session.execute(stmt)
            for season in result.scalars():
                season.status = SeasonStatus.PENDING

            # Clear blacklisted releases for this item
            stmt_bl = select(BlacklistedRelease).where(
                BlacklistedRelease.media_item_id == item_id
            )
            bl_result = await session.execute(stmt_bl)
            for bl in bl_result.scalars():
                await session.delete(bl)

            await session.commit()
            return HTMLResponse(content="<script>window.location.reload();</script>")
    return HTMLResponse(content="Error", status_code=400)


@app.post("/items/{item_id}/confirm_grab")
async def confirm_grab_item(item_id: int):
    """Manually confirm and send the pending_candidate_json to TorBox for a MANUAL_GRAB item."""
    from app.db.database import async_session_factory
    from app.db.models import DownloadHistory, MediaItem, MediaStatus
    from app.services import torbox, treasure_maps

    async with async_session_factory() as session:
        item = await session.get(MediaItem, item_id)
        if not item or not item.pending_candidate_json:
            return HTMLResponse(
                content='<div class="text-red-500">Kein ausstehender Kandidat gefunden.</div>',
                status_code=400,
            )

        candidate = item.pending_candidate_json
        guid = candidate.get("guid", "")
        title = candidate.get("title", "")

        download_url = await treasure_maps.get_download_url(guid)
        torbox_result = await torbox.send_nzb_link(download_url)

        if not torbox_result or (
            not torbox_result.get("hash") and not torbox_result.get("id")
        ):
            return HTMLResponse(
                content='<div class="text-red-500">Fehler beim Senden an TorBox.</div>',
                status_code=500,
            )

        history = DownloadHistory(
            media_item_id=item.id,
            nzb_title=title,
            nzb_guid=guid,
            score=candidate.get("score"),
            size_bytes=candidate.get("size_bytes"),
            resolution=candidate.get("resolution"),
            source=candidate.get("source"),
            release_group=candidate.get("release_group"),
            torbox_hash=str(torbox_result.get("hash"))
            if torbox_result.get("hash")
            else None,
            torbox_id=str(torbox_result.get("id")) if torbox_result.get("id") else None,
        )
        session.add(history)
        item.status = MediaStatus.DOWNLOADING
        item.pending_candidate_json = None
        item.fail_count = 0
        item.last_error = None
        await session.commit()
        return HTMLResponse(content="<script>window.location.reload();</script>")


@app.post("/api/torbox/add", response_class=HTMLResponse)
async def manual_push_to_torbox(magnet: str = Form(...)):
    """Push a search result directly to TorBox."""
    import logging

    from app.services import torbox, treasure_maps

    logger = logging.getLogger(__name__)

    try:
        # Some indexers return GUID as link (e.g. treasure_maps usually uses GUID for download).
        # Or it might be a direct link or magnet.
        if magnet.startswith("magnet:"):
            result = await torbox.send_magnet_link(magnet)
        elif (
            magnet.startswith("http")
            and "api.treasure" not in magnet.lower()
            and "nzb" in magnet.lower()
        ):
            result = await torbox.send_nzb_link(magnet)
        else:
            # If it's a GUID or a generic HTTP link (like Newznab download URL), use send_nzb_link
            # Some indexers require an API key to download, but TreasureMaps handles that internally via `get_download_url` if it's a GUID.
            if not magnet.startswith("http"):
                download_url = await treasure_maps.get_download_url(magnet)
            else:
                download_url = magnet

            result = await torbox.send_nzb_link(download_url)

        if result and (result.get("hash") or result.get("id")):
            return HTMLResponse(
                content='<span class="text-emerald-400 font-medium text-xs px-2 py-1.5 bg-emerald-500/10 border border-emerald-500/20 rounded-md">Sent to TorBox!</span>'
            )
        else:
            err_msg = result.get("error", "Failed to send") if result else "Failed to send"
            # Return 400 so the frontend can display the error without triggering a generic 500
            return HTMLResponse(
                content=f'<span class="text-red-400 font-medium text-xs px-2 py-1.5 bg-red-500/10 border border-red-500/20 rounded-md" title="{err_msg}">{err_msg}</span>',
                status_code=400,
            )
    except Exception as e:
        logger.error(f"Error in manual_push_to_torbox: {e}")
        return HTMLResponse(
            content='<span class="text-red-400 font-medium text-xs px-2 py-1.5 bg-red-500/10 border border-red-500/20 rounded-md">Error</span>',
            status_code=500,
        )


@app.post("/items/{item_id}/ignore")
async def ignore_item(item_id: int):
    """Set an item's status to IGNORED so automation skips it."""
    from app.db.database import async_session_factory
    from app.db.models import MediaItem, MediaStatus

    async with async_session_factory() as session:
        item = await session.get(MediaItem, item_id)
        if item:
            item.status = MediaStatus.IGNORED
            await session.commit()
            return HTMLResponse(content="<script>window.location.reload();</script>")
    return HTMLResponse(content="Error", status_code=400)


@app.post("/items/{item_id}/toggle_anime_type")
async def toggle_anime_type(item_id: int):
    """Toggle whether an Anime is considered a Movie or a Series."""
    from app.db.database import async_session_factory
    from app.db.models import MediaItem

    async with async_session_factory() as session:
        item = await session.get(MediaItem, item_id)
        if item:
            item.is_anime_movie = not item.is_anime_movie
            await session.commit()
            return HTMLResponse(content="<script>window.location.reload();</script>")
    return HTMLResponse(content="Error", status_code=400)


@app.post("/items/{item_id}/toggle_auto_monitor")
async def toggle_auto_monitor(item_id: int):
    """Toggle whether to automatically monitor the next season when current is completed."""
    from app.db.database import async_session_factory
    from app.db.models import MediaItem

    async with async_session_factory() as session:
        item = await session.get(MediaItem, item_id)
        if item:
            item.auto_monitor_next_season = not item.auto_monitor_next_season
            await session.commit()
            return HTMLResponse(content="<script>window.location.reload();</script>")
    return HTMLResponse(content="Error", status_code=400)


@app.delete("/items/{item_id}")
async def delete_item(item_id: int):
    """Delete an item entirely from the database."""
    from app.db.database import async_session_factory
    from app.db.models import MediaItem

    async with async_session_factory() as session:
        item = await session.get(MediaItem, item_id)
        if item:
            await session.delete(item)
            await session.commit()
            return HTMLResponse(
                content=""
            )  # Empty response means success (HTMX can remove element)
    return HTMLResponse(content="Error", status_code=400)


@app.post("/api/automation/state")
async def set_automation_state(state: str = Form(...)):
    """Set the global automation state."""
    from sqlalchemy import select

    from app.config import reload_settings_from_db
    from app.db.database import async_session_factory
    from app.db.models import AutomationState, SystemSettings

    valid_states = {
        "active": AutomationState.ACTIVE,
        "paused": AutomationState.PAUSED,
        "disabled": AutomationState.DISABLED,
    }
    if state not in valid_states:
        return HTMLResponse(
            content='<div class="text-red-500">Invalid state</div>', status_code=400
        )

    async with async_session_factory() as session:
        stmt = select(SystemSettings).where(SystemSettings.id == 1)
        db_settings = (await session.execute(stmt)).scalar_one_or_none()
        if db_settings:
            db_settings.automation_state = valid_states[state]
            await session.commit()
            await reload_settings_from_db(session)

    return HTMLResponse(content="<script>window.location.reload();</script>")


@app.post("/sync")
async def sync_watchlist():
    """Trigger manual Simkl sync."""
    from app.core.automation import sync_simkl_watchlist

    await sync_simkl_watchlist()
    return HTMLResponse(
        content='<div class="p-4 bg-[#d40060] text-white rounded">Sync complete! Reload the page.</div>'
    )


@app.get("/api/status")
async def get_status():
    """Health check and scheduler status."""
    return {
        "status": "ok",
        "scheduler_running": scheduler.running,
        "jobs": [job.id for job in scheduler.get_jobs()],
    }


# ---------------------------------------------------------------------------
# Settings & Configuration Routes
# ---------------------------------------------------------------------------


@app.get("/settings", response_class=HTMLResponse)
async def get_settings_page(request: Request):
    """Render the settings form."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.models import NotificationChannel, Provider, SystemSettings

    async with async_session_factory() as session:
        stmt = select(SystemSettings).where(SystemSettings.id == 1)
        db_settings = (await session.execute(stmt)).scalar_one_or_none()

        providers = (
            (
                await session.execute(
                    select(Provider).options(selectinload(Provider.profiles))
                )
            )
            .scalars()
            .all()
        )
        notifications = (
            (await session.execute(select(NotificationChannel))).scalars().all()
        )

        # Flatten scoring logic for the UI if needed
        scoring = (
            db_settings.scoring_settings
            if db_settings and db_settings.scoring_settings
            else {}
        )

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "db_settings": db_settings,
            "scoring": scoring,
            "providers": providers,
            "notifications": notifications,
        },
    )


@app.get("/settings/export")
async def export_settings():
    """Export all settings as a JSON file."""
    from fastapi.responses import JSONResponse
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.models import NotificationChannel, Provider, SystemSettings

    async with async_session_factory() as session:
        # Get SystemSettings
        stmt = select(SystemSettings).where(SystemSettings.id == 1)
        db_settings = (await session.execute(stmt)).scalar_one_or_none()
        settings_dict = {}
        if db_settings:
            settings_dict = {
                "tmdb_api_key": db_settings.tmdb_api_key,
                "treasure_maps_api_key": db_settings.treasure_maps_api_key,
                "torbox_api_key": db_settings.torbox_api_key,
                "scan_interval_multiplier": db_settings.scan_interval_multiplier,
                "sh_max_retries": db_settings.sh_max_retries,
                "sh_max_time_hours": db_settings.sh_max_time_hours,
                "sh_auto_retry": db_settings.sh_auto_retry,
                "sh_retry_wait_hours": db_settings.sh_retry_wait_hours,
                "scoring_settings": db_settings.scoring_settings,
            }

        # Get Providers
        providers_list = []
        providers = (
            (
                await session.execute(
                    select(Provider).options(selectinload(Provider.profiles))
                )
            )
            .scalars()
            .all()
        )
        for p in providers:
            profiles = []
            for prof in p.profiles:
                profiles.append(
                    {
                        "media_type": prof.media_type,
                        "path": prof.path,
                        "mode": prof.mode,
                        "search_cycle_skip": prof.search_cycle_skip,
                        "resolution": prof.resolution,
                        "languages_csv": prof.languages_csv,
                        "min_mb": prof.min_mb,
                        "max_mb": prof.max_mb,
                        "reject_words_csv": prof.reject_words_csv,
                        "prefer_complete_seasons": prof.prefer_complete_seasons,
                        "episode_block_size": prof.episode_block_size,
                        "notification_channel_id": prof.notification_channel_id,
                    }
                )
            providers_list.append(
                {
                    "name": p.name,
                    "type": p.type,
                    "username": p.username,
                    "access_token": p.access_token,
                    "client_id": p.client_id,
                    "movie_category_id": p.movie_category_id,
                    "series_category_id": p.series_category_id,
                    "bandwidth_mbit": p.bandwidth_mbit,
                    "profiles": profiles,
                }
            )

        # Get Notifications
        notif_list = []
        notifications = (
            (await session.execute(select(NotificationChannel))).scalars().all()
        )
        for n in notifications:
            notif_list.append(
                {
                    "name": n.name,
                    "type": n.type,
                    "bot_token": n.bot_token,
                    "chat_id": n.chat_id,
                }
            )

        export_data = {
            "version": 1,
            "system_settings": settings_dict,
            "providers": providers_list,
            "notifications": notif_list,
        }

    return JSONResponse(
        content=export_data,
        headers={"Content-Disposition": 'attachment; filename="nzboxer_backup.json"'},
    )


@app.post("/settings/global")
async def save_global_settings(
    request: Request,
    tmdb_api_key: str = Form(""),
    treasure_maps_api_key: str = Form(""),
    torbox_api_key: str = Form(""),
    scan_interval_multiplier: int = Form(1),
    sh_max_retries: int = Form(3),
    sh_max_time_hours: float = Form(12.0),
    sh_auto_retry: bool = Form(True),
    sh_retry_wait_hours: float = Form(24.0),
    dry_run: bool = Form(False),
):
    import copy

    from sqlalchemy import select

    from app.core.default_scoring import DEFAULT_SCORING_CONFIG
    from app.db.models import SystemSettings

    form_data = await request.form()

    async with async_session_factory() as session:
        stmt = select(SystemSettings).where(SystemSettings.id == 1)
        db_settings = (await session.execute(stmt)).scalar_one_or_none()

        if db_settings:
            db_settings.tmdb_api_key = tmdb_api_key
            db_settings.treasure_maps_api_key = treasure_maps_api_key
            db_settings.torbox_api_key = torbox_api_key
            db_settings.scan_interval_multiplier = scan_interval_multiplier
            db_settings.sh_max_retries = sh_max_retries
            db_settings.sh_max_time_hours = sh_max_time_hours
            db_settings.sh_auto_retry = sh_auto_retry
            db_settings.sh_retry_wait_hours = sh_retry_wait_hours
            db_settings.dry_run = dry_run

            sc: dict[str, Any] = copy.deepcopy(
                db_settings.scoring_settings or DEFAULT_SCORING_CONFIG
            )

            def _get_int(key: str) -> int | None:
                val = form_data.get(key)
                if val is not None and isinstance(val, (str, int)):
                    try:
                        return int(val)
                    except ValueError:
                        pass
                return None

            scoring_res = sc.setdefault("scoring", {})
            if isinstance(scoring_res, dict):
                res_map = scoring_res.setdefault("resolution", {})
                if isinstance(res_map, dict):
                    v1080 = _get_int("res_1080p")
                    if v1080 is not None:
                        res_map["1080p"] = v1080
                    v2160 = _get_int("res_2160p")
                    if v2160 is not None:
                        res_map["2160p"] = v2160
                    v720 = _get_int("res_720p")
                    if v720 is not None:
                        res_map["720p"] = v720

                vc_map = scoring_res.setdefault("video_codec", {})
                if isinstance(vc_map, dict):
                    vh265 = _get_int("codec_h265")
                    if vh265 is not None:
                        vc_map["h265"] = vh265
                    vh264 = _get_int("codec_h264")
                    if vh264 is not None:
                        vc_map["h264"] = vh264

                src_map = scoring_res.setdefault("source", {})
                if isinstance(src_map, dict):
                    vremux = _get_int("source_remux")
                    if vremux is not None:
                        src_map["remux"] = vremux
                    vbluray = _get_int("source_bluray")
                    if vbluray is not None:
                        src_map["bluray"] = vbluray
                    vwebdl = _get_int("source_webdl")
                    if vwebdl is not None:
                        src_map["web-dl"] = vwebdl
                    vwebrip = _get_int("source_webrip")
                    if vwebrip is not None:
                        src_map["webrip"] = vwebrip

            cutoffs = sc.setdefault("cutoffs", {})
            if isinstance(cutoffs, dict):
                vtarget = _get_int("cutoffs_target")
                if vtarget is not None:
                    cutoffs["target_score"] = vtarget
                vupg = _get_int("cutoffs_upgrade")
                if vupg is not None:
                    cutoffs["upgrade_threshold"] = vupg

            db_settings.scoring_settings = sc

            await session.commit()
            await reload_settings_from_db(session)

    return HTMLResponse(
        content='<div class="p-4 mb-4 bg-[#d40060] text-white rounded">Settings saved successfully!</div>'
    )


@app.get("/settings/provider/new", response_class=HTMLResponse)
@app.get("/settings/provider/{provider_id}/edit", response_class=HTMLResponse)
async def provider_modal(request: Request, provider_id: int | None = None):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.models import NotificationChannel, Provider

    async with async_session_factory() as session:
        provider = None
        if provider_id:
            provider = await session.get(
                Provider, provider_id, options=[selectinload(Provider.profiles)]
            )

        notifications = (
            (await session.execute(select(NotificationChannel))).scalars().all()
        )

    return templates.TemplateResponse(
        request=request,
        name="modals/provider.html",
        context={"provider": provider, "notifications": notifications},
    )


@app.post("/settings/provider", response_class=HTMLResponse)
@app.post("/settings/provider/{provider_id}", response_class=HTMLResponse)
async def save_provider(
    request: Request,
    provider_id: int | None = None,
    name: str = Form(...),
    simkl_username: str = Form(""),
    access_token: str = Form(""),
    client_id: str = Form(""),
    movie_category_id: int = Form(2000),
    series_category_id: int = Form(5000),
    anime_category_id: int = Form(5070),
    bandwidth_mbit: int = Form(None),
    # Unified setting for all profiles
    search_cycle_skip: int = Form(1),
    # Profile settings
    enable_movies: bool = Form(False),
    movies_mode: str = Form(""),
    movies_resolution: str = Form("any"),
    movies_source: str = Form("any"),
    movies_video_codec: str = Form("any"),
    movies_hdr: str = Form("any"),
    movies_audio_tier: str = Form("any"),
    movies_audio_channels: str = Form("any"),
    movies_langs: str = Form(""),
    movies_min_mb: int = Form(None),
    movies_max_mb: int = Form(None),
    movies_reject: str = Form(""),
    enable_series: bool = Form(False),
    series_mode: str = Form(""),
    series_resolution: str = Form("any"),
    series_source: str = Form("any"),
    series_video_codec: str = Form("any"),
    series_hdr: str = Form("any"),
    series_audio_tier: str = Form("any"),
    series_audio_channels: str = Form("any"),
    series_langs: str = Form(""),
    series_min_mb: int = Form(None),
    series_max_mb: int = Form(None),
    series_reject: str = Form(""),
    series_prefer_seasons: bool = Form(False),
    series_block_size: int = Form(5),
    global_notification: int = Form(None),
    enable_anime: bool = Form(False),
    anime_mode: str = Form(""),
    anime_resolution: str = Form("any"),
    anime_source: str = Form("any"),
    anime_video_codec: str = Form("any"),
    anime_hdr: str = Form("any"),
    anime_audio_tier: str = Form("any"),
    anime_audio_channels: str = Form("any"),
    anime_langs: str = Form(""),
    anime_min_mb: int = Form(None),
    anime_max_mb: int = Form(None),
    anime_reject: str = Form(""),
    anime_prefer_seasons: bool = Form(False),
    anime_block_size: int = Form(5),
):
    from sqlalchemy.orm import selectinload

    from app.db.models import Provider, ProviderProfile

    async with async_session_factory() as session:
        if provider_id:
            provider = await session.get(
                Provider, provider_id, options=[selectinload(Provider.profiles)]
            )
            if not provider:
                provider = Provider(type="simkl")
                session.add(provider)
        else:
            provider = Provider(type="simkl")
            session.add(provider)

        provider.name = name
        provider.username = simkl_username
        provider.access_token = access_token
        provider.client_id = client_id
        provider.movie_category_id = movie_category_id
        provider.series_category_id = series_category_id
        provider.anime_category_id = anime_category_id

        provider.bandwidth_mbit = bandwidth_mbit

        await session.flush()  # get ID

        # clear old profiles and recreate
        if provider_id:
            for p in list(provider.profiles):
                await session.delete(p)
            provider.profiles.clear()

        if enable_movies:
            pm = ProviderProfile(
                provider_id=provider.id,
                media_type="movies",
                mode=movies_mode,
                search_cycle_skip=search_cycle_skip,
                resolution=movies_resolution,
                source=movies_source,
                video_codec=movies_video_codec,
                hdr=movies_hdr,
                audio_tier=movies_audio_tier,
                audio_channels=movies_audio_channels,
                languages_csv=movies_langs,
                min_mb=movies_min_mb,
                max_mb=movies_max_mb,
                reject_words_csv=movies_reject,
                notification_channel_id=global_notification
                if global_notification
                else None,
            )
            session.add(pm)

        if enable_series:
            ps = ProviderProfile(
                provider_id=provider.id,
                media_type="shows",
                mode=series_mode,
                search_cycle_skip=search_cycle_skip,
                resolution=series_resolution,
                source=series_source,
                video_codec=series_video_codec,
                hdr=series_hdr,
                audio_tier=series_audio_tier,
                audio_channels=series_audio_channels,
                languages_csv=series_langs,
                min_mb=series_min_mb,
                max_mb=series_max_mb,
                reject_words_csv=series_reject,
                prefer_complete_seasons=series_prefer_seasons,
                episode_block_size=series_block_size,
                notification_channel_id=global_notification
                if global_notification
                else None,
            )
            session.add(ps)

        if enable_anime:
            pa = ProviderProfile(
                provider_id=provider.id,
                media_type="anime",
                mode=anime_mode,
                search_cycle_skip=search_cycle_skip,
                resolution=anime_resolution,
                source=anime_source,
                video_codec=anime_video_codec,
                hdr=anime_hdr,
                audio_tier=anime_audio_tier,
                audio_channels=anime_audio_channels,
                languages_csv=anime_langs,
                min_mb=anime_min_mb,
                max_mb=anime_max_mb,
                reject_words_csv=anime_reject,
                prefer_complete_seasons=anime_prefer_seasons,
                episode_block_size=anime_block_size,
                notification_channel_id=global_notification
                if global_notification
                else None,
            )
            session.add(pa)

        await session.commit()

    # Reload page
    return HTMLResponse(content="<script>window.location.reload();</script>")


@app.delete("/settings/provider/{provider_id}")
async def delete_provider(provider_id: int):
    from app.db.models import Provider

    async with async_session_factory() as session:
        provider = await session.get(Provider, provider_id)
        if provider:
            await session.delete(provider)
            await session.commit()
    return HTMLResponse(content="<script>window.location.reload();</script>")


@app.get("/settings/notification/new", response_class=HTMLResponse)
@app.get("/settings/notification/{notification_id}/edit", response_class=HTMLResponse)
async def notification_modal(request: Request, notification_id: int | None = None):
    from app.db.models import NotificationChannel

    async with async_session_factory() as session:
        notification = None
        if notification_id:
            notification = await session.get(NotificationChannel, notification_id)

    return templates.TemplateResponse(
        request=request,
        name="modals/notification.html",
        context={"notification": notification},
    )


@app.post("/settings/notification", response_class=HTMLResponse)
@app.post("/settings/notification/{notification_id}", response_class=HTMLResponse)
async def save_notification(
    request: Request,
    notification_id: int | None = None,
    name: str = Form(...),
    bot_token: str = Form(""),
    chat_id: str = Form(""),
):
    from app.db.models import NotificationChannel

    async with async_session_factory() as session:
        if notification_id:
            notification = await session.get(NotificationChannel, notification_id)
            if not notification:
                notification = NotificationChannel(type="telegram")
                session.add(notification)
        else:
            notification = NotificationChannel(type="telegram")
            session.add(notification)

        notification.name = name
        notification.bot_token = bot_token
        notification.chat_id = chat_id
        await session.commit()

    return HTMLResponse(content="<script>window.location.reload();</script>")


@app.delete("/settings/notification/{notification_id}")
async def delete_notification(notification_id: int):
    from app.db.models import NotificationChannel

    async with async_session_factory() as session:
        notification = await session.get(NotificationChannel, notification_id)
        if notification:
            await session.delete(notification)
            await session.commit()
    return HTMLResponse(content="<script>window.location.reload();</script>")


@app.post("/simkl/auth/start")
async def start_simkl_auth(client_id: str = Form(...)):
    """Start the Simkl PIN flow."""
    from app.services import simkl

    try:
        data = await simkl.request_pin(client_id)
    except Exception as e:
        return HTMLResponse(
            content=f'<div class="text-red-500">Error requesting PIN: {e}</div>'
        )

    if "user_code" not in data:
        return HTMLResponse(
            content='<div class="text-red-500">Invalid response from Simkl</div>'
        )

    user_code = data["user_code"]
    verification_uri = data.get("verification_uri", "https://simkl.com/pin")

    html = f'''
    <div class="bg-[#1e1e24] border border-[#d40060] rounded p-4 text-center mt-4">
        <h4 class="text-white font-bold mb-2">Simkl Device Authorization</h4>
        <p class="text-[#a1a1aa] text-sm mb-4">Go to <a href="{verification_uri}" target="_blank" class="text-[#d40060] hover:underline font-bold">{verification_uri}</a> and enter the code below:</p>
        <div class="text-3xl font-mono text-[#d40060] tracking-widest mb-4">{user_code}</div>
        
        <div id="simkl-poll-status" hx-get="/simkl/auth/poll?client_id={client_id}&user_code={user_code}" hx-trigger="every 5s" class="text-sm text-yellow-500 animate-pulse">
            Waiting for authorization...
        </div>
    </div>
    '''
    return HTMLResponse(content=html)


@app.get("/simkl/auth/poll")
async def poll_simkl_auth(client_id: str, user_code: str):
    """Poll Simkl for the access token."""
    from app.services import simkl

    try:
        data = await simkl.check_pin(client_id, user_code)
    except Exception as e:
        return HTMLResponse(
            content=f'<div class="text-red-500">Error polling status: {e}</div>'
        )

    if data.get("result") == "OK" and "access_token" in data:
        # Success! Fill the access token field via JS
        access_token = data["access_token"]
        return HTMLResponse(
            content=f'''
            <div class="text-green-500 font-bold mb-4">Successfully authorized!</div>
            <script>
                document.getElementById('access_token').value = "{access_token}";
                document.getElementById('simkl-auth-container-modal').innerHTML = '';
            </script>
        '''
        )

    # Still pending
    return HTMLResponse(
        content=f"""
        <div id="simkl-poll-status" hx-get="/simkl/auth/poll?client_id={client_id}&user_code={user_code}" hx-trigger="every 5s" hx-swap="outerHTML" class="text-sm text-yellow-500 animate-pulse">
            Waiting for authorization...
        </div>
    """
    )


@app.get("/system/check/modal", response_class=HTMLResponse)
async def system_check_modal(request: Request):
    """Render the system check modal."""
    return templates.TemplateResponse(
        request=request, name="modals/system_check.html", context={}
    )


@app.get("/system/check/run", response_class=HTMLResponse)
async def run_system_check(request: Request):
    """Run tests for all configured APIs and return the results HTML."""
    import httpx
    from sqlalchemy import select

    from app.db.models import NotificationChannel, Provider, SystemSettings

    results = {}
    async with async_session_factory() as session:
        db_settings = (
            await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
        ).scalar_one_or_none()
        providers = (await session.execute(select(Provider))).scalars().all()
        notifications = (
            (await session.execute(select(NotificationChannel))).scalars().all()
        )

        async with httpx.AsyncClient(timeout=10.0) as client:
            # 1. TMDB
            if db_settings and db_settings.tmdb_api_key:
                try:
                    h = {}
                    p = {}
                    if db_settings.tmdb_api_key.startswith("ey"):
                        h["Authorization"] = f"Bearer {db_settings.tmdb_api_key}"
                    else:
                        p["api_key"] = db_settings.tmdb_api_key
                    r = await client.get(
                        "https://api.themoviedb.org/3/configuration",
                        params=p,
                        headers=h,
                    )
                    results["tmdb"] = {
                        "ok": r.status_code == 200,
                        "msg": "Erfolgreich"
                        if r.status_code == 200
                        else f"Fehler {r.status_code}",
                    }
                except Exception as e:
                    results["tmdb"] = {"ok": False, "msg": str(e)}
            else:
                results["tmdb"] = {"ok": False, "msg": "API Key fehlt"}

            # 2. Treasure Maps
            if db_settings and db_settings.treasure_maps_api_key:
                try:
                    r = await client.get(
                        "https://treasure-maps.com/api",
                        params={
                            "t": "caps",
                            "apikey": db_settings.treasure_maps_api_key,
                        },
                    )
                    results["treasure_maps"] = {
                        "ok": r.status_code == 200,
                        "msg": "Erfolgreich"
                        if r.status_code == 200
                        else f"Fehler {r.status_code}",
                    }
                except Exception as e:
                    results["treasure_maps"] = {"ok": False, "msg": str(e)}
            else:
                results["treasure_maps"] = {"ok": False, "msg": "API Key fehlt"}

            # 3. TorBox
            if db_settings and db_settings.torbox_api_key:
                try:
                    r = await client.get(
                        "https://api.torbox.app/v1/api/user/me",
                        headers={
                            "Authorization": f"Bearer {db_settings.torbox_api_key}"
                        },
                    )
                    results["torbox"] = {
                        "ok": r.status_code == 200,
                        "msg": "Erfolgreich"
                        if r.status_code == 200
                        else f"Fehler {r.status_code}",
                    }
                except Exception as e:
                    results["torbox"] = {"ok": False, "msg": str(e)}
            else:
                results["torbox"] = {"ok": False, "msg": "API Key fehlt"}

            # 4. Simkl (check first provider)
            if providers and providers[0].access_token and providers[0].client_id:
                try:
                    r = await client.get(
                        "https://api.simkl.com/users/settings",
                        headers={
                            "Authorization": f"Bearer {providers[0].access_token}",
                            "simkl-api-key": providers[0].client_id,
                        },
                    )
                    results["simkl"] = {
                        "ok": r.status_code == 200,
                        "msg": "Erfolgreich"
                        if r.status_code == 200
                        else f"Fehler {r.status_code}",
                    }
                except Exception as e:
                    results["simkl"] = {"ok": False, "msg": str(e)}
            else:
                results["simkl"] = {"ok": False, "msg": "Kein konfigurierter Provider"}

            # 5. Telegram
            if notifications and notifications[0].bot_token:
                try:
                    r = await client.get(
                        f"https://api.telegram.org/bot{notifications[0].bot_token}/getMe"
                    )
                    results["telegram"] = {
                        "ok": r.status_code == 200,
                        "msg": "Erfolgreich"
                        if r.status_code == 200
                        else f"Fehler {r.status_code}",
                    }
                except Exception as e:
                    results["telegram"] = {"ok": False, "msg": str(e)}
            else:
                results["telegram"] = {"ok": False, "msg": "Kein konfigurierter Bot"}

    # Return HTML list of results
    html = '<div class="space-y-4">'
    for key, data in results.items():
        name_map = {
            "tmdb": "TheMovieDB (TMDB)",
            "treasure_maps": "Treasure Maps API",
            "torbox": "TorBox API",
            "simkl": "Simkl API",
            "telegram": "Telegram Bot",
        }
        name = name_map.get(key, key)
        icon = "✅" if data["ok"] else "❌"
        color = "text-green-400" if data["ok"] else "text-red-400"
        html += f'''
        <div class="flex items-center justify-between p-3 bg-[#111115] border border-[#3f3f46] rounded-lg">
            <span class="font-medium text-white">{name}</span>
            <span class="{color} flex items-center gap-2 text-sm">{data["msg"]} {icon}</span>
        </div>
        '''

    html += "</div>"
    return HTMLResponse(content=html)


@app.post("/sync", response_class=HTMLResponse)
async def manual_sync(background_tasks: BackgroundTasks):
    """Trigger manual Simkl sync and show flash message."""
    from app.core.automation import sync_simkl_watchlist

    background_tasks.add_task(sync_simkl_watchlist)
    msg = "Simkl Sync im Hintergrund gestartet."

    return HTMLResponse(
        content=f"""
        <div class="bg-[#3f3f46] text-white px-4 py-3 rounded-md shadow-lg border border-[#52525b] flex items-center justify-between animate-fade-in-down mb-4">
            <div class="flex items-center gap-3">
                <svg class="w-5 h-5 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                <span class="text-sm font-medium">{msg}</span>
            </div>
            <button onclick="this.parentElement.remove()" class="text-gray-400 hover:text-white">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
            </button>
        </div>
    """
    )


@app.post("/search", response_class=HTMLResponse)
async def manual_search_full(background_tasks: BackgroundTasks):
    """Trigger manual full automation cycle."""
    from app.core.automation import run_automation_cycle

    background_tasks.add_task(run_automation_cycle, force=True)

    return HTMLResponse(
        content="""
        <div class="bg-[#d40060] text-white px-4 py-3 rounded-md shadow-lg border border-[#a3004a] flex items-center justify-between animate-fade-in-down mb-4">
            <div class="flex items-center gap-3">
                <svg class="w-5 h-5 text-pink-200" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
                <span class="text-sm font-medium">Suchlauf (Automatisierungszyklus) im Hintergrund gestartet!</span>
            </div>
            <button onclick="this.parentElement.remove()" class="text-gray-300 hover:text-white">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
            </button>
        </div>
    """
    )
