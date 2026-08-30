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
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request, Response
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
static_dir = _ROOT / "static"

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


APP_VERSION = "2.5.0"

templates.env.filters["relative_date"] = relative_date
templates.env.globals["settings"] = settings
templates.env.globals["app_version"] = APP_VERSION


async def run_orchestrator_tick(now: datetime | None = None) -> None:
    """Execute scheduled domain tasks aligned to wall-clock intervals.

    State rules:
    - DISABLED: Halts immediately (no self-healing, download checks, or searches).
    - PAUSED: Runs Self-Healing & Download Checks on schedule, but skips Video & Print searches.
    - ACTIVE: Runs all due domain tasks (Self-Healing/Download Checks, Video Search, Print Search).
    """
    from datetime import datetime

    from app.core.automation import run_automation_cycle, run_print_automation_cycle
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

    # 1. Self-Healing & Download Check (Runs in ACTIVE and PAUSED states)
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
        logger.info("Automation is PAUSED. Skipping Video and Print search automation.")
        return

    # 2. Video Media Automation (Runs strictly in ACTIVE state)
    if is_interval_due(settings.video_search_interval, now):
        logger.info(
            "⏰ Running scheduled Video Automation (Interval: %dm)",
            settings.video_search_interval,
        )
        await run_automation_cycle()

    if automation_state_manager.is_aborting():
        logger.info("🛑 Automation abort detected after video cycle. Halting tick.")
        return

    # 3. Print Media Automation (Runs strictly in ACTIVE state)
    if is_interval_due(settings.print_search_interval, now):
        logger.info(
            "⏰ Running scheduled Print Automation (Interval: %dm)",
            settings.print_search_interval,
        )
        await run_print_automation_cycle()


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
                selectinload(MediaItem.failure_logs),
                selectinload(MediaItem.download_history),
                selectinload(MediaItem.failure_logs),
                selectinload(MediaItem.provider),
            )
        )
        result = await session.execute(stmt)
        all_items = result.scalars().all()
        items = [i for i in all_items if not i.is_fully_completed]

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


@app.post("/api/search/print/manual", response_class=HTMLResponse)
async def manual_search_print(
    request: Request,
    media_type: str = Form("manga"),
    query: str = Form(""),
    volume: str = Form(""),
    author: str = Form(""),
    format_filter: str = Form("any"),
):
    """Dedicated manual search endpoint for Print Media releases."""
    from app.core.reading_scorer import (
        detect_print_format,
        match_volume_or_issue,
        score_print_release,
    )
    from app.services import treasure_maps

    if not query.strip():
        return HTMLResponse(
            content='<div class="p-8 text-center bg-[#2a2a32] border border-[#3f3f46] rounded-xl text-[#a1a1aa]">'
            '<div class="text-3xl mb-2">🔍</div><p>Please enter a title or search query.</p></div>'
        )

    base_title = query.strip()
    vol_clean = (
        volume.strip()
        .lower()
        .replace("volume", "")
        .replace("vol", "")
        .replace("v", "")
        .replace("#", "")
        .strip()
        if volume.strip()
        else ""
    )

    # Build search query fallback cascade
    search_queries = []
    if media_type == "manga" and vol_clean:
        if vol_clean.isdigit():
            vol_num = int(vol_clean)
            search_queries.append(f"{base_title} v{vol_num:02d}")
            search_queries.append(f"{base_title} {vol_num:02d}")
            search_queries.append(f"{base_title} vol {vol_num}")
            search_queries.append(f"{base_title} v{vol_num}")
            search_queries.append(base_title)
        else:
            search_queries.append(f"{base_title} {volume.strip()}")
            search_queries.append(base_title)
    elif media_type == "book" and author.strip():
        search_queries.append(f"{base_title} {author.strip()}")
        search_queries.append(base_title)
    else:
        search_queries.append(base_title)

    # Categories to query: primary + generic fallback
    cat_ids = (
        [7030, 7000]
        if media_type == "manga"
        else ([7010, 7000] if media_type == "magazine" else [7020, 7000])
    )

    PRINT_CATEGORY_NAMES: dict[int, str] = {
        7000: "EBooks (General)",
        7010: "Magazines",
        7020: "EBooks",
        7030: "Comics / Manga",
    }

    seen_guids: set[str] = set()
    raw_results = []

    for q in search_queries:
        for cat in cat_ids:
            items = await treasure_maps.search_raw(query=q, category=cat)
            for item in items:
                guid = item.get("guid") or item.get("title") or item.get("link")
                if guid and guid not in seen_guids:
                    seen_guids.add(guid)
                    raw_results.append(item)

    if not raw_results:
        return templates.TemplateResponse(
            request=request,
            name="partials/print_search_results.html",
            context={"results": []},
        )

    valid_results = []
    for item in raw_results:
        title = item.get("title", "")
        description = item.get("description", "")

        # Safely coerce category to int — indexers may return a human-readable
        # string like "Books > Comics" instead of a numeric ID.
        raw_cat = item.get("category", cat_ids[0])
        try:
            item_cat_id = int(raw_cat) if raw_cat is not None else cat_ids[0]
        except (ValueError, TypeError):
            item_cat_id = cat_ids[0]

        # Volume strict filtering if specified
        is_exact_vol = False
        if vol_clean:
            is_match, is_exact_vol = match_volume_or_issue(title, vol_clean)
            if not is_match:
                continue  # Skip releases that do not match the requested volume!

        detected_fmt = detect_print_format(
            title=title,
            description=description,
            category_id=item_cat_id,
            media_type=media_type,
        )

        # Apply format filter if specified
        if format_filter != "any" and detected_fmt != format_filter.lower():
            continue

        scoring_res = score_print_release(
            parsed_format=detected_fmt, media_type=media_type
        )
        score_val = scoring_res.get("score", 0)

        # Priority boost for exact volume single releases vs packs
        if vol_clean:
            score_val += 500 if is_exact_vol else 200

        valid_results.append(
            {
                "title": title,
                "link": item.get("link", ""),
                "size": item.get("size", 0),
                "pub_date": item.get("pub_date", ""),
                "format": detected_fmt,
                "score": score_val,
                "category_id": item_cat_id,
                "category_name": PRINT_CATEGORY_NAMES.get(
                    item_cat_id, f"Cat {item_cat_id}"
                ),
            }
        )

    valid_results.sort(key=lambda x: x["score"], reverse=True)

    return templates.TemplateResponse(
        request=request,
        name="partials/print_search_results.html",
        context={"results": valid_results},
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
                selectinload(MediaItem.seasons)
                .selectinload(Season.episodes)
                .selectinload(Episode.failure_logs),
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
    from app.db.database import async_session_factory
    from app.db.models import DownloadHistory, MediaItem, MediaStatus
    from app.services import torbox, treasure_maps

    async with async_session_factory() as session:
        item = await session.get(MediaItem, item_id)
        if not item or not item.pending_candidate_json:
            return HTMLResponse(
                content='<div class="text-red-500">No pending candidate found.</div>',
                status_code=400,
            )

        candidate = item.pending_candidate_json
        guid = candidate.get("guid", "")
        title = candidate.get("title", "")

        try:
            nzb_bytes, filename = await treasure_maps.fetch_nzb_bytes(guid)
            torbox_result = await torbox.send_nzb_file(nzb_bytes, filename=filename)
        except treasure_maps.IndexerError as e:
            return HTMLResponse(
                content=f'<div class="text-red-500">NZB Download Error: {e}</div>',
                status_code=500,
            )

        if not torbox_result or (
            not torbox_result.get("hash") and not torbox_result.get("id")
        ):
            err_msg = (
                torbox_result.get("error")
                if isinstance(torbox_result, dict) and torbox_result.get("error")
                else "Error sending to TorBox."
            )
            return HTMLResponse(
                content=f'<div class="text-red-500">{err_msg}</div>',
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
        if magnet.startswith("magnet:"):
            result = await torbox.send_magnet_link(magnet)
        else:
            try:
                nzb_bytes, filename = await treasure_maps.fetch_nzb_bytes(magnet)
                result = await torbox.send_nzb_file(nzb_bytes, filename=filename)
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
    item_id: int, target_type: str = Form(...), background_tasks: BackgroundTasks = None
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
            from sqlalchemy import update

            from app.db.models import DownloadHistory, Season

            session.expunge(item)

            # Unlink DH
            await session.execute(
                update(DownloadHistory)
                .where(DownloadHistory.media_item_id == item.id)
                .values(season_id=None, episode_id=None)
            )

            await session.execute(
                Season.__table__.delete().where(Season.media_item_id == item.id)
            )

            # Reload item
            item = await session.get(
                MediaItem, item_id, options=[selectinload(MediaItem.seasons)]
            )

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

    return HTMLResponse("<script>window.location.reload();</script>")


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
        db_settings = (await session.execute(stmt)).scalars().first()
        if db_settings:
            db_settings.automation_state = valid_states[state]
            await session.commit()
            await reload_settings_from_db(session)

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


@app.get("/dashboard/print", response_class=HTMLResponse)
async def print_dashboard(request: Request):
    """Main dashboard displaying Print Media (Manga, Books, Magazines)."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.database import async_session_factory
    from app.db.models import BookItem, MagazineSubscription, MangaItem, MediaStatus

    async with async_session_factory() as session:
        manga_stmt = (
            select(MangaItem)
            .options(selectinload(MangaItem.volumes))
            .order_by(MangaItem.title)
        )
        book_stmt = select(BookItem).order_by(BookItem.title)
        mag_stmt = (
            select(MagazineSubscription)
            .options(selectinload(MagazineSubscription.issues))
            .order_by(MagazineSubscription.title)
        )

        all_mangas = (await session.execute(manga_stmt)).scalars().all()
        mangas = [m for m in all_mangas if not m.is_fully_completed]
        all_books = (await session.execute(book_stmt)).scalars().all()
        books = [b for b in all_books if not b.is_fully_completed]
        all_mags = (await session.execute(mag_stmt)).scalars().all()
        magazines = [
            m
            for m in all_mags
            if m.status not in (MediaStatus.COMPLETED, MediaStatus.IGNORED)
        ]

        manga_stats = {
            "total": len(mangas),
            "wanted": sum(
                1
                for m in mangas
                if m.status in [MediaStatus.PENDING, MediaStatus.SEARCHING]
            ),
            "completed": sum(
                1
                for m in mangas
                if m.status in [MediaStatus.DOWNLOADED, MediaStatus.COMPLETED]
            ),
            "ignored": sum(1 for m in mangas if m.status == MediaStatus.IGNORED),
        }
        book_stats = {
            "total": len(books),
            "wanted": sum(
                1
                for b in books
                if b.status in [MediaStatus.PENDING, MediaStatus.SEARCHING]
            ),
            "completed": sum(
                1
                for b in books
                if b.status in [MediaStatus.DOWNLOADED, MediaStatus.COMPLETED]
            ),
            "ignored": sum(1 for b in books if b.status == MediaStatus.IGNORED),
        }
        magazine_stats = {
            "total": len(magazines),
            "wanted": sum(
                1
                for m in magazines
                if m.status in [MediaStatus.PENDING, MediaStatus.SEARCHING]
            ),
            "completed": sum(
                1
                for m in magazines
                if m.status in [MediaStatus.DOWNLOADED, MediaStatus.COMPLETED]
            ),
            "ignored": sum(1 for m in magazines if m.status == MediaStatus.IGNORED),
        }

    return templates.TemplateResponse(
        request=request,
        name="print_dashboard.html",
        context={
            "mangas": mangas,
            "books": books,
            "magazines": magazines,
            "manga_stats": manga_stats,
            "book_stats": book_stats,
            "magazine_stats": magazine_stats,
            "max_upgrade_attempts": settings.max_upgrade_attempts,
        },
    )


@app.get("/dashboard/print/manual-search", response_class=HTMLResponse)
async def print_manual_search_page(
    request: Request,
    query: str = "",
    media_type: str = "manga",
    format_type: str = "any",
    volume: str = "",
    volume_number: str = "",
    author: str = "",
):
    """Dedicated dashboard for manual searching print media (Manga, Books, Magazines)."""
    vol = volume or volume_number
    defaults = {
        "query": query,
        "media_type": media_type,
        "format_type": format_type,
        "volume": vol,
        "author": author,
    }
    return templates.TemplateResponse(
        request=request,
        name="print_manual_search.html",
        context={"defaults": defaults},
    )


@app.get("/print/add/modal", response_class=HTMLResponse)
async def get_add_print_media_modal(request: Request):
    """Render the modal to add a new print media item."""
    return templates.TemplateResponse(
        request=request,
        name="modals/add_print_media.html",
        context={},
    )


@app.post("/api/print/add")
async def add_print_media(
    request: Request,
    media_type: str = Form(...),
    title: str = Form(...),
    anilist_id: str = Form(""),
    start_year: str = Form(""),
    author: str = Form(""),
    isbn: str = Form(""),
    publisher: str = Form(""),
):
    """Add a new Manga, Book, or Magazine subscription to database."""
    from app.db.database import async_session_factory
    from app.db.models import (
        BookItem,
        EpisodeStatus,
        MagazineSubscription,
        MangaItem,
        MangaVolume,
        MediaStatus,
    )
    from app.services.anilist import get_manga_details, search_manga

    clean_title = title.strip()
    clean_anilist = anilist_id.strip() if anilist_id else ""

    async with async_session_factory() as session:
        if media_type == "manga":
            year_val = int(start_year) if start_year.isdigit() else None
            cover_img = None
            desc = None
            total_volumes = None

            # Attempt metadata fetch from AniList
            if clean_anilist and clean_anilist.isdigit():
                anilist_data = await get_manga_details(int(clean_anilist))
                if anilist_data:
                    cover_img = anilist_data.get("coverImage", {}).get("large")
                    desc = anilist_data.get("description")
                    total_volumes = anilist_data.get("volumes")
                    if not year_val and anilist_data.get("startDate", {}).get("year"):
                        year_val = anilist_data.get("startDate", {}).get("year")
            else:
                search_results = await search_manga(clean_title)
                if search_results:
                    top_match = search_results[0]
                    clean_anilist = str(top_match.get("id", ""))
                    cover_img = top_match.get("coverImage", {}).get("large")
                    desc = top_match.get("description")
                    total_volumes = top_match.get("volumes")
                    if not year_val and top_match.get("startDate", {}).get("year"):
                        year_val = top_match.get("startDate", {}).get("year")

            manga = MangaItem(
                title=clean_title,
                anilist_id=clean_anilist if clean_anilist else None,
                start_year=year_val,
                cover_image=cover_img,
                description=desc,
                status=MediaStatus.SEARCHING,
            )
            session.add(manga)
            await session.flush()

            # Initialize volume rows in unmonitored (IGNORED) state
            num_vols = total_volumes if (total_volumes and total_volumes > 0) else 1
            for v_num in range(1, num_vols + 1):
                vol = MangaVolume(
                    manga_id=manga.id,
                    volume_number=v_num,
                    status=EpisodeStatus.IGNORED,
                )
                session.add(vol)

        elif media_type == "book":
            book = BookItem(
                title=clean_title,
                author=author.strip() if author else None,
                isbn=isbn.strip() if isbn else None,
                status=MediaStatus.SEARCHING,
            )
            session.add(book)
            await session.flush()
            from app.core.self_healing import match_and_adopt_target_from_cache

            await match_and_adopt_target_from_cache(session, book)
        elif media_type == "magazine":
            magazine = MagazineSubscription(
                title=clean_title,
                publisher=publisher.strip() if publisher else None,
                status=MediaStatus.PENDING,
            )
            session.add(magazine)

        await session.commit()

    return Response(headers={"HX-Redirect": "/dashboard/print"})


@app.get("/dashboard/print/manga/{manga_id}/volumes", response_class=HTMLResponse)
async def get_manga_volumes_partial(request: Request, manga_id: int):
    """Render the volume management drawer partial for a specific manga."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.database import async_session_factory
    from app.db.models import MangaItem

    async with async_session_factory() as session:
        stmt = (
            select(MangaItem)
            .options(selectinload(MangaItem.volumes))
            .where(MangaItem.id == manga_id)
        )
        manga = (await session.execute(stmt)).scalars().first()
        if not manga:
            raise HTTPException(status_code=404, detail="Manga not found")

    return templates.TemplateResponse(
        request=request,
        name="partials/manga_volumes.html",
        context={"manga": manga, "max_upgrade_attempts": settings.max_upgrade_attempts},
    )


@app.post("/api/print/manga/{manga_id}/monitor", response_class=HTMLResponse)
async def set_manga_monitoring(
    request: Request,
    manga_id: int,
    mode: str = "all",
    start_volume: int = Form(1),
):
    """Bulk configure monitoring status for manga volumes ('all', 'from_volume', 'none')."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.database import async_session_factory
    from app.db.models import EpisodeStatus, MangaItem

    # Support query parameter fallback if sent via GET/button query string
    query_mode = request.query_params.get("mode")
    if query_mode:
        mode = query_mode

    async with async_session_factory() as session:
        stmt = (
            select(MangaItem)
            .options(selectinload(MangaItem.volumes))
            .where(MangaItem.id == manga_id)
        )
        manga = (await session.execute(stmt)).scalars().first()
        if not manga:
            raise HTTPException(status_code=404, detail="Manga not found")

        for vol in manga.volumes:
            if mode == "all":
                vol.status = EpisodeStatus.SEARCHING
            elif mode == "none":
                vol.status = EpisodeStatus.IGNORED
            elif mode == "from_volume":
                if vol.volume_number >= start_volume:
                    vol.status = EpisodeStatus.SEARCHING
                else:
                    vol.status = EpisodeStatus.IGNORED

        await session.commit()
        await session.refresh(manga)

    return templates.TemplateResponse(
        request=request,
        name="partials/manga_volumes.html",
        context={"manga": manga, "max_upgrade_attempts": settings.max_upgrade_attempts},
    )


@app.post(
    "/api/print/manga/volume/{volume_id}/toggle-status", response_class=HTMLResponse
)
async def toggle_manga_volume_status(request: Request, volume_id: int):
    """Toggle an individual manga volume between SEARCHING (Wanted) and IGNORED."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.database import async_session_factory
    from app.db.models import EpisodeStatus, MangaItem, MangaVolume

    async with async_session_factory() as session:
        vol_stmt = select(MangaVolume).where(MangaVolume.id == volume_id)
        volume = (await session.execute(vol_stmt)).scalars().first()
        if not volume:
            raise HTTPException(status_code=404, detail="Volume not found")

        if volume.status in [EpisodeStatus.SEARCHING, EpisodeStatus.PENDING]:
            volume.status = EpisodeStatus.IGNORED
        else:
            volume.status = EpisodeStatus.SEARCHING

        manga_id = volume.manga_id
        await session.commit()
        manga_stmt = (
            select(MangaItem)
            .options(selectinload(MangaItem.volumes))
            .where(MangaItem.id == manga_id)
        )
        m = (await session.execute(manga_stmt)).scalars().first()
        if m:
            from app.core.self_healing import match_and_adopt_target_from_cache

            await match_and_adopt_target_from_cache(session, m)

        # Reload manga with all volumes
        manga_stmt = (
            select(MangaItem)
            .options(selectinload(MangaItem.volumes))
            .where(MangaItem.id == manga_id)
        )
        manga = (await session.execute(manga_stmt)).scalars().first()

    return templates.TemplateResponse(
        request=request,
        name="partials/manga_volumes.html",
        context={"manga": manga, "max_upgrade_attempts": settings.max_upgrade_attempts},
    )


@app.post("/api/print/manga/{manga_id}/add-volume", response_class=HTMLResponse)
async def add_manga_volume(request: Request, manga_id: int):
    """Add a new volume to the manga series."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.database import async_session_factory
    from app.db.models import EpisodeStatus, MangaItem, MangaVolume

    async with async_session_factory() as session:
        stmt = (
            select(MangaItem)
            .options(selectinload(MangaItem.volumes))
            .where(MangaItem.id == manga_id)
        )
        manga = (await session.execute(stmt)).scalars().first()
        if not manga:
            raise HTTPException(status_code=404, detail="Manga not found")

        max_vol = max((v.volume_number for v in manga.volumes), default=0)
        new_vol = MangaVolume(
            manga_id=manga.id,
            volume_number=max_vol + 1,
            status=EpisodeStatus.IGNORED,
        )
        session.add(new_vol)
        await session.commit()
        await session.refresh(manga)

    return templates.TemplateResponse(
        request=request,
        name="partials/manga_volumes.html",
        context={"manga": manga, "max_upgrade_attempts": settings.max_upgrade_attempts},
    )


@app.delete("/api/print/manga/{manga_id}")
async def delete_manga(manga_id: int):
    """Delete a tracked Manga series and all its volumes."""
    from sqlalchemy import delete

    from app.db.database import async_session_factory
    from app.db.models import MangaItem, MangaVolume

    async with async_session_factory() as session:
        vols_stmt = delete(MangaVolume).where(MangaVolume.manga_id == manga_id)
        await session.execute(vols_stmt)
        stmt = delete(MangaItem).where(MangaItem.id == manga_id)
        await session.execute(stmt)
        await session.commit()

    return Response(content="", status_code=200)


@app.post("/api/print/book/{book_id}/status")
async def update_book_status(book_id: int, status: str = Form(...)):
    """Update status of a book (e.g. searching, completed, ignored)."""
    from sqlalchemy import select

    from app.db.database import async_session_factory
    from app.db.models import BookItem, MediaStatus

    status_enum = MediaStatus(status.lower())

    async with async_session_factory() as session:
        stmt = select(BookItem).where(BookItem.id == book_id)
        book = (await session.execute(stmt)).scalars().first()
        if not book:
            raise HTTPException(status_code=404, detail="Book not found")

        book.status = status_enum
        await session.commit()

    return Response(headers={"HX-Redirect": "/dashboard/print"})


@app.delete("/api/print/book/{book_id}")
async def delete_book(book_id: int):
    """Delete a tracked book."""
    from sqlalchemy import delete

    from app.db.database import async_session_factory
    from app.db.models import BookItem

    async with async_session_factory() as session:
        stmt = delete(BookItem).where(BookItem.id == book_id)
        await session.execute(stmt)
        await session.commit()

    return Response(content="", status_code=200)


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
                "print_search_interval": getattr(
                    db_settings, "print_search_interval", 60
                ),
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
                        "search_cycle_skip": getattr(prof, "search_cycle_skip", 1),
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
    scan_interval_multiplier: int = Form(1),
    self_healing_interval: int = Form(15),
    video_search_interval: int = Form(60),
    print_search_interval: int = Form(60),
    upgrade_search_interval_hours: int = Form(24),
    sh_max_retries: int = Form(3),
    sh_max_time_hours: float = Form(12.0),
    sh_auto_retry: bool = Form(True),
    sh_retry_wait_hours: float = Form(24.0),
    max_upgrade_attempts: int = Form(7),
    dry_run: bool = Form(False),
    upgrade_threshold: int = Form(500),
    backoff_tier2_skip: int = Form(6),
    backoff_tier3_skip: int = Form(24),
    reading_download_dir: str = Form("downloads"),
    manga_blacklisted_formats: str = Form(""),
    book_blacklisted_formats: str = Form(""),
    magazine_blacklisted_formats: str = Form(""),
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
    if print_search_interval not in VALID_INTERVAL_PRESETS:
        print_search_interval = 60

    async with async_session_factory() as session:
        stmt = select(SystemSettings).where(SystemSettings.id == 1)
        db_settings = (await session.execute(stmt)).scalars().first()

        if db_settings:
            db_settings.scan_interval_multiplier = scan_interval_multiplier
            db_settings.self_healing_interval = self_healing_interval
            db_settings.video_search_interval = video_search_interval
            db_settings.print_search_interval = print_search_interval
            db_settings.upgrade_search_interval_hours = upgrade_search_interval_hours
            db_settings.sh_max_retries = sh_max_retries
            db_settings.sh_max_time_hours = sh_max_time_hours
            db_settings.sh_auto_retry = sh_auto_retry
            db_settings.sh_retry_wait_hours = sh_retry_wait_hours
            db_settings.max_upgrade_attempts = max_upgrade_attempts
            db_settings.dry_run = dry_run
            db_settings.upgrade_threshold = upgrade_threshold
            db_settings.backoff_tier2_skip = backoff_tier2_skip
            db_settings.backoff_tier3_skip = backoff_tier3_skip
            db_settings.reading_download_dir = reading_download_dir
            db_settings.manga_blacklisted_formats = manga_blacklisted_formats
            db_settings.book_blacklisted_formats = book_blacklisted_formats
            db_settings.magazine_blacklisted_formats = magazine_blacklisted_formats

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
    enable_books: bool = Form(False),
    enable_manga: bool = Form(False),
    movies_mode: str = Form(""),
    movies_resolution: str = Form("any"),
    movies_source: str = Form("any"),
    movies_video_codec: str = Form("any"),
    movies_hdr: str = Form("any"),
    movies_audio_tier: str = Form("any"),
    movies_audio_channels: str = Form("any"),
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
    anime_langs: str = Form(""),
    anime_min_mb: int = Form(200),
    anime_max_mb: int = Form(8000),
    anime_reject: str = Form(""),
    anime_prefer_seasons: bool = Form(False),
    anime_block_size: int = Form(5),
    provider_type: str = Form("simkl"),
):
    from sqlalchemy.orm import selectinload

    from app.db.models import Provider, ProviderCategory, ProviderProfile
    from app.services import provider_service

    # Determine standard category from provider type if not explicitly supplied
    resolved_category = category
    if not resolved_category:
        type_cat_map = {
            "simkl": ProviderCategory.WATCHLIST.value,
            "hardcover": ProviderCategory.PRINT_MEDIA.value,
            "openlibrary": ProviderCategory.PRINT_MEDIA.value,
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

        if enable_movies or (enable_books and provider_type == "hardcover"):
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
                languages_csv=movies_langs,
                min_mb=movies_min_mb if movies_min_mb is not None else 500,
                max_mb=movies_max_mb if movies_max_mb is not None else 25000,
                reject_words_csv=movies_reject,
                notification_channel_id=global_notification
                if global_notification
                else None,
            )
            session.add(pm)

        if enable_series or (enable_manga and provider_type == "anilist"):
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
                languages_csv=series_langs,
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
                languages_csv=anime_langs,
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
                <svg class="w-5 h-5 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                <span class="text-sm font-medium">{msg}</span>
            </div>
            <button onclick="this.parentElement.remove()" class="text-gray-400 hover:text-white">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
            </button>
        </div>
    """
    )


@app.post("/api/automation/rescan-torbox", response_class=HTMLResponse)
async def rescan_torbox_cache():
    """Manually invalidates TorBox cache and triggers an immediate full adoption pass."""
    import logging

    from app.core.self_healing import (
        adopt_torbox_downloads_for_print,
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
                await adopt_torbox_downloads_for_print(session)
                return HTMLResponse(
                    content='<span class="text-green-500 font-medium text-sm flex items-center gap-1.5"><svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg>Rescan complete! Reload to see changes.</span>'
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

    if not automation_state_manager.set_running(AutomationStatus.RUNNING_VIDEO):
        return HTMLResponse(
            content="""
            <div class="bg-amber-600 text-white px-4 py-3 rounded-md shadow-lg border border-amber-700 flex items-center justify-between animate-fade-in-down mb-4">
                <div class="flex items-center gap-3">
                    <svg class="w-5 h-5 text-amber-200" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
                    <span class="text-sm font-medium">An automation run is already active or aborting!</span>
                </div>
                <button onclick="this.parentElement.remove()" class="text-gray-200 hover:text-white">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
                </button>
            </div>
            """
        )

    background_tasks.add_task(run_automation_cycle, force=True)

    return HTMLResponse(
        content="""
        <div class="bg-[#d40060] text-white px-4 py-3 rounded-md shadow-lg border border-[#a3004a] flex items-center justify-between animate-fade-in-down mb-4">
            <div class="flex items-center gap-3">
                <svg class="w-5 h-5 text-pink-200" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
                <span class="text-sm font-medium">Video media search started in background!</span>
            </div>
            <button onclick="this.parentElement.remove()" class="text-gray-300 hover:text-white">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
            </button>
        </div>
        """
    )


@app.post("/api/automation/run/print", response_class=HTMLResponse)
async def run_print_automation_endpoint(background_tasks: BackgroundTasks):
    """Trigger manual print automation cycle."""
    from app.core.automation import run_print_automation_cycle
    from app.core.automation_state import AutomationStatus, automation_state_manager

    if not automation_state_manager.set_running(AutomationStatus.RUNNING_PRINT):
        return HTMLResponse(
            content="""
            <div class="bg-amber-600 text-white px-4 py-3 rounded-md shadow-lg border border-amber-700 flex items-center justify-between animate-fade-in-down mb-4">
                <div class="flex items-center gap-3">
                    <svg class="w-5 h-5 text-amber-200" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
                    <span class="text-sm font-medium">An automation run is already active or aborting!</span>
                </div>
                <button onclick="this.parentElement.remove()" class="text-gray-200 hover:text-white">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
                </button>
            </div>
            """
        )

    background_tasks.add_task(run_print_automation_cycle, force=True)

    return HTMLResponse(
        content="""
        <div class="bg-[#d40060] text-white px-4 py-3 rounded-md shadow-lg border border-[#a3004a] flex items-center justify-between animate-fade-in-down mb-4">
            <div class="flex items-center gap-3">
                <svg class="w-5 h-5 text-pink-200" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
                <span class="text-sm font-medium">Print media search started in background!</span>
            </div>
            <button onclick="this.parentElement.remove()" class="text-gray-300 hover:text-white">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
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
                    <svg class="w-5 h-5 animate-spin text-amber-200" fill="none" viewBox="0 0 24 24">
                        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                    </svg>
                    <span class="text-sm font-medium">Search will gracefully abort after current item...</span>
                </div>
                <button onclick="this.parentElement.remove()" class="text-gray-200 hover:text-white">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
                </button>
            </div>
            """
        )
    return HTMLResponse(
        content="""
        <div class="bg-slate-700 text-white px-4 py-3 rounded-md shadow-lg border border-slate-600 flex items-center justify-between animate-fade-in-down mb-4">
            <div class="flex items-center gap-3">
                <svg class="w-5 h-5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                <span class="text-sm font-medium">No active search run found.</span>
            </div>
            <button onclick="this.parentElement.remove()" class="text-gray-300 hover:text-white">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
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
    next_print = get_next_scheduled_time(settings.print_search_interval, now)

    return templates.TemplateResponse(
        request=request,
        name="partials/automation_status.html",
        context={
            "state": state,
            "global_state": global_state,
            "next_heal": next_heal,
            "next_video": next_video,
            "next_print": next_print,
        },
    )


@app.get("/history", response_class=HTMLResponse)
async def history_dashboard(request: Request):
    """Dashboard displaying fully completed media items (History Archive)."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db.database import async_session_factory
    from app.db.models import (
        BookItem,
        MagazineSubscription,
        MangaItem,
        MediaItem,
        MediaStatus,
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

        # Fetch Print Media
        manga_stmt = (
            select(MangaItem)
            .options(selectinload(MangaItem.volumes))
            .order_by(MangaItem.title)
        )
        book_stmt = select(BookItem).order_by(BookItem.title)
        mag_stmt = (
            select(MagazineSubscription)
            .options(selectinload(MagazineSubscription.issues))
            .order_by(MagazineSubscription.title)
        )

        manga_items = [
            i
            for i in (await session.execute(manga_stmt)).scalars().all()
            if i.is_fully_completed
        ]
        book_items = [
            i
            for i in (await session.execute(book_stmt)).scalars().all()
            if i.is_fully_completed
        ]
        mag_items = [
            i
            for i in (await session.execute(mag_stmt)).scalars().all()
            if i.status in (MediaStatus.COMPLETED, MediaStatus.IGNORED)
        ]

    return templates.TemplateResponse(
        request=request,
        name="history_dashboard.html",
        context={
            "movie_items": movie_items,
            "series_items": series_items,
            "anime_items": anime_items,
            "manga_items": manga_items,
            "book_items": book_items,
            "mag_items": mag_items,
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
