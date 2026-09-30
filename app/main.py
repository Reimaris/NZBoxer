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
from fastapi import BackgroundTasks, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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

file_handler: TimedRotatingFileHandler | None = None
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

    print(
        f"WARNING: Permission denied creating log file {log_file}. Falling back to console logging.",
        file=sys.stderr,
    )

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


logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# Scheduler instance
scheduler = AsyncIOScheduler()

# Templates setup
_ROOT = Path(__file__).parent.parent
templates_dir = _ROOT / "templates"
static_dir = Path(__file__).parent / "static"

# Ensure directories exist
templates_dir.mkdir(exist_ok=True)
static_dir.mkdir(exist_ok=True)

templates = Jinja2Templates(directory=str(templates_dir))


def relative_date(dt: datetime | None) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        today = datetime.now().date()
    else:
        today = datetime.now(dt.tzinfo).date()
    target_date = dt.date()
    days = (target_date - today).days
    if days == 0:
        return "Today"
    elif days == 1:
        return "in 1 day"
    elif days > 1:
        return f"in {days} days"
    elif days == -1:
        return "1 day ago"
    else:
        return f"{abs(days)} days ago"


APP_VERSION = "2.11.2"


templates.env.filters["relative_date"] = relative_date
templates.env.globals["settings"] = settings
templates.env.globals["app_version"] = APP_VERSION


async def run_orchestrator_tick(now: datetime | None = None) -> None:
    """Execute scheduled domain tasks aligned to wall-clock intervals.

    State rules:
    - DISABLED: Halts immediately (no self-healing, download checks, or searches).
    - PAUSED: Runs Self-Healing & Download Checks on schedule, but skips Video searches.
    - UPGRADES_ONLY: Runs Self-Healing & Download Checks, and runs Video automation in upgrades-only mode.
    - ACTIVE: Runs all due domain tasks (Self-Healing/Download Checks, Video Search).
    """
    from datetime import datetime

    from app.core.automation import (
        run_automation_cycle,
        run_periodic_simkl_sync_if_due,
    )
    from app.core.automation_state import automation_state_manager
    from app.core.scheduler_utils import is_interval_due
    from app.core.self_healing import run_download_check_cycle, run_self_healing_cycle

    current_state = (
        settings.automation_state.value
        if hasattr(settings.automation_state, "value")
        else str(settings.automation_state)
    ).lower()

    if current_state == "disabled":
        logger.info("Automation is DISABLED. Skipping orchestrator cycle.")
        return

    if automation_state_manager.is_running():
        logger.info(
            "⏳ Automation is currently running. Skipping orchestrator tick to prevent concurrent execution."
        )
        return

    if now is None:
        now = datetime.now()

    logger.info(
        "⏰ Orchestrator clock tick at %s (State: %s)",
        now.strftime("%Y-%m-%d %H:%M:%S"),
        current_state,
    )

    # 0. Optional Periodic Simkl Watchlist Sync (per provider simkl_config)
    await run_periodic_simkl_sync_if_due(now)

    # 1. Self-Healing & Download Check (Runs in ACTIVE, UPGRADES_ONLY, and PAUSED states)
    if is_interval_due(settings.self_healing_interval, now):
        logger.info(
            "⏰ Running scheduled Self-Healing & Download Check (Interval: %dm)",
            settings.self_healing_interval,
        )
        await run_self_healing_cycle()
        await run_download_check_cycle()

    if automation_state_manager.is_aborting():
        logger.info("🛑 Automation abort detected after self-healing. Halting tick.")
        return

    if current_state == "paused":
        logger.info("Automation is PAUSED. Skipping Video search automation.")
        return

    is_upgrades_only = current_state == "upgrades_only"

    # 2. Video Media Automation (Runs in ACTIVE and UPGRADES_ONLY states)
    if is_interval_due(settings.video_search_interval, now):
        logger.info(
            "⏰ Running scheduled Video Automation (Interval: %dm, Upgrades Only: %s)",
            settings.video_search_interval,
            is_upgrades_only,
        )
        await run_automation_cycle(upgrades_only=is_upgrades_only)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifecycle manager."""
    # 1. Initialize Database
    await init_db(settings.database_url)

    # 1.5 Load settings from DB into memory
    async with async_session_factory() as session:
        await reload_settings_from_db(session)

    # 2. Setup and Start APScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        run_orchestrator_tick,
        CronTrigger(minute="0,15,30,45"),
        id="orchestrator_job",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "APScheduler started with quarter-hour cron trigger (:00, :15, :30, :45)."
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
    description="Autonomous Media Processing System",
    version=APP_VERSION,
    lifespan=lifespan,
)

# Mount static files
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ---------------------------------------------------------------------------
# Routes (HTML & HTMX)
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, background_tasks: BackgroundTasks):
    """Main dashboard displaying the watchlist."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.core.automation import (
        consolidate_standalone_anime_sequels,
        should_trigger_dashboard_simkl_sync,
        sync_all_providers,
    )
    from app.db.database import async_session_factory
    from app.db.models import MediaItem, MediaType

    async with async_session_factory() as session:
        if await should_trigger_dashboard_simkl_sync(session):
            background_tasks.add_task(sync_all_providers)

        # Auto-consolidate any legacy standalone sequels before rendering
        await consolidate_standalone_anime_sequels(session)

        # Fetch items
        stmt = (
            select(MediaItem)
            .order_by(MediaItem.created_at.desc())
            .options(
                selectinload(MediaItem.seasons),
                selectinload(MediaItem.failure_logs),
                selectinload(MediaItem.download_history),
                selectinload(MediaItem.provider),
            )
        )
        result = await session.execute(stmt)
        all_items = result.scalars().all()
        items = [i for i in all_items if not i.is_fully_completed]

        from app.core.automation import classify_video_item_partition

        for item in items:
            item.partition = classify_video_item_partition(item)

        missing_items = [i for i in items if i.partition == "missing"]
        upgrading_items = [i for i in items if i.partition == "upgrading"]

        items_payload = [
            {
                "category": "anime"
                if (
                    i.media_type.value == "movie"
                    and getattr(i, "is_anime_movie", False)
                )
                else i.media_type.value,
                "partition": getattr(i, "partition", "missing"),
                "title": (i.title or "").lower(),
                "alt_title": (i.alt_title or "").lower(),
                "ids": f"{i.tvdb_id or ''} {i.tmdb_id or ''} {i.imdb_id or ''} {i.simkl_id or ''}".lower(),
            }
            for i in items
        ]

        # Detailed stats calculations
        movie_items = [
            i
            for i in items
            if i.media_type == MediaType.MOVIE
            and not getattr(i, "is_anime_movie", False)
        ]
        series_items = [i for i in items if i.media_type == MediaType.SHOW]
        anime_items = [
            i
            for i in items
            if i.media_type == MediaType.ANIME or getattr(i, "is_anime_movie", False)
        ]

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
            "items_payload": items_payload,
            "missing_items": missing_items,
            "upgrading_items": upgrading_items,
            "movie_items": movie_items,
            "series_items": series_items,
            "anime_items": anime_items,
            "movie_stats": movie_stats,
            "series_stats": series_stats,
            "anime_stats": anime_stats,
            "movies_wanted": movie_stats["wanted"],
            "series_wanted": series_stats["wanted"],
            "anime_wanted": anime_stats["wanted"],
            "max_upgrade_attempts": settings.max_upgrade_attempts,
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
        db_settings = (await session.execute(stmt)).scalars().first()

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
    primary_language: str = Form("any"),
    fallback_language: str = Form("none"),
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
            db_settings = (await session.execute(stmt)).scalars().first()
            if db_settings:
                new_defaults = {
                    "category": category,
                    "language": language,
                    "primary_language": primary_language,
                    "fallback_language": fallback_language,
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

    primary_results = []
    fallback_results = []
    mismatched_results = []

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

    prim_lang = (
        primary_language
        if primary_language != "any"
        else (language if language != "any" else None)
    )
    fall_lang = (
        fallback_language if fallback_language not in ("none", "any", "") else None
    )

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

        sr = score_release(
            parsed,
            size_bytes=item.get("size", 0),
            age_days=0,
            primary_language=prim_lang,
            fallback_language=fall_lang,
            api_language=item.get("api_language"),
        )

        if sr.is_rejected:
            if sr.reject_reason and "Language mismatch" in sr.reject_reason:
                sr_mismatch = score_release(
                    parsed,
                    size_bytes=item.get("size", 0),
                    age_days=0,
                    primary_language=None,
                    fallback_language=None,
                    api_language=item.get("api_language"),
                )
                item["score"] = sr_mismatch.score
                item["parsed"] = parsed
                item["is_mismatch"] = True
                item["reject_reason"] = sr.reject_reason
                mismatched_results.append(item)
            continue

        item["score"] = sr.score
        item["parsed"] = parsed
        item["is_primary"] = sr.is_primary
        item["is_fallback"] = sr.is_fallback
        if sr.is_fallback:
            fallback_results.append(item)
        else:
            primary_results.append(item)

    primary_results.sort(key=lambda x: x["score"], reverse=True)
    fallback_results.sort(key=lambda x: x["score"], reverse=True)
    mismatched_results.sort(key=lambda x: x["score"], reverse=True)

    primary_results = primary_results[:limit]
    fallback_results = fallback_results[:limit]
    mismatched_results = mismatched_results[:limit]

    all_results = primary_results + fallback_results + mismatched_results

    if not all_results:
        return HTMLResponse(
            content='<div class="p-6 bg-[#2a2a32] border border-[#3f3f46] rounded-xl text-center text-[#a1a1aa] shadow-lg"><div class="text-4xl mb-4">🛸</div><h3 class="text-xl text-white font-semibold mb-2">No items found</h3><p>Try loosening your search filters.</p></div>'
        )

    return templates.TemplateResponse(
        request=request,
        name="partials/search_results.html",
        context={
            "results": all_results,
            "primary_results": primary_results,
            "fallback_results": fallback_results,
            "mismatched_results": mismatched_results,
        },
    )


@app.get("/items/{item_id}", response_class=HTMLResponse)
async def item_detail(request: Request, item_id: int):
    """Detailed view for a single item (shows seasons if it's a series)."""
    from sqlalchemy.orm import selectinload

    from app.db.database import async_session_factory
    from app.db.models import Episode, MediaItem, Season

    async with async_session_factory() as session:
        item = await session.get(
            MediaItem,
            item_id,
            options=[
                selectinload(MediaItem.seasons).selectinload(Season.episodes),
                selectinload(MediaItem.seasons).selectinload(Season.failure_logs),
                selectinload(MediaItem.seasons).selectinload(Season.download_history),
                selectinload(MediaItem.seasons)
                .selectinload(Season.episodes)
                .selectinload(Episode.failure_logs),
                selectinload(MediaItem.seasons)
                .selectinload(Season.episodes)
                .selectinload(Episode.download_history),
                selectinload(MediaItem.download_history),
                selectinload(MediaItem.failure_logs),
            ],
        )

    if not item:
        return HTMLResponse(content="Item not found", status_code=404)

    return templates.TemplateResponse(
        request=request,
        name="item_detail.html",
        context={"item": item, "max_upgrade_attempts": settings.max_upgrade_attempts},
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
        season = result.scalars().first()

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


@app.post("/items/{item_id}/seasons/{season_number}/search")
async def manual_search_season_route(item_id: int, season_number: int):
    """Manually search and download a whole season synchronously, bypassing episode batch limits."""
    from app.core.automation import manual_search_season
    from app.core.search_lock import create_conflict_response, item_lock_manager
    from app.db.database import async_session_factory

    if not item_lock_manager.try_acquire(item_id):
        owner = item_lock_manager.get_lock_owner(item_id)
        msg = (
            "Background automation is currently searching this show. Please wait."
            if owner == "background"
            else "A search is already in progress for this show. Please wait for it to complete."
        )
        return create_conflict_response(msg)

    try:
        async with async_session_factory() as session:
            await manual_search_season(session, item_id, season_number)
            return HTMLResponse(content="<script>window.location.reload();</script>")
    finally:
        item_lock_manager.release(item_id)


@app.post("/episodes/{episode_id}/search")
async def manual_search_episode_route(episode_id: int):
    """Manually search and download a single episode synchronously."""
    from sqlalchemy import select

    from app.core.automation import manual_search_episode
    from app.core.search_lock import create_conflict_response, item_lock_manager
    from app.db.database import async_session_factory
    from app.db.models import Episode, Season

    async with async_session_factory() as session:
        stmt = (
            select(Season.media_item_id)
            .join(Episode, Episode.season_id == Season.id)
            .where(Episode.id == episode_id)
        )
        media_item_id = (await session.execute(stmt)).scalar_one_or_none()

    if media_item_id is not None:
        if not item_lock_manager.try_acquire(media_item_id):
            owner = item_lock_manager.get_lock_owner(media_item_id)
            msg = (
                "Background automation is currently searching this show. Please wait."
                if owner == "background"
                else "A search is already in progress for this show. Please wait for it to complete."
            )
            return create_conflict_response(msg)
        try:
            async with async_session_factory() as session:
                await manual_search_episode(session, episode_id)
                return HTMLResponse(
                    content="<script>window.location.reload();</script>"
                )
        finally:
            item_lock_manager.release(media_item_id)
    else:
        async with async_session_factory() as session:
            await manual_search_episode(session, episode_id)
            # We reload the page in both cases so the user sees the updated status (completed/downloaded or failed + fail_count)
            return HTMLResponse(content="<script>window.location.reload();</script>")


@app.post("/items/{item_id}/search")
async def manual_search_movie_route(item_id: int):
    """Manually search and download a single movie synchronously."""
    from app.core.automation import manual_search_movie
    from app.core.search_lock import create_conflict_response, item_lock_manager
    from app.db.database import async_session_factory

    if not item_lock_manager.try_acquire(item_id):
        owner = item_lock_manager.get_lock_owner(item_id)
        msg = (
            "Background automation is currently searching this movie. Please wait."
            if owner == "background"
            else "A search is already in progress for this movie. Please wait for it to complete."
        )
        return create_conflict_response(msg)

    try:
        async with async_session_factory() as session:
            await manual_search_movie(session, item_id)
            return HTMLResponse(content="<script>window.location.reload();</script>")
    finally:
        item_lock_manager.release(item_id)


@app.post("/items/{item_id}/refresh_metadata")
async def refresh_item_metadata_route(item_id: int):
    """Force an immediate metadata refresh for a specific show/anime."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.core.automation import _refresh_series_metadata
    from app.db.database import async_session_factory
    from app.db.models import MediaItem, MediaType, Season

    async with async_session_factory() as session:
        stmt = (
            select(MediaItem)
            .where(MediaItem.id == item_id)
            .options(
                selectinload(MediaItem.seasons).selectinload(Season.episodes),
                selectinload(MediaItem.download_history),
                selectinload(MediaItem.failure_logs),
            )
        )
        item = (await session.execute(stmt)).scalars().first()
        if not item or item.media_type not in (MediaType.SHOW, MediaType.ANIME):
            return HTMLResponse(content="Invalid Item", status_code=400)

        item.last_metadata_refreshed_at = None
        await _refresh_series_metadata(session, item)
        await session.commit()

        return HTMLResponse(content="<script>window.location.reload();</script>")


@app.post("/items/{item_id}/reset-metadata")
async def reset_anime_metadata_endpoint(item_id: int):
    """Rebuilds anime metadata cleanly from AniList, purges phantom episodes, and resets backoff counters."""
    from app.core.automation import reset_anime_metadata
    from app.db.database import async_session_factory
    from app.db.models import MediaItem, MediaType

    async with async_session_factory() as session:
        item = await session.get(MediaItem, item_id)
        if not item or (
            item.media_type != MediaType.ANIME
            and not getattr(item, "is_anime_movie", False)
        ):
            return HTMLResponse(content="Invalid Item", status_code=400)

        await reset_anime_metadata(session, item_id)

    return HTMLResponse(
        content="<script>window.location.reload();</script>",
        headers={"HX-Refresh": "true"},
    )


@app.post("/items/{item_id}/retry")
async def retry_item(item_id: int):
    """Reset item and season status to pending and delete blacklisted releases for this item."""
    from datetime import datetime, timezone

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
            rd = item.release_date
            if rd and rd.tzinfo is None:
                rd = rd.replace(tzinfo=timezone.utc)
            if (rd and rd > datetime.now(timezone.utc)) or (
                not rd and item.year and item.year > datetime.now().year
            ):
                item.status = MediaStatus.FUTURE
            else:
                item.status = MediaStatus.SEARCHING
            item.fail_count = 0
            item.last_error = None

            # Reset seasons
            stmt = select(Season).where(Season.media_item_id == item_id)
            result = await session.execute(stmt)
            for season in result.scalars():
                if item.status == MediaStatus.FUTURE:
                    season.status = SeasonStatus.FUTURE
                else:
                    season.status = (
                        SeasonStatus.SEARCHING
                        if season.monitored
                        else SeasonStatus.PENDING
                    )
                season.fail_count = 0
                season.last_error = None

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
    from app.core.automation import increment_today_grab_count
    from app.core.fake_detector import is_nzb_content_fake
    from app.db.database import async_session_factory
    from app.db.models import (
        BlacklistedRelease,
        DownloadHistory,
        MediaItem,
        MediaStatus,
    )
    from app.services import torbox, treasure_maps

    async with async_session_factory() as session:
        item = await session.get(MediaItem, item_id)
        if not item or not item.pending_candidate_json:
            logger.warning(
                "confirm_grab_item called on item %s with no pending candidate", item_id
            )
            return HTMLResponse(
                content='<div class="text-red-500">No pending candidate found.</div>',
                status_code=400,
            )

        candidate = item.pending_candidate_json
        guid = candidate.get("guid", "")
        title = candidate.get("title", "")
        indexer_url = candidate.get("indexer_url")
        indexer_key = candidate.get("indexer_key")

        logger.info(
            "📥 [Manual Grab] Confirming movie grab for '%s' (%s) [guid=%s]",
            item.title,
            title,
            guid,
        )

        try:
            nzb_bytes, filename = await treasure_maps.fetch_nzb_bytes(
                guid,
                api_url=indexer_url,
                api_key=indexer_key,
                session=session,
            )
            is_fake, fake_reason = is_nzb_content_fake(nzb_bytes, media_type="movie")
            if is_fake:
                err_msg = f"NZB flagged as fake/executable: {fake_reason}"
                logger.warning(
                    "🚫 [Manual Grab] Rejected movie '%s': %s", title, err_msg
                )
                bl = BlacklistedRelease(
                    media_item_id=item.id,
                    nzb_guid=guid,
                    nzb_title=title,
                    reason=err_msg,
                )
                session.add(bl)
                item.status = MediaStatus.SEARCHING
                item.pending_candidate_json = None
                item.last_error = err_msg
                await session.commit()
                return HTMLResponse(
                    content=f'<div class="text-red-500">{err_msg}</div>',
                    status_code=400,
                    headers={"HX-Refresh": "true"},
                )

            torbox_result = await torbox.send_nzb_file(
                nzb_bytes, filename=filename, session=session, is_manual=True
            )
        except treasure_maps.IndexerError as e:
            logger.error(
                "❌ [Manual Grab] Indexer error fetching NZB for '%s': %s", title, e
            )
            return HTMLResponse(
                content=f'<div class="text-red-500">NZB Download Error: {e}</div>',
                status_code=500,
            )
        except Exception as e:
            logger.error(
                "❌ [Manual Grab] Unexpected error grabbing movie '%s': %s",
                title,
                e,
                exc_info=True,
            )
            return HTMLResponse(
                content=f'<div class="text-red-500">Error: {e}</div>',
                status_code=500,
            )

        if not torbox_result or (
            not torbox_result.get("hash") and not torbox_result.get("id")
        ):
            err_msg = str(
                torbox_result.get("error")
                if isinstance(torbox_result, dict) and torbox_result.get("error")
                else "Error sending to TorBox."
            )
            logger.error(
                "❌ [Manual Grab] TorBox dispatch failed for '%s': %s", title, err_msg
            )

            return HTMLResponse(
                content=f'<div class="text-red-500">{err_msg}</div>',
                status_code=500,
            )

        logger.info(
            "✅ [Manual Grab] Dispatched '%s' to TorBox successfully (id=%s, hash=%s)",
            title,
            torbox_result.get("id"),
            torbox_result.get("hash"),
        )
        await increment_today_grab_count(session)
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
        return HTMLResponse(
            content="<script>window.location.reload();</script>",
            headers={"HX-Refresh": "true"},
        )


@app.post("/items/{item_id}/decline_grab")
async def decline_grab_item(item_id: int):
    """Manually decline and blacklist the pending_candidate_json for a MANUAL_GRAB movie item."""
    from app.db.database import async_session_factory
    from app.db.models import BlacklistedRelease, MediaItem, MediaStatus

    async with async_session_factory() as session:
        item = await session.get(MediaItem, item_id)
        if not item or not item.pending_candidate_json:
            return HTMLResponse(
                content='<div class="text-red-500">No pending candidate found.</div>',
                status_code=400,
            )

        candidate = item.pending_candidate_json
        title = candidate.get("title", "")
        guid = candidate.get("guid")
        logger.info(
            "🚫 [Manual Grab] User declined candidate '%s' for item %s", title, item_id
        )
        bl = BlacklistedRelease(
            media_item_id=item.id,
            nzb_title=title,
            nzb_guid=guid,
            reason="Manually declined by user",
        )
        session.add(bl)
        item.status = MediaStatus.SEARCHING
        item.pending_candidate_json = None
        item.fail_count = 0
        item.last_error = None
        await session.commit()
        return HTMLResponse(
            content="<script>window.location.reload();</script>",
            headers={"HX-Refresh": "true"},
        )


@app.post("/items/{item_id}/seasons/{season_number}/confirm_grab")
async def confirm_grab_season(item_id: int, season_number: int):
    """Manually confirm and send season pack pending candidate to TorBox."""
    from sqlalchemy import select

    from app.core.automation import increment_today_grab_count
    from app.core.fake_detector import is_nzb_content_fake
    from app.db.database import async_session_factory
    from app.db.models import (
        BlacklistedRelease,
        DownloadHistory,
        Episode,
        EpisodeStatus,
        Season,
        SeasonStatus,
    )
    from app.services import torbox, treasure_maps

    async with async_session_factory() as session:
        stmt = select(Season).where(
            Season.media_item_id == item_id,
            Season.season_number == season_number,
        )
        season = (await session.execute(stmt)).scalars().first()
        if not season or not season.pending_candidate_json:
            logger.warning(
                "confirm_grab_season called on item %s S%s with no pending candidate",
                item_id,
                season_number,
            )
            return HTMLResponse(
                content='<div class="text-red-500">No pending candidate found.</div>',
                status_code=400,
            )

        candidate = season.pending_candidate_json
        guid = candidate.get("guid", "")
        title = candidate.get("title", "")
        indexer_url = candidate.get("indexer_url")
        indexer_key = candidate.get("indexer_key")

        logger.info(
            "📥 [Manual Grab] Confirming season pack grab for item %s S%s: '%s' [guid=%s]",
            item_id,
            season_number,
            title,
            guid,
        )

        try:
            nzb_bytes, filename = await treasure_maps.fetch_nzb_bytes(
                guid,
                api_url=indexer_url,
                api_key=indexer_key,
                session=session,
            )
            is_fake, fake_reason = is_nzb_content_fake(nzb_bytes, media_type="episode")
            if is_fake:
                err_msg = f"NZB flagged as fake/executable: {fake_reason}"
                logger.warning(
                    "🚫 [Manual Grab] Rejected season pack '%s': %s", title, err_msg
                )
                bl = BlacklistedRelease(
                    media_item_id=item_id,
                    nzb_guid=guid,
                    nzb_title=title,
                    reason=err_msg,
                )
                session.add(bl)
                season.status = (
                    SeasonStatus.SEARCHING if season.monitored else SeasonStatus.PENDING
                )
                season.pending_candidate_json = None
                season.last_error = err_msg
                await session.commit()
                return HTMLResponse(
                    content=f'<div class="text-red-500">{err_msg}</div>',
                    status_code=400,
                    headers={"HX-Refresh": "true"},
                )

            torbox_result = await torbox.send_nzb_file(
                nzb_bytes, filename=filename, session=session, is_manual=True
            )
        except treasure_maps.IndexerError as e:
            logger.error(
                "❌ [Manual Grab] Indexer error fetching NZB for season '%s': %s",
                title,
                e,
            )
            return HTMLResponse(
                content=f'<div class="text-red-500">NZB Download Error: {e}</div>',
                status_code=500,
            )
        except Exception as e:
            logger.error(
                "❌ [Manual Grab] Unexpected error grabbing season '%s': %s",
                title,
                e,
                exc_info=True,
            )
            return HTMLResponse(
                content=f'<div class="text-red-500">Error: {e}</div>',
                status_code=500,
            )

        if not torbox_result or (
            not torbox_result.get("hash") and not torbox_result.get("id")
        ):
            err_msg = str(
                torbox_result.get("error")
                if isinstance(torbox_result, dict) and torbox_result.get("error")
                else "Error sending to TorBox."
            )
            logger.error(
                "❌ [Manual Grab] TorBox dispatch failed for season '%s': %s",
                title,
                err_msg,
            )

            return HTMLResponse(
                content=f'<div class="text-red-500">{err_msg}</div>',
                status_code=500,
            )

        logger.info(
            "✅ [Manual Grab] Dispatched season '%s' to TorBox successfully (id=%s, hash=%s)",
            title,
            torbox_result.get("id"),
            torbox_result.get("hash"),
        )
        await increment_today_grab_count(session)
        history = DownloadHistory(
            media_item_id=item_id,
            season_id=season.id,
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
        season.status = SeasonStatus.DOWNLOADING
        season.pending_candidate_json = None
        season.fail_count = 0
        season.last_error = None

        # Update child episodes
        ep_stmt = select(Episode).where(Episode.season_id == season.id)
        episodes = (await session.execute(ep_stmt)).scalars().all()
        for ep in episodes:
            if ep.monitored or ep.status == EpisodeStatus.MANUAL_GRAB:
                ep.status = EpisodeStatus.DOWNLOADING
                ep.pending_candidate_json = None
                ep.fail_count = 0
                ep.last_error = None

        await session.commit()
        return HTMLResponse(
            content="<script>window.location.reload();</script>",
            headers={"HX-Refresh": "true"},
        )


@app.post("/items/{item_id}/seasons/{season_number}/decline_grab")
async def decline_grab_season(item_id: int, season_number: int):
    """Manually decline and blacklist season pack candidate."""
    from sqlalchemy import select

    from app.db.database import async_session_factory
    from app.db.models import (
        BlacklistedRelease,
        Episode,
        EpisodeStatus,
        Season,
        SeasonStatus,
    )

    async with async_session_factory() as session:
        stmt = select(Season).where(
            Season.media_item_id == item_id,
            Season.season_number == season_number,
        )
        season = (await session.execute(stmt)).scalars().first()
        if not season or not season.pending_candidate_json:
            return HTMLResponse(
                content='<div class="text-red-500">No pending candidate found.</div>',
                status_code=400,
            )

        candidate = season.pending_candidate_json
        title = candidate.get("title", "")
        guid = candidate.get("guid")
        logger.info(
            "🚫 [Manual Grab] User declined season candidate '%s' for item %s S%s",
            title,
            item_id,
            season_number,
        )
        bl = BlacklistedRelease(
            media_item_id=item_id,
            nzb_title=title,
            nzb_guid=guid,
            reason="Manually declined by user",
        )
        session.add(bl)
        season.status = (
            SeasonStatus.SEARCHING if season.monitored else SeasonStatus.PENDING
        )
        season.pending_candidate_json = None
        season.fail_count = 0
        season.last_error = None

        # Clear child episodes if they had this candidate
        ep_stmt = select(Episode).where(Episode.season_id == season.id)
        episodes = (await session.execute(ep_stmt)).scalars().all()
        for ep in episodes:
            if ep.status == EpisodeStatus.MANUAL_GRAB:
                ep.status = (
                    EpisodeStatus.SEARCHING if ep.monitored else EpisodeStatus.PENDING
                )
                ep.pending_candidate_json = None
                ep.fail_count = 0
                ep.last_error = None

        await session.commit()
        return HTMLResponse(
            content="<script>window.location.reload();</script>",
            headers={"HX-Refresh": "true"},
        )


@app.post("/episodes/{episode_id}/confirm_grab")
async def confirm_grab_episode(episode_id: int):
    """Manually confirm and send episode pending candidate to TorBox."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.core.automation import increment_today_grab_count
    from app.core.fake_detector import is_nzb_content_fake
    from app.db.database import async_session_factory
    from app.db.models import (
        BlacklistedRelease,
        DownloadHistory,
        Episode,
        EpisodeStatus,
    )
    from app.services import torbox, treasure_maps

    async with async_session_factory() as session:
        stmt = (
            select(Episode)
            .where(Episode.id == episode_id)
            .options(selectinload(Episode.season))
        )
        episode = (await session.execute(stmt)).scalars().first()
        if not episode or not episode.pending_candidate_json:
            logger.warning(
                "confirm_grab_episode called on episode %s with no pending candidate",
                episode_id,
            )
            return HTMLResponse(
                content='<div class="text-red-500">No pending candidate found.</div>',
                status_code=400,
            )

        candidate = episode.pending_candidate_json
        guid = candidate.get("guid", "")
        title = candidate.get("title", "")
        indexer_url = candidate.get("indexer_url")
        indexer_key = candidate.get("indexer_key")

        logger.info(
            "📥 [Manual Grab] Confirming episode grab for ep %s: '%s' [guid=%s]",
            episode_id,
            title,
            guid,
        )

        try:
            nzb_bytes, filename = await treasure_maps.fetch_nzb_bytes(
                guid,
                api_url=indexer_url,
                api_key=indexer_key,
                session=session,
            )
            is_fake, fake_reason = is_nzb_content_fake(nzb_bytes, media_type="episode")
            if is_fake:
                media_item_id = episode.season.media_item_id if episode.season else None
                err_msg = f"NZB flagged as fake/executable: {fake_reason}"
                logger.warning(
                    "🚫 [Manual Grab] Rejected episode '%s': %s", title, err_msg
                )
                bl = BlacklistedRelease(
                    media_item_id=media_item_id,
                    nzb_guid=guid,
                    nzb_title=title,
                    reason=err_msg,
                )
                session.add(bl)
                episode.status = (
                    EpisodeStatus.SEARCHING
                    if episode.monitored
                    else EpisodeStatus.PENDING
                )
                episode.pending_candidate_json = None
                episode.last_error = err_msg
                await session.commit()
                return HTMLResponse(
                    content=f'<div class="text-red-500">{err_msg}</div>',
                    status_code=400,
                    headers={"HX-Refresh": "true"},
                )

            torbox_result = await torbox.send_nzb_file(
                nzb_bytes, filename=filename, session=session, is_manual=True
            )
        except treasure_maps.IndexerError as e:
            logger.error(
                "❌ [Manual Grab] Indexer error fetching NZB for ep '%s': %s", title, e
            )
            return HTMLResponse(
                content=f'<div class="text-red-500">NZB Download Error: {e}</div>',
                status_code=500,
            )
        except Exception as e:
            logger.error(
                "❌ [Manual Grab] Unexpected error grabbing ep '%s': %s",
                title,
                e,
                exc_info=True,
            )
            return HTMLResponse(
                content=f'<div class="text-red-500">Error: {e}</div>',
                status_code=500,
            )

        if not torbox_result or (
            not torbox_result.get("hash") and not torbox_result.get("id")
        ):
            err_msg = str(
                torbox_result.get("error")
                if isinstance(torbox_result, dict) and torbox_result.get("error")
                else "Error sending to TorBox."
            )
            logger.error(
                "❌ [Manual Grab] TorBox dispatch failed for ep '%s': %s",
                title,
                err_msg,
            )

            return HTMLResponse(
                content=f'<div class="text-red-500">{err_msg}</div>',
                status_code=500,
            )

        logger.info(
            "✅ [Manual Grab] Dispatched ep '%s' to TorBox successfully (id=%s, hash=%s)",
            title,
            torbox_result.get("id"),
            torbox_result.get("hash"),
        )
        await increment_today_grab_count(session)
        media_item_id = episode.season.media_item_id if episode.season else None
        history = DownloadHistory(
            media_item_id=media_item_id,
            season_id=episode.season_id,
            episode_id=episode.id,
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
        episode.status = EpisodeStatus.DOWNLOADING
        episode.pending_candidate_json = None
        episode.fail_count = 0
        episode.last_error = None
        await session.commit()
        return HTMLResponse(
            content="<script>window.location.reload();</script>",
            headers={"HX-Refresh": "true"},
        )


@app.post("/episodes/{episode_id}/decline_grab")
async def decline_grab_episode(episode_id: int):
    """Manually decline and blacklist episode candidate."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.database import async_session_factory
    from app.db.models import BlacklistedRelease, Episode, EpisodeStatus

    async with async_session_factory() as session:
        stmt = (
            select(Episode)
            .where(Episode.id == episode_id)
            .options(selectinload(Episode.season))
        )
        episode = (await session.execute(stmt)).scalars().first()
        if not episode or not episode.pending_candidate_json:
            return HTMLResponse(
                content='<div class="text-red-500">No pending candidate found.</div>',
                status_code=400,
            )

        candidate = episode.pending_candidate_json
        title = candidate.get("title", "")
        guid = candidate.get("guid")
        logger.info(
            "🚫 [Manual Grab] User declined candidate '%s' for episode %s",
            title,
            episode_id,
        )
        media_item_id = episode.season.media_item_id if episode.season else None
        bl = BlacklistedRelease(
            media_item_id=media_item_id,
            nzb_title=title,
            nzb_guid=guid,
            reason="Manually declined by user",
        )
        session.add(bl)
        episode.status = (
            EpisodeStatus.SEARCHING if episode.monitored else EpisodeStatus.PENDING
        )
        episode.pending_candidate_json = None
        episode.fail_count = 0
        episode.last_error = None
        await session.commit()
        return HTMLResponse(
            content="<script>window.location.reload();</script>",
            headers={"HX-Refresh": "true"},
        )


@app.post("/api/torbox/add", response_class=HTMLResponse)
async def manual_push_to_torbox(magnet: str = Form(...)):
    """Push a search result directly to TorBox."""
    import logging

    from app.services import torbox, treasure_maps

    logger = logging.getLogger(__name__)

    try:
        if magnet.startswith("magnet:"):
            result = await torbox.send_magnet_link(magnet, is_manual=True)
        else:
            try:
                nzb_bytes, filename = await treasure_maps.fetch_nzb_bytes(magnet)
                result = await torbox.send_nzb_file(
                    nzb_bytes, filename=filename, is_manual=True
                )
            except treasure_maps.IndexerError as e:
                result = {"error": f"NZB Download Error: {e}"}

        if result and (result.get("hash") or result.get("id")):
            return HTMLResponse(
                content='<span class="text-emerald-400 font-medium text-xs px-2 py-1.5 bg-emerald-500/10 border border-emerald-500/20 rounded-md">Sent to TorBox!</span>'
            )
        else:
            err_msg = (
                result.get("error", "Failed to send") if result else "Failed to send"
            )
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


@app.post("/api/items/{item_id}/change-type")
async def change_type_endpoint(
    request: Request,
    item_id: int,
    background_tasks: BackgroundTasks,
    target_type: str = Form(...),
):

    from sqlalchemy.orm import selectinload

    from app.db.database import async_session_factory
    from app.db.models import MediaItem, MediaType, Season

    async with async_session_factory() as session:
        item = await session.get(
            MediaItem,
            item_id,
            options=[
                selectinload(MediaItem.seasons),
                selectinload(MediaItem.download_history),
            ],
        )
        if not item:
            return Response("Item not found", status_code=404)

        if target_type not in ("movie", "series", "anime", "anime-movie"):
            return Response("Invalid target_type", status_code=400)

        currently_is_series = item.media_type in (MediaType.SHOW, MediaType.ANIME)
        target_is_series = target_type in ("series", "anime")

        # 1. Structural Reshaping
        if not currently_is_series and target_is_series:
            season_1 = next((s for s in item.seasons if s.season_number == 1), None)
            if not season_1:
                season_1 = Season(
                    media_item_id=item.id,
                    season_number=1,
                    monitored=True,
                    status=item.status,
                )
                session.add(season_1)
                await session.flush()

            for dh in item.download_history:
                dh.season_id = season_1.id

        elif currently_is_series and not target_is_series:
            from sqlalchemy import delete, update

            from app.db.models import DownloadHistory, Season

            session.expunge(item)

            # Unlink DH
            await session.execute(
                update(DownloadHistory)
                .where(DownloadHistory.media_item_id == item.id)
                .values(season_id=None, episode_id=None)
            )

            await session.execute(delete(Season).where(Season.media_item_id == item.id))

            # Reload item
            item = await session.get(
                MediaItem, item_id, options=[selectinload(MediaItem.seasons)]
            )

        if item is not None:
            # 2. Field updates
            if target_type == "movie":
                item.media_type = MediaType.MOVIE
                item.is_anime_movie = False
            elif target_type == "anime-movie":
                item.media_type = MediaType.MOVIE
                item.is_anime_movie = True
            elif target_type == "series":
                item.media_type = MediaType.SHOW
                item.is_anime_movie = False
            elif target_type == "anime":
                item.media_type = MediaType.ANIME
                item.is_anime_movie = False

            # 3. Search Resets
            item.empty_search_count = 0
            item.last_searched_at = None
            if target_is_series:
                for s in item.seasons:
                    s.empty_search_count = 0
                    s.last_searched_at = None

            await session.commit()

        # 4. Dispatch background task
        if target_type in ("anime", "anime-movie") and background_tasks:

            async def _bg_enrich():
                from app.core.automation import enrich_anime_metadata
                from app.db.database import async_session_factory

                async with async_session_factory() as bg_session:
                    bg_item = await bg_session.get(MediaItem, item_id)
                    if bg_item and bg_item.anilist_id:
                        await enrich_anime_metadata(bg_session, bg_item)
                        await bg_session.commit()

            background_tasks.add_task(_bg_enrich)

    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)


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
        "upgrades_only": AutomationState.UPGRADES_ONLY,
        "paused": AutomationState.PAUSED,
        "disabled": AutomationState.DISABLED,
    }
    if state not in valid_states:
        return HTMLResponse(
            content='<div class="text-red-500">Invalid state</div>', status_code=400
        )

    target_enum = valid_states[state]
    if target_enum in (AutomationState.PAUSED, AutomationState.DISABLED):
        from app.core.automation_state import automation_state_manager

        automation_state_manager.request_abort()

    import asyncio

    from sqlalchemy.exc import OperationalError

    for attempt in range(5):
        try:
            async with async_session_factory() as session:
                stmt = select(SystemSettings).where(SystemSettings.id == 1)
                db_settings = (await session.execute(stmt)).scalars().first()
                if db_settings:
                    db_settings.automation_state = target_enum
                    await session.commit()
                    await reload_settings_from_db(session)
            break
        except OperationalError as e:
            if "database is locked" in str(e).lower() and attempt < 4:
                logger.warning(
                    "Database locked while updating automation state, retrying (attempt %d/5)...",
                    attempt + 1,
                )
                await asyncio.sleep(0.5 * (attempt + 1))
            else:
                raise

    return HTMLResponse(content="<script>window.location.reload();</script>")


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

    from app.db.models import (
        BlacklistedRelease,
        NotificationChannel,
        Provider,
        SystemSettings,
    )

    async with async_session_factory() as session:
        stmt = select(SystemSettings).where(SystemSettings.id == 1)
        db_settings = (await session.execute(stmt)).scalars().first()

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

        from app.services.preset_service import list_presets

        presets = await list_presets(session)

        blacklisted_releases = (
            (
                await session.execute(
                    select(BlacklistedRelease).order_by(
                        BlacklistedRelease.created_at.desc()
                    )
                )
            )
            .scalars()
            .all()
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
            "presets": presets,
            "notifications": notifications,
            "blacklisted_releases": blacklisted_releases,
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
        db_settings = (await session.execute(stmt)).scalars().first()
        settings_dict = {}
        if db_settings:
            settings_dict = {
                "tmdb_api_key": db_settings.tmdb_api_key,
                "treasure_maps_api_key": db_settings.treasure_maps_api_key,
                "torbox_api_key": db_settings.torbox_api_key,
                "scan_interval_multiplier": db_settings.scan_interval_multiplier,
                "self_healing_interval": getattr(
                    db_settings, "self_healing_interval", 15
                ),
                "video_search_interval": getattr(
                    db_settings, "video_search_interval", 60
                ),
                "sh_max_retries": db_settings.sh_max_retries,
                "sh_max_time_hours": db_settings.sh_max_time_hours,
                "sh_auto_retry": db_settings.sh_auto_retry,
                "sh_retry_wait_hours": db_settings.sh_retry_wait_hours,
                "download_timeout_hours": getattr(
                    db_settings, "download_timeout_hours", 24
                ),
                "discord_webhook_url": getattr(
                    db_settings, "discord_webhook_url", None
                ),
                "discord_enabled": getattr(db_settings, "discord_enabled", False),
                "notify_on_push_initiated": getattr(
                    db_settings, "notify_on_push_initiated", False
                ),
                "notify_on_completed": getattr(
                    db_settings, "notify_on_completed", True
                ),
                "notify_on_failure": getattr(db_settings, "notify_on_failure", True),
                "notify_on_auto_advance": getattr(
                    db_settings, "notify_on_auto_advance", True
                ),
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
                        "search_cycle_skip": getattr(prof, "search_cycle_skip", 1),
                        "resolution": prof.resolution,
                        "languages_csv": prof.languages_csv,
                        "primary_language": getattr(prof, "primary_language", None),
                        "fallback_language": getattr(prof, "fallback_language", None),
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
                    "config_json": getattr(p, "config_json", "{}"),
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


@app.post("/settings")
@app.post("/settings/global")
@app.post("/settings/system")
async def save_global_settings(
    request: Request,
    scan_interval_multiplier: int = Form(1),
    self_healing_interval: int = Form(15),
    video_search_interval: int = Form(60),
    upgrade_search_interval_hours: int = Form(24),
    download_timeout_hours: int = Form(24),
    sh_max_retries: int = Form(3),
    sh_max_time_hours: float = Form(12.0),
    sh_auto_retry: bool = Form(True),
    sh_retry_wait_hours: float = Form(24.0),
    max_upgrade_attempts: int = Form(7),
    dry_run: bool = Form(False),
    auto_grab_title_fallbacks: bool = Form(False),
    upgrade_threshold: int = Form(500),
    backoff_tier2_skip: int = Form(6),
    backoff_tier3_skip: int = Form(24),
):
    import copy

    from sqlalchemy import select

    from app.core.default_scoring import DEFAULT_SCORING_CONFIG
    from app.core.scheduler_utils import VALID_INTERVAL_PRESETS
    from app.db.models import SystemSettings

    form_data = await request.form()

    # Validate interval presets
    if self_healing_interval not in VALID_INTERVAL_PRESETS:
        self_healing_interval = 15
    if video_search_interval not in VALID_INTERVAL_PRESETS:
        video_search_interval = 60

    if download_timeout_hours < 1 or download_timeout_hours > 168:
        download_timeout_hours = 24

    async with async_session_factory() as session:
        stmt = select(SystemSettings).where(SystemSettings.id == 1)
        db_settings = (await session.execute(stmt)).scalars().first()

        if db_settings:
            db_settings.scan_interval_multiplier = scan_interval_multiplier
            db_settings.self_healing_interval = self_healing_interval
            db_settings.video_search_interval = video_search_interval
            db_settings.upgrade_search_interval_hours = upgrade_search_interval_hours
            db_settings.download_timeout_hours = download_timeout_hours
            db_settings.sh_max_retries = sh_max_retries
            db_settings.sh_max_time_hours = sh_max_time_hours
            db_settings.sh_auto_retry = sh_auto_retry
            db_settings.sh_retry_wait_hours = sh_retry_wait_hours
            db_settings.max_upgrade_attempts = max_upgrade_attempts
            db_settings.dry_run = dry_run
            db_settings.auto_grab_title_fallbacks = auto_grab_title_fallbacks
            db_settings.upgrade_threshold = upgrade_threshold
            db_settings.backoff_tier2_skip = backoff_tier2_skip
            db_settings.backoff_tier3_skip = backoff_tier3_skip

            if "discord_webhook_url" in form_data:
                raw_url = str(form_data.get("discord_webhook_url") or "").strip()
                db_settings.discord_webhook_url = raw_url or None
            if "discord_enabled" in form_data:
                db_settings.discord_enabled = str(
                    form_data.get("discord_enabled")
                ).lower() in ("1", "true", "on", "yes")
            if "notify_on_push_initiated" in form_data:
                db_settings.notify_on_push_initiated = str(
                    form_data.get("notify_on_push_initiated")
                ).lower() in ("1", "true", "on", "yes")
            if "notify_on_completed" in form_data:
                db_settings.notify_on_completed = str(
                    form_data.get("notify_on_completed")
                ).lower() in ("1", "true", "on", "yes")
            if "notify_on_failure" in form_data:
                db_settings.notify_on_failure = str(
                    form_data.get("notify_on_failure")
                ).lower() in ("1", "true", "on", "yes")
            if "notify_on_auto_advance" in form_data:
                db_settings.notify_on_auto_advance = str(
                    form_data.get("notify_on_auto_advance")
                ).lower() in ("1", "true", "on", "yes")

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
                    db_settings.upgrade_threshold = vupg

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
    api_key: str = Form(""),
    api_url: str = Form(""),
    priority: int = Form(1),
    is_active: bool = Form(False),
    category: str = Form(""),
    movie_category_id: int = Form(2000),
    series_category_id: int = Form(5000),
    anime_category_id: int = Form(5070),
    bandwidth_mbit: int = Form(None),
    # Profile settings
    enable_movies: bool = Form(False),
    movies_mode: str = Form(""),
    movies_resolution: str = Form("any"),
    movies_source: str = Form("any"),
    movies_video_codec: str = Form("any"),
    movies_hdr: str = Form("any"),
    movies_audio_tier: str = Form("any"),
    movies_audio_channels: str = Form("any"),
    movies_primary_lang: str = Form("any"),
    movies_fallback_lang: str = Form("none"),
    movies_langs: str = Form(""),
    movies_min_mb: int = Form(500),
    movies_max_mb: int = Form(25000),
    movies_reject: str = Form(""),
    enable_series: bool = Form(False),
    series_mode: str = Form(""),
    series_resolution: str = Form("any"),
    series_source: str = Form("any"),
    series_video_codec: str = Form("any"),
    series_hdr: str = Form("any"),
    series_audio_tier: str = Form("any"),
    series_audio_channels: str = Form("any"),
    series_primary_lang: str = Form("any"),
    series_fallback_lang: str = Form("none"),
    series_langs: str = Form(""),
    series_min_mb: int = Form(200),
    series_max_mb: int = Form(8000),
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
    anime_primary_lang: str = Form("any"),
    anime_fallback_lang: str = Form("none"),
    anime_langs: str = Form(""),
    anime_min_mb: int = Form(200),
    anime_max_mb: int = Form(8000),
    anime_reject: str = Form(""),
    anime_prefer_seasons: bool = Form(False),
    anime_block_size: int = Form(5),
    provider_type: str = Form("simkl"),
    sync_interval_minutes: int = Form(0),
    sync_movies: bool | None = Form(None),
    sync_series: bool | None = Form(None),
    sync_anime: bool | None = Form(None),
):
    from sqlalchemy.orm import selectinload

    from app.db.models import Provider, ProviderCategory, ProviderProfile
    from app.services import provider_service

    # Determine standard category from provider type if not explicitly supplied
    resolved_category = category
    if not resolved_category:
        type_cat_map = {
            "simkl": ProviderCategory.WATCHLIST.value,
            "tmdb": ProviderCategory.METADATA.value,
            "anilist": ProviderCategory.METADATA.value,
            "torbox": ProviderCategory.DOWNLOADER.value,
            "treasure_maps": ProviderCategory.INDEXER.value,
        }
        resolved_category = type_cat_map.get(
            provider_type.lower(), ProviderCategory.WATCHLIST.value
        )

    async with async_session_factory() as session:
        if provider_id:
            provider = await session.get(
                Provider, provider_id, options=[selectinload(Provider.profiles)]
            )
            if not provider:
                provider = Provider(type=provider_type, category=resolved_category)
                session.add(provider)
            else:
                provider.type = provider_type
                provider.category = resolved_category
        else:
            provider = Provider(type=provider_type, category=resolved_category)
            session.add(provider)

        provider.name = name
        provider.username = simkl_username
        provider.access_token = access_token
        provider.client_id = client_id
        provider.api_key = api_key
        provider.api_url = api_url
        provider.priority = priority
        provider.is_active = is_active
        provider.movie_category_id = movie_category_id
        provider.series_category_id = series_category_id
        provider.anime_category_id = anime_category_id
        provider.bandwidth_mbit = bandwidth_mbit

        if provider.type.lower() == "simkl":
            import json

            existing_cfg = provider.simkl_config
            existing_cfg["sync_interval_minutes"] = max(0, int(sync_interval_minutes))
            existing_cfg["sync_movies"] = (
                bool(sync_movies)
                if sync_movies is not None
                else bool(enable_movies or existing_cfg.get("sync_movies", True))
            )
            existing_cfg["sync_series"] = (
                bool(sync_series)
                if sync_series is not None
                else bool(enable_series or existing_cfg.get("sync_series", True))
            )
            existing_cfg["sync_anime"] = (
                bool(sync_anime)
                if sync_anime is not None
                else bool(enable_anime or existing_cfg.get("sync_anime", True))
            )
            provider.config_json = json.dumps(existing_cfg)

        await session.flush()  # get ID

        # If Downloader and active, enforce single-active downloader rule
        if (
            provider.category == ProviderCategory.DOWNLOADER.value
            and provider.is_active
        ):
            await provider_service.set_active_downloader(session, provider.id)

        # clear old profiles and recreate
        if provider_id:
            for p in list(provider.profiles):
                await session.delete(p)
            provider.profiles.clear()

        if enable_movies:
            m_prim = (
                movies_primary_lang
                if movies_primary_lang and movies_primary_lang not in ("any", "")
                else None
            )
            m_fall = (
                movies_fallback_lang
                if movies_fallback_lang and movies_fallback_lang not in ("none", "")
                else None
            )
            pm = ProviderProfile(
                provider_id=provider.id,
                media_type="movies",
                mode=movies_mode,
                resolution=movies_resolution,
                source=movies_source,
                video_codec=movies_video_codec,
                hdr=movies_hdr,
                audio_tier=movies_audio_tier,
                audio_channels=movies_audio_channels,
                primary_language=m_prim,
                fallback_language=m_fall,
                languages_csv=m_prim or movies_langs,
                min_mb=movies_min_mb if movies_min_mb is not None else 500,
                max_mb=movies_max_mb if movies_max_mb is not None else 25000,
                reject_words_csv=movies_reject,
                notification_channel_id=global_notification
                if global_notification
                else None,
            )
            session.add(pm)

        if enable_series:
            s_prim = (
                series_primary_lang
                if series_primary_lang and series_primary_lang not in ("any", "")
                else None
            )
            s_fall = (
                series_fallback_lang
                if series_fallback_lang and series_fallback_lang not in ("none", "")
                else None
            )
            ps = ProviderProfile(
                provider_id=provider.id,
                media_type="shows",
                mode=series_mode,
                resolution=series_resolution,
                source=series_source,
                video_codec=series_video_codec,
                hdr=series_hdr,
                audio_tier=series_audio_tier,
                audio_channels=series_audio_channels,
                primary_language=s_prim,
                fallback_language=s_fall,
                languages_csv=s_prim or series_langs,
                min_mb=series_min_mb if series_min_mb is not None else 200,
                max_mb=series_max_mb if series_max_mb is not None else 8000,
                reject_words_csv=series_reject,
                prefer_complete_seasons=series_prefer_seasons,
                episode_block_size=series_block_size,
                notification_channel_id=global_notification
                if global_notification
                else None,
            )
            session.add(ps)

        if enable_anime:
            a_prim = (
                anime_primary_lang
                if anime_primary_lang and anime_primary_lang not in ("any", "")
                else None
            )
            a_fall = (
                anime_fallback_lang
                if anime_fallback_lang and anime_fallback_lang not in ("none", "")
                else None
            )
            pa = ProviderProfile(
                provider_id=provider.id,
                media_type="anime",
                mode=anime_mode,
                resolution=anime_resolution,
                source=anime_source,
                video_codec=anime_video_codec,
                hdr=anime_hdr,
                audio_tier=anime_audio_tier,
                audio_channels=anime_audio_channels,
                primary_language=a_prim,
                fallback_language=a_fall,
                languages_csv=a_prim or anime_langs,
                min_mb=anime_min_mb if anime_min_mb is not None else 200,
                max_mb=anime_max_mb if anime_max_mb is not None else 8000,
                reject_words_csv=anime_reject,
                prefer_complete_seasons=anime_prefer_seasons,
                episode_block_size=anime_block_size,
                notification_channel_id=global_notification
                if global_notification
                else None,
            )
            session.add(pa)

        await session.commit()

    # Reload page preserving providers tab
    return HTMLResponse(
        content="<script>window.location.hash = 'providers'; window.location.reload();</script>"
    )


@app.post("/settings/provider/{provider_id}/toggle-active", response_class=HTMLResponse)
async def toggle_provider_active(provider_id: int) -> HTMLResponse:
    from app.db.models import Provider, ProviderCategory
    from app.services import provider_service

    async with async_session_factory() as session:
        provider = await session.get(Provider, provider_id)
        if provider:
            is_downloader = (
                provider.category == ProviderCategory.DOWNLOADER.value
                or provider.category == ProviderCategory.DOWNLOADER
            )
            if is_downloader:
                if not provider.is_active:
                    await provider_service.set_active_downloader(session, provider.id)
                else:
                    provider.is_active = False
                    await session.commit()
            else:
                provider.is_active = not provider.is_active
                await session.commit()

    return HTMLResponse(
        content="<script>window.location.hash = 'providers'; window.location.reload();</script>"
    )


@app.delete("/settings/provider/{provider_id}", response_class=HTMLResponse)
async def delete_provider(provider_id: int) -> HTMLResponse:
    from app.db.models import Provider

    async with async_session_factory() as session:
        provider = await session.get(Provider, provider_id)
        if provider:
            await session.delete(provider)
            await session.commit()
    return HTMLResponse(
        content="<script>window.location.hash = 'providers'; window.location.reload();</script>"
    )


@app.get("/settings/notification/new", response_class=HTMLResponse)
@app.get("/settings/notification/{notification_id}/edit", response_class=HTMLResponse)
async def notification_modal(
    request: Request, notification_id: int | None = None
) -> HTMLResponse:
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
) -> HTMLResponse:
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

    return HTMLResponse(
        content="<script>window.location.hash = 'notifications'; window.location.reload();</script>"
    )


@app.delete("/settings/notification/{notification_id}", response_class=HTMLResponse)
async def delete_notification(notification_id: int) -> HTMLResponse:
    from app.db.models import NotificationChannel

    async with async_session_factory() as session:
        notification = await session.get(NotificationChannel, notification_id)
        if notification:
            await session.delete(notification)
            await session.commit()
    return HTMLResponse(
        content="<script>window.location.hash = 'notifications'; window.location.reload();</script>"
    )


@app.post("/api/notifications/test/discord")
@app.post("/settings/notification/discord/test")
async def test_discord_notification(request: Request) -> Response:
    """Send a test rich Discord Embed to verify the configured or provided Discord Webhook URL."""
    from sqlalchemy import select

    from app.db.models import SystemSettings
    from app.services import discord

    payload = await _parse_request_payload(request)
    webhook_url = str(payload.get("discord_webhook_url") or "").strip()

    async with async_session_factory() as session:
        if not webhook_url:
            stmt = select(SystemSettings).where(SystemSettings.id == 1)
            db_settings = (await session.execute(stmt)).scalars().first()
            if db_settings and db_settings.discord_webhook_url:
                webhook_url = db_settings.discord_webhook_url.strip()
            elif settings.discord_webhook_url:
                webhook_url = settings.discord_webhook_url.strip()

    if not webhook_url:
        if request.headers.get("HX-Request") == "true":
            return HTMLResponse(
                content='<span class="text-xs text-red-400">Please provide a Discord Webhook URL first.</span>',
                status_code=400,
            )
        return JSONResponse(
            {"ok": False, "error": "Discord Webhook URL is not configured"},
            status_code=400,
        )

    embed = discord.build_discord_embed(
        event_type="completed",
        media_title="NZBoxer Test Notification",
        media_year=2026,
        target_label="Test Connection",
        release_name="NZBoxer.v3.0.0.2160p.WEB-DL.DDP5.1.Atmos.H.265",
        resolution="2160p",
        source="WEB-DL",
        video_codec="H.265",
        audio_codec="Atmos",
        language="en",
        status_reason="Discord Webhook integration verified!",
    )
    ok = await discord.send_discord_webhook(webhook_url, embed=embed)
    if request.headers.get("HX-Request") == "true":
        if ok:
            return HTMLResponse(
                content='<span class="text-xs text-emerald-400">Discord test notification sent!</span>'
            )
        return HTMLResponse(
            content='<span class="text-xs text-red-400">Discord webhook delivery failed.</span>',
            status_code=400,
        )
    return JSONResponse(
        {"ok": ok, "channel": "discord"},
        status_code=200 if ok else 400,
    )


@app.post("/api/notifications/test/telegram")
@app.post("/settings/notification/telegram/test")
@app.post("/settings/notification/{notification_id}/test")
async def test_telegram_notification(
    request: Request, notification_id: int | None = None
) -> Response:
    """Send a test Telegram message to verify a specific or configured Telegram channel."""
    from sqlalchemy import select

    from app.db.models import NotificationChannel
    from app.services import telegram

    payload = await _parse_request_payload(request)
    bot_token = str(payload.get("bot_token") or "").strip()
    chat_id = str(payload.get("chat_id") or "").strip()
    target_id = notification_id
    if target_id is None and payload.get("notification_id"):
        try:
            target_id = int(payload["notification_id"])
        except (TypeError, ValueError):
            target_id = None

    channels_to_test: list[tuple[str, str]] = []
    if bot_token and chat_id:
        channels_to_test.append((bot_token, chat_id))
    else:
        async with async_session_factory() as session:
            if target_id is not None:
                ch = await session.get(NotificationChannel, target_id)
                if ch and ch.bot_token and ch.chat_id:
                    channels_to_test.append((ch.bot_token, ch.chat_id))
            else:
                stmt = select(NotificationChannel).where(
                    NotificationChannel.type == "telegram"
                )
                rows = (await session.execute(stmt)).scalars().all()
                for ch in rows:
                    if ch.bot_token and ch.chat_id:
                        channels_to_test.append((ch.bot_token, ch.chat_id))

    if not channels_to_test:
        if request.headers.get("HX-Request") == "true":
            return HTMLResponse(
                content='<span class="text-xs text-red-400">No Telegram credentials configured.</span>',
                status_code=400,
            )
        return JSONResponse(
            {"ok": False, "error": "No Telegram channel configured"},
            status_code=400,
        )

    test_msg = (
        "<b>🔔 NZBoxer Test Notification</b>\nTelegram notification channel verified!"
    )
    sent_count = 0
    for token, cid in channels_to_test:
        if await telegram.send_notification(test_msg, token, cid):
            sent_count += 1

    ok = sent_count > 0
    if request.headers.get("HX-Request") == "true":
        if ok:
            return HTMLResponse(
                content='<span class="text-xs text-emerald-400">Telegram test notification sent!</span>'
            )
        return HTMLResponse(
            content='<span class="text-xs text-red-400">Telegram delivery failed.</span>',
            status_code=400,
        )
    return JSONResponse(
        {"ok": ok, "channel": "telegram", "sent_count": sent_count},
        status_code=200 if ok else 400,
    )


@app.post("/api/notifications/settings")
async def save_notification_settings(request: Request) -> Response:
    """Save Discord webhook URL, discord_enabled, and per-event trigger checkboxes on SystemSettings."""
    from sqlalchemy import select

    from app.config import reload_settings_from_db
    from app.db.models import SystemSettings

    payload = await _parse_request_payload(request)

    def _to_bool(val: Any) -> bool:
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "on", "yes")

    async with async_session_factory() as session:
        stmt = select(SystemSettings).where(SystemSettings.id == 1)
        db_settings = (await session.execute(stmt)).scalars().first()
        if not db_settings:
            db_settings = SystemSettings(id=1)
            session.add(db_settings)

        if "discord_webhook_url" in payload:
            raw_url = str(payload.get("discord_webhook_url") or "").strip()
            db_settings.discord_webhook_url = raw_url or None

        db_settings.discord_enabled = _to_bool(payload.get("discord_enabled", False))
        db_settings.notify_on_push_initiated = _to_bool(
            payload.get("notify_on_push_initiated", False)
        )
        db_settings.notify_on_completed = _to_bool(
            payload.get("notify_on_completed", False)
        )
        db_settings.notify_on_failure = _to_bool(
            payload.get("notify_on_failure", False)
        )
        db_settings.notify_on_auto_advance = _to_bool(
            payload.get("notify_on_auto_advance", False)
        )

        await session.commit()
        await reload_settings_from_db(session)

    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(
            content='<div class="p-3 bg-emerald-900/40 border border-emerald-500/40 text-emerald-200 rounded-lg text-xs">Notification settings saved!</div>'
        )
    return JSONResponse({"ok": True})


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

    from app.config import DEFAULT_USER_AGENT
    from app.db.models import NotificationChannel, Provider, SystemSettings

    results = {}
    async with async_session_factory() as session:
        db_settings = (
            (
                await session.execute(
                    select(SystemSettings).where(SystemSettings.id == 1)
                )
            )
            .scalars()
            .first()
        )
        providers = (await session.execute(select(Provider))).scalars().all()
        notifications = (
            (await session.execute(select(NotificationChannel))).scalars().all()
        )

        active_providers = {p.type: p for p in providers if p.is_active}
        all_providers = {p.type: p for p in providers}

        def get_prov(p_type: str) -> Provider | None:
            return active_providers.get(p_type) or all_providers.get(p_type)

        async with httpx.AsyncClient(
            timeout=10.0, headers={"User-Agent": DEFAULT_USER_AGENT}
        ) as client:
            # 1. TMDB
            tmdb_p = get_prov("tmdb")
            tmdb_key = (
                tmdb_p.api_key
                if tmdb_p and tmdb_p.api_key
                else (db_settings.tmdb_api_key if db_settings else "")
            ) or ""
            if tmdb_key:
                try:
                    headers = {}
                    params = {}
                    if tmdb_key.startswith("ey"):
                        headers["Authorization"] = f"Bearer {tmdb_key}"
                    else:
                        params["api_key"] = tmdb_key
                    r = await client.get(
                        "https://api.themoviedb.org/3/configuration",
                        params=params,
                        headers=headers,
                    )
                    results["tmdb"] = {
                        "ok": r.status_code == 200,
                        "msg": "Success"
                        if r.status_code == 200
                        else f"Error {r.status_code}",
                    }
                except Exception as e:
                    results["tmdb"] = {"ok": False, "msg": str(e)}
            else:
                results["tmdb"] = {"ok": False, "msg": "API key missing"}

            # 2. Treasure Maps
            tm_p = get_prov("treasure_maps")
            tm_key = (
                tm_p.api_key
                if tm_p and tm_p.api_key
                else (db_settings.treasure_maps_api_key if db_settings else "")
            ) or ""
            tm_url = (
                tm_p.api_url
                if tm_p and tm_p.api_url
                else "https://treasure-maps.com/api"
            ) or "https://treasure-maps.com/api"
            if tm_key:
                try:
                    r = await client.get(
                        tm_url,
                        params={
                            "t": "caps",
                            "apikey": tm_key,
                        },
                    )
                    results["treasure_maps"] = {
                        "ok": r.status_code == 200,
                        "msg": "Success"
                        if r.status_code == 200
                        else f"Error {r.status_code}",
                    }
                except Exception as e:
                    results["treasure_maps"] = {"ok": False, "msg": str(e)}
            else:
                results["treasure_maps"] = {"ok": False, "msg": "API key missing"}

            # 3. TorBox
            tb_p = get_prov("torbox")
            tb_key = (
                tb_p.api_key
                if tb_p and tb_p.api_key
                else (db_settings.torbox_api_key if db_settings else "")
            ) or ""
            tb_url = (
                tb_p.api_url
                if tb_p and tb_p.api_url
                else "https://api.torbox.app/v1/api"
            ) or "https://api.torbox.app/v1/api"
            if tb_key:
                try:
                    base_url = tb_url.rstrip("/")
                    endpoint = (
                        f"{base_url}/user/me"
                        if base_url.endswith("/api")
                        else f"{base_url}/api/user/me"
                    )
                    r = await client.get(
                        endpoint,
                        headers={"Authorization": f"Bearer {tb_key}"},
                    )
                    results["torbox"] = {
                        "ok": r.status_code == 200,
                        "msg": "Success"
                        if r.status_code == 200
                        else f"Error {r.status_code}",
                    }
                except Exception as e:
                    results["torbox"] = {"ok": False, "msg": str(e)}
            else:
                results["torbox"] = {"ok": False, "msg": "API key missing"}

            # 4. Simkl
            simkl_p = get_prov("simkl")
            if simkl_p and simkl_p.access_token and simkl_p.client_id:
                try:
                    r = await client.get(
                        "https://api.simkl.com/users/settings",
                        headers={
                            "Authorization": f"Bearer {simkl_p.access_token}",
                            "simkl-api-key": simkl_p.client_id,
                        },
                    )
                    results["simkl"] = {
                        "ok": r.status_code == 200,
                        "msg": "Success"
                        if r.status_code == 200
                        else f"Error {r.status_code}",
                    }
                except Exception as e:
                    results["simkl"] = {"ok": False, "msg": str(e)}
            else:
                results["simkl"] = {"ok": False, "msg": "No configured provider"}

            # 5. Telegram
            tg = next(
                (n for n in notifications if n.type == "telegram" and n.bot_token), None
            )
            if tg and tg.bot_token:
                try:
                    r = await client.get(
                        f"https://api.telegram.org/bot{tg.bot_token}/getMe"
                    )
                    results["telegram"] = {
                        "ok": r.status_code == 200,
                        "msg": "Success"
                        if r.status_code == 200
                        else f"Error {r.status_code}",
                    }
                except Exception as e:
                    results["telegram"] = {"ok": False, "msg": str(e)}
            else:
                results["telegram"] = {"ok": False, "msg": "No configured bot"}

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
    """Trigger manual sync for all configured providers and show flash message."""
    from app.core.automation import sync_all_providers

    background_tasks.add_task(sync_all_providers)
    msg = "Provider sync started in background."

    return HTMLResponse(
        content=f"""
        <div class="bg-[#3f3f46] text-white px-4 py-3 rounded-md shadow-lg border border-[#52525b] flex items-center justify-between animate-fade-in-down mb-4">
            <div class="flex items-center gap-3">
                <svg class="w-5 h-5 shrink-0 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                <span class="text-sm font-medium">{msg}</span>
            </div>
            <button onclick="this.parentElement.remove()" class="p-1 text-gray-400 hover:text-white hover:bg-black/20 rounded transition-colors focus:outline-none">
                <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
            </button>
        </div>
    """
    )


@app.post("/api/automation/rescan-torbox", response_class=HTMLResponse)
async def rescan_torbox_cache():
    """Manually invalidates TorBox cache and triggers an immediate full adoption pass."""
    import logging

    from app.core.self_healing import (
        adopt_torbox_downloads_for_video,
        sync_torbox_cache,
    )
    from app.db.database import async_session_factory
    from app.services import torbox

    logger = logging.getLogger(__name__)

    async with async_session_factory() as session:
        try:
            raw_downloads = await torbox.get_usenet_downloads(session=session)
            if raw_downloads is not None:
                await sync_torbox_cache(session, raw_downloads, force_rescan=True)
                await adopt_torbox_downloads_for_video(session)
                return HTMLResponse(
                    content='<span class="text-green-500 font-medium text-sm flex items-center gap-1.5"><svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg>Rescan complete! Reload to see changes.</span>'
                )
            else:
                return HTMLResponse(
                    content='<span class="text-red-500 font-medium text-sm">Error: Could not reach TorBox</span>',
                    status_code=500,
                )
        except Exception as e:
            logger.error("Error during manual TorBox rescan: %s", e)
            return HTMLResponse(
                content=f'<span class="text-red-500 font-medium text-sm">Error: {str(e)}</span>',
                status_code=500,
            )


@app.post("/api/automation/run/video", response_class=HTMLResponse)
async def run_video_automation_endpoint(background_tasks: BackgroundTasks):
    """Trigger manual video automation cycle."""
    from app.core.automation import run_automation_cycle
    from app.core.automation_state import AutomationStatus, automation_state_manager

    if (
        automation_state_manager.is_running()
        or not automation_state_manager.set_running(AutomationStatus.RUNNING_VIDEO)
    ):
        return HTMLResponse(
            content="""
            <div class="bg-amber-600 text-white px-4 py-3 rounded-md shadow-lg border border-amber-700 flex items-center justify-between animate-fade-in-down mb-4">
                <div class="flex items-center gap-3">
                    <svg class="w-5 h-5 shrink-0 text-amber-200" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
                    <span class="text-sm font-medium">An automation run is already active or aborting!</span>
                </div>
                <button onclick="this.parentElement.remove()" class="p-1 text-gray-200 hover:text-white hover:bg-black/20 rounded transition-colors focus:outline-none">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
                </button>
            </div>
            """
        )

    from app.config import settings

    current_state = (
        settings.automation_state.value
        if hasattr(settings.automation_state, "value")
        else str(settings.automation_state)
    ).lower()
    is_upgrades_only = current_state == "upgrades_only"
    background_tasks.add_task(
        run_automation_cycle, force=True, upgrades_only=is_upgrades_only
    )

    toast_msg = (
        "Video upgrades evaluation started in background!"
        if is_upgrades_only
        else "Video media search started in background!"
    )

    return HTMLResponse(
        content=f"""
        <div class="bg-[#d40060] text-white px-4 py-3 rounded-md shadow-lg border border-[#a3004a] flex items-center justify-between animate-fade-in-down mb-4">
            <div class="flex items-center gap-3">
                <svg class="w-5 h-5 shrink-0 text-pink-200" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
                <span class="text-sm font-medium">{toast_msg}</span>
            </div>
            <button onclick="this.parentElement.remove()" class="p-1 text-gray-300 hover:text-white hover:bg-black/20 rounded transition-colors focus:outline-none">
                <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
            </button>
        </div>
        """
    )


@app.post("/api/automation/abort", response_class=HTMLResponse)
async def abort_automation_endpoint():
    """Trigger graceful abort of active automation cycle."""
    from app.core.automation_state import automation_state_manager

    aborted = automation_state_manager.request_abort()
    if aborted:
        return HTMLResponse(
            content="""
            <div class="bg-amber-600 text-white px-4 py-3 rounded-md shadow-lg border border-amber-700 flex items-center justify-between animate-fade-in-down mb-4">
                <div class="flex items-center gap-3">
                    <svg class="w-5 h-5 shrink-0 animate-spin text-amber-200" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    <span class="text-sm font-medium">Search will gracefully abort after current item...</span>
                </div>
                <button onclick="this.parentElement.remove()" class="p-1 text-gray-200 hover:text-white hover:bg-black/20 rounded transition-colors focus:outline-none">
                    <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
                </button>
            </div>
            """
        )
    return HTMLResponse(
        content="""
        <div class="bg-slate-700 text-white px-4 py-3 rounded-md shadow-lg border border-slate-600 flex items-center justify-between animate-fade-in-down mb-4">
            <div class="flex items-center gap-3">
                <svg class="w-5 h-5 shrink-0 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                <span class="text-sm font-medium">No active search run found.</span>
            </div>
            <button onclick="this.parentElement.remove()" class="p-1 text-gray-300 hover:text-white hover:bg-black/20 rounded transition-colors focus:outline-none">
                <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
            </button>
        </div>
        """
    )


@app.get("/api/automation/status", response_class=HTMLResponse)
async def automation_status_endpoint(request: Request):
    """Return top-nav status badge partial for the current automation state."""
    from datetime import datetime

    from app.core.automation_state import automation_state_manager
    from app.core.scheduler_utils import get_next_scheduled_time

    state = automation_state_manager.get_state()
    global_state = (
        settings.automation_state.value
        if hasattr(settings.automation_state, "value")
        else str(settings.automation_state)
    ).lower()

    now = datetime.now()
    next_heal = get_next_scheduled_time(settings.self_healing_interval, now)
    next_video = get_next_scheduled_time(settings.video_search_interval, now)

    return templates.TemplateResponse(
        request=request,
        name="partials/automation_status.html",
        context={
            "state": state,
            "global_state": global_state,
            "next_heal": next_heal,
            "next_video": next_video,
        },
    )


@app.get("/history", response_class=HTMLResponse)
async def history_dashboard(request: Request):
    """Dashboard displaying fully completed media items (History Archive)."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.database import async_session_factory
    from app.db.models import (
        MediaItem,
        MediaType,
    )

    async with async_session_factory() as session:
        # Fetch Video Media
        video_stmt = (
            select(MediaItem)
            .order_by(MediaItem.updated_at.desc())
            .options(
                selectinload(MediaItem.seasons),
                selectinload(MediaItem.failure_logs),
                selectinload(MediaItem.download_history),
                selectinload(MediaItem.provider),
            )
        )
        video_items = (await session.execute(video_stmt)).scalars().all()
        history_video = [i for i in video_items if i.is_fully_completed]

        movie_items = [
            i
            for i in history_video
            if i.media_type == MediaType.MOVIE
            and not getattr(i, "is_anime_movie", False)
        ]
        series_items = [i for i in history_video if i.media_type == MediaType.SHOW]
        anime_items = [
            i
            for i in history_video
            if i.media_type == MediaType.ANIME or getattr(i, "is_anime_movie", False)
        ]

    return templates.TemplateResponse(
        request=request,
        name="history_dashboard.html",
        context={
            "movie_items": movie_items,
            "series_items": series_items,
            "anime_items": anime_items,
        },
    )


@app.get("/api/blacklist")
async def get_blacklist():
    """Retrieve all blacklisted releases."""
    from sqlalchemy import select

    from app.db.database import async_session_factory
    from app.db.models import BlacklistedRelease

    async with async_session_factory() as session:
        items = (
            (
                await session.execute(
                    select(BlacklistedRelease).order_by(
                        BlacklistedRelease.created_at.desc()
                    )
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": i.id,
                "media_item_id": i.media_item_id,
                "nzb_guid": i.nzb_guid,
                "nzb_title": i.nzb_title,
                "reason": i.reason,
                "created_at": i.created_at.isoformat(),
            }
            for i in items
        ]


@app.delete("/api/blacklist/{blacklist_id}")
async def delete_blacklisted_release(blacklist_id: int):
    """Delete a blacklisted release so it can be grabbed again."""
    from app.db.database import async_session_factory
    from app.db.models import BlacklistedRelease

    async with async_session_factory() as session:
        item = await session.get(BlacklistedRelease, blacklist_id)
        if item:
            await session.delete(item)
            await session.commit()

    # Return empty string for HTMX to clear the row
    return Response(content="", status_code=200)


# ---------------------------------------------------------------------------
# Search Presets & Sticky Item Config API (v3.0.0)
# ---------------------------------------------------------------------------


async def _parse_request_payload(request: Request) -> dict[str, Any]:
    """Parse either JSON body or Form data into a dictionary."""
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    try:
        form = await request.form()
        payload: dict[str, Any] = {}
        for k in form.keys():
            vals = form.getlist(k)
            if k.endswith("[]"):
                payload[k[:-2]] = vals
            elif len(vals) > 1:
                payload[k] = vals
            else:
                payload[k] = form.get(k)
        return payload
    except Exception:
        return {}


@app.get("/api/presets")
async def api_list_presets() -> list[dict[str, Any]]:
    """Return all saved SearchPreset rows ordered with the default preset first."""
    from app.db.database import async_session_factory
    from app.services.preset_service import list_presets

    async with async_session_factory() as session:
        presets = await list_presets(session)
        return [p.to_dict() for p in presets]


@app.get("/api/presets/{preset_id}")
async def api_get_preset(preset_id: int) -> Response:
    """Return a single SearchPreset by ID."""
    from fastapi.responses import JSONResponse

    from app.db.database import async_session_factory
    from app.services.preset_service import get_preset

    async with async_session_factory() as session:
        preset = await get_preset(session, preset_id)
        if preset is None:
            return JSONResponse(status_code=404, content={"error": "Preset not found"})
        return JSONResponse(status_code=200, content=preset.to_dict())


@app.post("/api/presets")
async def api_create_preset(request: Request) -> Response:
    """Create a new SearchPreset (or update if same name) and enforce single default."""
    from fastapi.responses import JSONResponse

    from app.db.database import async_session_factory
    from app.services.preset_service import create_preset

    payload = await _parse_request_payload(request)
    async with async_session_factory() as session:
        try:
            preset = await create_preset(session, payload)
            await session.commit()
            await session.refresh(preset)
            return JSONResponse(status_code=201, content=preset.to_dict())
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})


@app.post("/api/presets/save-inline")
async def api_save_inline_preset(request: Request) -> Response:
    """Save the current Push Modal configuration as a named SearchPreset without leaving the modal."""
    from fastapi.responses import JSONResponse

    from app.db.database import async_session_factory
    from app.services.preset_service import save_inline_preset

    payload = await _parse_request_payload(request)
    async with async_session_factory() as session:
        try:
            preset = await save_inline_preset(session, payload)
            await session.commit()
            await session.refresh(preset)
            return JSONResponse(status_code=201, content=preset.to_dict())
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})


@app.put("/api/presets/{preset_id}")
@app.post("/api/presets/{preset_id}")
async def api_update_preset(preset_id: int, request: Request) -> Response:
    """Update an existing SearchPreset by ID."""
    from fastapi.responses import JSONResponse

    from app.db.database import async_session_factory
    from app.services.preset_service import update_preset

    payload = await _parse_request_payload(request)
    async with async_session_factory() as session:
        preset = await update_preset(session, preset_id, payload)
        if preset is None:
            return JSONResponse(status_code=404, content={"error": "Preset not found"})
        await session.commit()
        await session.refresh(preset)
        return JSONResponse(status_code=200, content=preset.to_dict())


@app.post("/api/presets/{preset_id}/default")
@app.post("/api/presets/{preset_id}/set-default")
async def api_set_default_preset(preset_id: int) -> Response:
    """Mark a SearchPreset as the single default preset."""
    from fastapi.responses import JSONResponse

    from app.db.database import async_session_factory
    from app.services.preset_service import set_default_preset

    async with async_session_factory() as session:
        preset = await set_default_preset(session, preset_id)
        if preset is None:
            return JSONResponse(status_code=404, content={"error": "Preset not found"})
        await session.commit()
        await session.refresh(preset)
        return JSONResponse(status_code=200, content=preset.to_dict())


@app.delete("/api/presets/{preset_id}")
async def api_delete_preset(preset_id: int) -> Response:
    """Delete a SearchPreset and promote another preset to default if needed."""
    from fastapi.responses import JSONResponse

    from app.db.database import async_session_factory
    from app.services.preset_service import delete_preset

    async with async_session_factory() as session:
        deleted = await delete_preset(session, preset_id)
        if not deleted:
            return JSONResponse(status_code=404, content={"error": "Preset not found"})
        await session.commit()
        return JSONResponse(status_code=200, content={"deleted": True, "id": preset_id})


@app.post("/api/items/{item_id}/search-config")
@app.put("/api/items/{item_id}/search-config")
async def api_update_item_search_config(item_id: int, request: Request) -> Response:
    """Persist sticky search configuration on a MediaItem."""
    from fastapi.responses import JSONResponse

    from app.db.database import async_session_factory
    from app.services.preset_service import update_item_sticky_search_config

    payload = await _parse_request_payload(request)
    async with async_session_factory() as session:
        item = await update_item_sticky_search_config(session, item_id, payload)
        if item is None:
            return JSONResponse(
                status_code=404, content={"error": "MediaItem not found"}
            )
        await session.commit()
        await session.refresh(item)
        return JSONResponse(
            status_code=200,
            content={
                "id": item.id,
                "preset_id": item.preset_id,
                "custom_search_config_json": item.custom_search_config_json,
                "prefer_season_packs": item.prefer_season_packs,
                "auto_advance_seasons": item.auto_advance_seasons,
            },
        )


# ---------------------------------------------------------------------------
# Unified Push Modal & On-Demand Push Engine API (v3.0.0)
# ---------------------------------------------------------------------------


@app.get("/api/items/{item_id}/push-modal")
async def api_get_push_modal(item_id: int, request: Request) -> Response:
    """Return the Unified Push Modal context (JSON or HTML partial)."""
    from fastapi.responses import JSONResponse

    from app.core.push_engine import get_push_modal_context
    from app.db.database import async_session_factory

    async with async_session_factory() as session:
        ctx = await get_push_modal_context(session, item_id)
        if ctx is None:
            return JSONResponse(
                status_code=404, content={"error": "MediaItem not found"}
            )

        accept = (request.headers.get("accept") or "").lower()
        modal_tpl = Path("templates/modals/push_modal.html")
        if "application/json" not in accept and modal_tpl.exists():
            return templates.TemplateResponse(
                request=request,
                name="modals/push_modal.html",
                context=ctx,
            )

        item = ctx["item"]
        entries = ctx["entries"]
        presets = ctx["presets"]
        return JSONResponse(
            status_code=200,
            content={
                "item": {
                    "id": item.id,
                    "title": item.title,
                    "year": item.year,
                    "media_type": str(item.media_type),
                    "preset_id": item.preset_id,
                    "prefer_season_packs": item.prefer_season_packs,
                    "auto_advance_seasons": item.auto_advance_seasons,
                },
                "entries": [
                    {
                        "id": s.id,
                        "season_number": s.season_number,
                        "watch_order": s.watch_order,
                        "type_number": s.type_number,
                        "entry_type": s.entry_type,
                        "title": s.title,
                        "status": str(s.status),
                        "episodes": [
                            {
                                "id": ep.id,
                                "episode_number": ep.episode_number,
                                "status": str(ep.status),
                            }
                            for ep in sorted(s.episodes, key=lambda e: e.episode_number)
                        ],
                    }
                    for s in entries
                ],
                "presets": [p.to_dict() for p in presets],
                "effective_config": ctx["effective_config"],
            },
        )


@app.post("/api/items/{item_id}/push/auto")
async def api_push_item_auto(item_id: int, request: Request) -> Response:
    """Execute Auto-Push Best (`push_mode = 'auto'`) protected by MediaItemLockManager."""
    from fastapi.responses import JSONResponse

    from app.core.push_engine import execute_auto_push
    from app.core.search_lock import create_conflict_response, item_lock_manager
    from app.db.database import async_session_factory

    if not item_lock_manager.try_acquire(item_id, owner="manual"):
        owner = item_lock_manager.get_lock_owner(item_id)
        msg = (
            "Background automation is currently searching this item. Please wait."
            if owner == "background"
            else "A search or push is already in progress for this item. Please wait."
        )
        return create_conflict_response(msg)

    try:
        payload = await _parse_request_payload(request)
        async with async_session_factory() as session:
            result = await execute_auto_push(session, item_id, payload)
        status_code = int(result.pop("status_code", 200))
        return JSONResponse(status_code=status_code, content=result)
    finally:
        item_lock_manager.release(item_id)


@app.post("/api/items/{item_id}/push/manual-search")
async def api_push_item_manual_search(item_id: int, request: Request) -> Response:
    """Search indexers and return scored releases partitioned into Season Packs/Movies and Episode Accordions."""
    from fastapi.responses import JSONResponse

    from app.core.push_engine import execute_manual_search, persist_sticky_preferences
    from app.core.search_lock import create_conflict_response, item_lock_manager
    from app.db.database import async_session_factory
    from app.db.models import MediaItem

    if not item_lock_manager.try_acquire(item_id, owner="manual"):
        return create_conflict_response(
            "A search or push is already in progress for this item. Please wait."
        )

    try:
        payload = await _parse_request_payload(request)
        async with async_session_factory() as session:
            item = await session.get(MediaItem, item_id)
            if item is not None:
                await persist_sticky_preferences(session, item, payload)
                await session.commit()
            result = await execute_manual_search(session, item_id, payload)
        status_code = int(result.pop("status_code", 200))
        return JSONResponse(status_code=status_code, content=result)
    finally:
        item_lock_manager.release(item_id)


@app.post("/api/items/{item_id}/push/manual-grab")
async def api_push_item_manual_grab(item_id: int, request: Request) -> Response:
    """Dispatch a manually chosen release to TorBox with `DownloadHistory.push_mode = 'manual'`."""
    from fastapi.responses import JSONResponse

    from app.core.push_engine import execute_manual_grab
    from app.core.search_lock import create_conflict_response, item_lock_manager
    from app.db.database import async_session_factory

    if not item_lock_manager.try_acquire(item_id, owner="manual"):
        return create_conflict_response(
            "A search or push is already in progress for this item. Please wait."
        )

    try:
        payload = await _parse_request_payload(request)
        async with async_session_factory() as session:
            result = await execute_manual_grab(session, item_id, payload)
        status_code = int(result.pop("status_code", 200))
        return JSONResponse(status_code=status_code, content=result)
    finally:
        item_lock_manager.release(item_id)
