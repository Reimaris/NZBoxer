"""
NZBoxer Main Application Entrypoint
====================================
FastAPI application with lifespan for database initialization and
APScheduler background jobs. Contains HTML routes for the dashboard
and API endpoints for HTMX interactions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import BackgroundTasks, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import reload_settings_from_db, settings
from app.core.time_utils import format_local_datetime
from app.db.database import async_session_factory, close_db, init_db
from app.version import APP_VERSION

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

# Mask API keys and other secrets on every handler (ADR-097) and forward
# ERROR+ records to Telegram/Discord (ADR-096)
from app.core.error_forwarder import ErrorForwarderHandler  # noqa: E402
from app.core.log_redaction import (  # noqa: E402
    install_model_listeners,
    install_redaction,
    refresh_secrets,
)

error_forwarder = ErrorForwarderHandler()
root_logger.addHandler(error_forwarder)
install_redaction([console_handler, file_handler, error_forwarder])
install_model_listeners()

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


templates.env.filters["relative_date"] = relative_date
templates.env.filters["local_datetime"] = format_local_datetime
templates.env.globals["settings"] = settings
templates.env.globals["app_version"] = APP_VERSION


async def run_orchestrator_tick(now: datetime | None = None) -> None:
    """Execute scheduled periodic Simkl watchlist sync and air date rollover aligned to wall-clock intervals."""
    from app.core.automation import (
        run_orchestrator_tick as core_run_orchestrator_tick,
    )

    await core_run_orchestrator_tick(now)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifecycle manager."""
    # 1. Initialize Database
    await init_db(settings.database_url)

    # 1.5 Load settings from DB into memory
    async with async_session_factory() as session:
        await reload_settings_from_db(session)
        await refresh_secrets(session)

    import asyncio as _asyncio

    error_forwarder.set_loop(_asyncio.get_running_loop())

    # 2. Setup and Start APScheduler
    import asyncio

    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    from app.core.transfer_poller import (
        count_downloading_entities,
        run_transfer_poller_tick,
        transfer_poller,
    )

    scheduler.add_job(
        run_orchestrator_tick,
        CronTrigger(minute="0,15,30,45"),
        id="orchestrator_job",
        replace_existing=True,
    )
    scheduler.add_job(
        run_transfer_poller_tick,
        IntervalTrigger(seconds=20),
        id="transfer_poller_job",
        replace_existing=True,
    )
    from app.core.rate_limit_queue import (
        RESUME_JOB_ID,
        count_rate_limited_pushes,
        rate_limit_queue,
        run_rate_limit_resume_tick,
    )

    scheduler.add_job(
        run_rate_limit_resume_tick,
        IntervalTrigger(seconds=15),
        id=RESUME_JOB_ID,
        replace_existing=True,
    )
    scheduler.start()
    transfer_poller.set_scheduler(scheduler)
    rate_limit_queue.set_scheduler(scheduler)

    # Rebuild the rate-limit queue state from the database (survives restarts)
    async with async_session_factory() as session:
        if await count_rate_limited_pushes(session) == 0:
            rate_limit_queue.sleep()
        else:
            from app.core.rate_limit_queue import rate_limit_episode

            rate_limit_episode.adopt_existing_queue()

    async with async_session_factory() as session:
        active_dl_count = await count_downloading_entities(session)
        if active_dl_count > 0:
            transfer_poller.wake()
            asyncio.create_task(run_transfer_poller_tick())
        else:
            transfer_poller.sleep(0)

    logger.info(
        "APScheduler started with quarter-hour cron trigger and 20s auto-wake transfer poller."
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
    """Main dashboard displaying the 6-Card Interactive Top Deck and 3-Tier Status Tables."""
    from sqlalchemy import func, select
    from sqlalchemy.orm import selectinload

    from app.core.automation import (
        classify_v3_status_tier,
        consolidate_standalone_anime_sequels,
        deduplicate_media_items,
        should_trigger_dashboard_simkl_sync,
        sync_all_providers,
    )
    from app.core.transfer_poller import (
        count_downloading_entities,
        get_active_pushes,
        transfer_poller,
    )
    from app.db.database import async_session_factory
    from app.db.models import (
        DownloadHistory,
        MediaItem,
        MediaType,
        Season,
        SystemSettings,
    )

    async with async_session_factory() as session:
        if await should_trigger_dashboard_simkl_sync(session):
            background_tasks.add_task(sync_all_providers)

        # Auto-consolidate any legacy standalone sequels and deduplicate items before rendering
        await consolidate_standalone_anime_sequels(session)
        if await deduplicate_media_items(session) > 0:
            await session.commit()

        # Fetch items
        stmt = (
            select(MediaItem)
            .order_by(MediaItem.created_at.desc())
            .options(
                selectinload(MediaItem.seasons).selectinload(Season.episodes),
                selectinload(MediaItem.seasons).selectinload(Season.download_history),
                selectinload(MediaItem.failure_logs),
                selectinload(MediaItem.download_history),
                selectinload(MediaItem.provider),
            )
        )
        from app.db.models import MediaStatus

        result = await session.execute(stmt)
        all_items = result.scalars().all()
        items = [i for i in all_items if i.status != MediaStatus.IGNORED]

        for item in items:
            item.status_tier = classify_v3_status_tier(item)

        in_progress_items = [i for i in items if i.status_tier == "in_progress"]
        ready_to_push_items = [i for i in items if i.status_tier == "ready_to_push"]
        upcoming_items = [i for i in items if i.status_tier == "upcoming"]

        items_payload = [
            {
                "category": "anime"
                if (
                    i.media_type.value == "movie"
                    and getattr(i, "is_anime_movie", False)
                )
                else i.media_type.value,
                "status_tier": getattr(i, "status_tier", "ready_to_push"),
                "title": (i.title or "").lower(),
                "alt_title": (i.alt_title or "").lower(),
                "ids": f"{i.tvdb_id or ''} {i.tmdb_id or ''} {i.imdb_id or ''} {i.simkl_id or ''} {i.anilist_id or ''}".lower(),
            }
            for i in items
        ]

        # Active category collections
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

        def _build_cat_stats(cat_items: list[MediaItem]) -> dict[str, int]:
            return {
                "total": len(cat_items),
                "in_progress": sum(
                    1 for i in cat_items if i.status_tier == "in_progress"
                ),
                "ready_to_push": sum(
                    1 for i in cat_items if i.status_tier == "ready_to_push"
                ),
                "upcoming": sum(1 for i in cat_items if i.status_tier == "upcoming"),
                "wanted": sum(
                    1 for i in cat_items if i.status in ("searching", "pending")
                ),
                "ignored": sum(
                    1 for i in cat_items if i.status in ("ignored", "canceled")
                ),
            }

        movie_stats = _build_cat_stats(movie_items)
        series_stats = _build_cat_stats(series_items)
        anime_stats = _build_cat_stats(anime_items)

        from app.core.transfer_poller import (
            autoclear_history_ledger,
            count_downloading_entities,
            get_active_pushes,
            get_push_history_ledger,
            purge_expired_dry_run_history,
            transfer_poller,
        )

        # Auto-purge expired dry-run history on page load
        await purge_expired_dry_run_history(session)
        await autoclear_history_ledger(session)

        # Card 4: Push History Ledger
        push_history_ledger = await get_push_history_ledger(session)
        dry_run_history_count = (
            await session.execute(
                select(func.count(DownloadHistory.id)).where(
                    DownloadHistory.is_dry_run.is_(True)
                )
            )
        ).scalar() or 0

        # Card 5: Active Pushes
        active_pushes = await get_active_pushes(session)
        downloading_count = await count_downloading_entities(session)
        in_flight_titles = transfer_poller.in_flight_titles
        poller_awake = (
            transfer_poller.is_awake or downloading_count > 0 or bool(in_flight_titles)
        )

        # Card 6: Manual Search defaults & Search Presets
        from app.services.preset_service import list_presets

        presets = await list_presets(session)
        presets_payload = [p.to_dict() for p in presets]
        db_settings = (
            (
                await session.execute(
                    select(SystemSettings).where(SystemSettings.id == 1)
                )
            )
            .scalars()
            .first()
        )
        defaults: dict[str, Any] = {}
        if db_settings and db_settings.scoring_settings:
            defaults = db_settings.scoring_settings.get("manual_search_defaults", {})

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "items": items,
            "items_payload": items_payload,
            "in_progress_items": in_progress_items,
            "ready_to_push_items": ready_to_push_items,
            "upcoming_items": upcoming_items,
            "movie_items": movie_items,
            "series_items": series_items,
            "anime_items": anime_items,
            "movie_stats": movie_stats,
            "series_stats": series_stats,
            "anime_stats": anime_stats,
            "movies_wanted": movie_stats["wanted"],
            "series_wanted": series_stats["wanted"],
            "anime_wanted": anime_stats["wanted"],
            "push_history_ledger": push_history_ledger,
            "history_total": len(push_history_ledger),
            "dry_run_history_count": dry_run_history_count,
            "active_pushes": active_pushes,
            "active_pushes_count": len(active_pushes),
            "downloading_count": downloading_count,
            "in_flight_titles": in_flight_titles,
            "poller_awake": poller_awake,
            "presets": presets,
            "presets_payload": presets_payload,
            "defaults": defaults,
        },
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

    from app.db.database import async_session_factory
    from app.db.models import SystemSettings

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

    clean_query = query.strip()
    clean_imdb = imdb_id.strip()
    clean_tmdb = tmdb_id.strip()
    clean_tvdb = tvdb_id.strip()
    if not clean_query and not clean_imdb and not clean_tmdb and not clean_tvdb:
        return templates.TemplateResponse(
            request=request,
            name="partials/search_results.html",
            context={
                "results": [],
                "error_message": "Please enter a search query or at least one External ID (IMDb, TMDb, or TVDb).",
            },
        )

    from app.core.adhoc_search import AdHocFilters, search_and_score_releases

    season_val = int(season) if season and season.isdigit() else None
    ep_val = int(episode) if episode and episode.isdigit() else None
    tmdb_val = int(clean_tmdb) if clean_tmdb and clean_tmdb.isdigit() else None
    tvdb_val = int(clean_tvdb) if clean_tvdb and clean_tvdb.isdigit() else None
    if ep_val is not None:
        scope = "episode"
    elif season_val is not None:
        scope = "season"
    else:
        scope = "item"

    search = await search_and_score_releases(
        kind=(category or "any").strip().lower(),
        scope=scope,
        titles=[clean_query] if clean_query else [],
        year=None,
        imdb_id=clean_imdb or None,
        tmdb_id=tmdb_val,
        tvdb_id=tvdb_val,
        season=season_val,
        episode=ep_val,
        filters=AdHocFilters(
            primary_language=primary_language,
            fallback_language=fallback_language,
            language=language,
            resolution=resolution,
            source=source,
            hdr=hdr,
            video_codec=video_codec,
            audio_tier=audio_tier,
            audio_channels=audio_channels,
        ),
        limit=limit,
    )
    if search.error_message:
        return templates.TemplateResponse(
            request=request,
            name="partials/search_results.html",
            context={"results": [], "error_message": search.error_message},
        )
    primary_results = search.primary_results
    fallback_results = search.fallback_results
    mismatched_results = search.mismatched_results

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


@app.post("/items/{item_id}/reset-metadata")
async def reset_anime_metadata_endpoint(request: Request, item_id: int):
    """Rebuilds anime metadata cleanly from AniList, purges phantom episodes, and resets backoff counters."""
    from app.core.automation import reset_anime_metadata
    from app.core.push_engine import get_push_modal_context
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
        if request.headers.get("HX-Request") == "true":
            ctx = await get_push_modal_context(session, item_id)
            if ctx is not None:
                return templates.TemplateResponse(
                    request=request,
                    name="modals/push_modal.html",
                    context=ctx,
                )

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
            try:
                result = await torbox.send_magnet_link(magnet, is_manual=True)
            except (torbox.DownloaderNetworkError, torbox.TorBoxError) as e:
                result = {"error": str(e)}
        else:
            try:
                from app.core.fake_detector import extract_nzb_password

                nzb_bytes, filename = await treasure_maps.fetch_nzb_bytes(magnet)
                nzb_pass = extract_nzb_password(nzb_bytes)
                result = await torbox.send_nzb_file(
                    nzb_bytes, filename=filename, is_manual=True, password=nzb_pass
                )
            except treasure_maps.IndexerError as e:
                result = {"error": f"NZB Download Error: {e}"}
            except (torbox.DownloaderNetworkError, torbox.TorBoxError) as e:
                result = {"error": str(e)}

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


@app.post("/api/items/{item_id}/change-type")
async def change_type_endpoint(
    request: Request,
    item_id: int,
    background_tasks: BackgroundTasks,
    target_type: str = Form(...),
):

    from sqlalchemy.orm import selectinload

    from app.core.push_engine import get_push_modal_context
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
            item.last_searched_at = None
            if target_is_series:
                for s in item.seasons:
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

        if request.headers.get("HX-Request") == "true":
            ctx = await get_push_modal_context(session, item_id)
            if ctx is not None:
                return templates.TemplateResponse(
                    request=request,
                    name="modals/push_modal.html",
                    context=ctx,
                )

    return RedirectResponse(url=request.headers.get("referer", "/"), status_code=303)


@app.delete("/items/{item_id}")
async def delete_item(item_id: int):
    """Delete an item from the watchlist while preserving detached DownloadHistory snapshots."""
    from app.core.push_engine import detach_item_download_history
    from app.db.database import async_session_factory
    from app.db.models import MediaItem

    async with async_session_factory() as session:
        item = await session.get(MediaItem, item_id)
        if item:
            await detach_item_download_history(session, item)
            await session.delete(item)
            await session.commit()
            return HTMLResponse(
                content=""
            )  # Empty response means success (HTMX can remove element)
    return HTMLResponse(content="Error", status_code=400)


# ---------------------------------------------------------------------------
# Settings & Configuration Routes
# ---------------------------------------------------------------------------


@app.get("/settings", response_class=HTMLResponse)
async def get_settings_page(request: Request):
    """Render the settings form."""
    from sqlalchemy import select

    from app.db.models import (
        BlacklistedRelease,
        NotificationChannel,
        Provider,
        SystemSettings,
    )

    async with async_session_factory() as session:
        stmt = select(SystemSettings).where(SystemSettings.id == 1)
        db_settings = (await session.execute(stmt)).scalars().first()

        raw_providers = (await session.execute(select(Provider))).scalars().all()

        category_order = {"watchlist": 0, "metadata": 1, "downloader": 2, "indexer": 3}

        def provider_sort_key(p: Provider) -> tuple[int, int, str]:
            cat = p.category.value if hasattr(p.category, "value") else str(p.category)
            cat_rank = category_order.get(cat.lower(), 99)
            # Priority ascending, nulls last (rank infinity or 999999)
            pri = p.priority if p.priority is not None else 999999
            return (cat_rank, pri, p.name or "")

        providers = sorted(raw_providers, key=provider_sort_key)
        total_providers_count = len(providers)
        active_providers_count = sum(1 for p in providers if p.is_active)
        notifications = (
            (await session.execute(select(NotificationChannel))).scalars().all()
        )

        from app.services.preset_service import list_presets

        presets = await list_presets(session)
        default_preset = next((p for p in presets if p.is_default), None)

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
            "total_providers_count": total_providers_count,
            "active_providers_count": active_providers_count,
            "presets": presets,
            "default_preset": default_preset,
            "notifications": notifications,
            "blacklisted_releases": blacklisted_releases,
        },
    )


@app.post("/settings")
@app.post("/settings/global")
@app.post("/settings/system")
@app.post("/settings/auto-save")
async def save_global_settings(request: Request):
    import copy

    from sqlalchemy import select

    from app.core.default_scoring import DEFAULT_SCORING_CONFIG
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

        if "download_timeout_minutes" in payload:
            try:
                val = int(str(payload["download_timeout_minutes"]))
                if val in (15, 30, 60, 120, 240):
                    db_settings.download_timeout_minutes = val
                else:
                    db_settings.download_timeout_minutes = 60
            except (ValueError, TypeError):
                db_settings.download_timeout_minutes = 60

        for field, allowed, default in (
            ("air_delay_hours", (0, 1, 2, 4, 6, 12), 2),
            ("upgrade_window_hours", (24, 48, 72, 168), 48),
            ("max_upgrade_checks", (1, 2, 3, 5), 3),
            ("history_retention_days", (0, 7, 14, 30, 60, 90), 30),
        ):
            if field in payload:
                try:
                    val = int(str(payload[field]))
                except (ValueError, TypeError):
                    val = default
                setattr(db_settings, field, val if val in allowed else default)

        if "sh_max_retries" in payload:
            try:
                val = int(str(payload["sh_max_retries"]))
                if val >= 0:
                    db_settings.sh_max_retries = val
            except (ValueError, TypeError):
                pass

        if "discord_webhook_url" in payload:
            raw_url = str(payload.get("discord_webhook_url") or "").strip()
            db_settings.discord_webhook_url = raw_url or None

        if "discord_enabled" in payload:
            db_settings.discord_enabled = _to_bool(payload.get("discord_enabled"))

        if "notify_on_push_initiated" in payload:
            db_settings.notify_on_push_initiated = _to_bool(
                payload.get("notify_on_push_initiated")
            )

        if "notify_on_completed" in payload:
            db_settings.notify_on_completed = _to_bool(
                payload.get("notify_on_completed")
            )

        if "notify_on_failure" in payload:
            db_settings.notify_on_failure = _to_bool(payload.get("notify_on_failure"))

        if "notify_on_auto_advance" in payload:
            db_settings.notify_on_auto_advance = _to_bool(
                payload.get("notify_on_auto_advance")
            )

        if "notify_on_errors" in payload:
            db_settings.notify_on_errors = _to_bool(payload.get("notify_on_errors"))

        if "notify_on_dry_run" in payload:
            db_settings.notify_on_dry_run = _to_bool(payload.get("notify_on_dry_run"))

        if "debug_mode" in payload:
            db_settings.debug_mode = _to_bool(payload.get("debug_mode"))

        if "dry_run" in payload:
            db_settings.dry_run = _to_bool(payload.get("dry_run"))

        if "history_autoclear_pruned" in payload:
            db_settings.history_autoclear_pruned = _to_bool(
                payload.get("history_autoclear_pruned")
            )

        sc: dict[str, Any] = copy.deepcopy(DEFAULT_SCORING_CONFIG)
        if (
            isinstance(db_settings.scoring_settings, dict)
            and "manual_search_defaults" in db_settings.scoring_settings
        ):
            sc["manual_search_defaults"] = copy.deepcopy(
                db_settings.scoring_settings["manual_search_defaults"]
            )
        db_settings.scoring_settings = sc

        await session.commit()
        await reload_settings_from_db(session)

    # If requested via /settings/auto-save or caller requests JSON, return JSONResponse with HX-Trigger
    if (
        request.url.path == "/settings/auto-save"
        or request.headers.get("accept") == "application/json"
    ):
        return JSONResponse(
            {"status": "ok"},
            headers={"HX-Trigger": '{"showAutoSaveToast": "Saved"}'},
        )

    return HTMLResponse(
        content='<div class="p-4 mb-4 bg-[#d40060] text-white rounded">Settings saved successfully!</div>',
        headers={"HX-Trigger": '{"showAutoSaveToast": "Saved"}'},
    )


@app.get("/settings/provider/new", response_class=HTMLResponse)
@app.get("/settings/provider/{provider_id}/edit", response_class=HTMLResponse)
async def provider_modal(request: Request, provider_id: int | None = None):
    from sqlalchemy import select

    from app.db.models import NotificationChannel, Provider

    async with async_session_factory() as session:
        provider = None
        if provider_id:
            provider = await session.get(Provider, provider_id)

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
    refresh_token: str = Form(""),
    token_expires_at: str = Form(""),
    client_id: str = Form(""),
    api_key: str = Form(""),
    api_url: str = Form(""),
    priority: int = Form(1),
    is_active: bool = Form(False),
    category: str = Form(""),
    enable_movies: bool = Form(False),
    enable_series: bool = Form(False),
    enable_anime: bool = Form(False),
    provider_type: str = Form("simkl"),
    sync_interval_minutes: int = Form(0),
    sync_movies: bool | None = Form(None),
    sync_series: bool | None = Form(None),
    sync_anime: bool | None = Form(None),
):
    from app.db.models import Provider, ProviderCategory
    from app.services import provider_service, simkl

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
        old_client_id = ""
        old_access_token = ""
        old_refresh_token = ""

        if provider_id:
            provider = await session.get(Provider, provider_id)
            if not provider:
                provider = Provider(type=provider_type, category=resolved_category)
                session.add(provider)
            else:
                old_cfg = provider.simkl_config
                old_client_id = str(
                    provider.client_id or old_cfg.get("client_id") or ""
                ).strip()
                old_access_token = str(
                    provider.access_token or old_cfg.get("access_token") or ""
                ).strip()
                old_refresh_token = str(old_cfg.get("refresh_token") or "").strip()
                provider.type = provider_type
                provider.category = resolved_category
        else:
            provider = Provider(type=provider_type, category=resolved_category)
            session.add(provider)

        effective_access_token = (
            old_access_token
            if access_token.strip() == "***" and old_access_token
            else access_token.strip()
        )

        provider.name = name
        provider.username = simkl_username
        provider.access_token = effective_access_token
        provider.client_id = client_id.strip()
        provider.api_key = api_key
        provider.api_url = api_url
        provider.priority = priority
        provider.is_active = is_active

        if provider.type.lower() == "simkl":
            import json

            existing_cfg = provider.simkl_config
            # If user manually replaced an existing token with a different one, best-effort revoke old token
            if (
                old_access_token
                and effective_access_token
                and effective_access_token != old_access_token
            ):
                tok_to_revoke = old_refresh_token or old_access_token
                if (
                    tok_to_revoke
                    and tok_to_revoke != refresh_token.strip()
                    and tok_to_revoke != effective_access_token
                ):
                    await simkl.revoke_token(
                        old_client_id or client_id.strip(), tok_to_revoke
                    )

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
            existing_cfg["client_id"] = client_id.strip()
            existing_cfg["access_token"] = effective_access_token

            if refresh_token.strip():
                existing_cfg["refresh_token"] = refresh_token.strip()
            elif effective_access_token == old_access_token and old_refresh_token:
                existing_cfg["refresh_token"] = old_refresh_token
            elif not effective_access_token or (
                old_access_token and effective_access_token != old_access_token
            ):
                existing_cfg["refresh_token"] = None

            if token_expires_at.strip():
                existing_cfg["token_expires_at"] = token_expires_at.strip()
            elif effective_access_token == old_access_token and existing_cfg.get(
                "token_expires_at"
            ):
                pass
            elif not existing_cfg.get("refresh_token"):
                existing_cfg["token_expires_at"] = None

            provider.config_json = json.dumps(existing_cfg)

        await session.flush()  # get ID

        # If Downloader and active, enforce single-active downloader rule
        if (
            provider.category == ProviderCategory.DOWNLOADER.value
            and provider.is_active
        ):
            await provider_service.set_active_downloader(session, provider.id)

        await session.commit()

    # Reload page
    return HTMLResponse(content="<script>window.location.reload();</script>")


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

    return HTMLResponse(content="<script>window.location.reload();</script>")


@app.delete("/settings/provider/{provider_id}", response_class=HTMLResponse)
async def delete_provider(provider_id: int) -> HTMLResponse:
    from app.db.models import Provider
    from app.services import simkl

    async with async_session_factory() as session:
        provider = await session.get(Provider, provider_id)
        if provider:
            if (provider.type or "").lower() == "simkl" or (
                provider.name or ""
            ).lower() == "simkl":
                cfg = provider.simkl_config
                cid = str(provider.client_id or cfg.get("client_id") or "").strip()
                tok = str(
                    cfg.get("refresh_token")
                    or provider.access_token
                    or cfg.get("access_token")
                    or ""
                ).strip()
                if cid and tok:
                    await simkl.revoke_token(cid, tok)
            await session.delete(provider)
            await session.commit()
    return HTMLResponse(content="<script>window.location.reload();</script>")


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

    return HTMLResponse(content="<script>window.location.reload();</script>")


@app.delete("/settings/notification/{notification_id}", response_class=HTMLResponse)
async def delete_notification(notification_id: int) -> HTMLResponse:
    from app.db.models import NotificationChannel

    async with async_session_factory() as session:
        notification = await session.get(NotificationChannel, notification_id)
        if notification:
            await session.delete(notification)
            await session.commit()
    return HTMLResponse(content="<script>window.location.reload();</script>")


@app.post("/api/settings/discord/test")
@app.post("/api/notifications/test/discord")
@app.post("/settings/notification/discord/test")
async def test_discord_notification(request: Request) -> Response:
    """Send a test rich Discord Embed to verify the configured or provided Discord Webhook URL."""
    from sqlalchemy import select

    from app.config import reload_settings_from_db
    from app.db.models import SystemSettings
    from app.services import discord

    payload = await _parse_request_payload(request)
    provided_url = str(payload.get("discord_webhook_url") or "").strip()
    webhook_url = provided_url

    async with async_session_factory() as session:
        stmt = select(SystemSettings).where(SystemSettings.id == 1)
        db_settings = (await session.execute(stmt)).scalars().first()
        if not webhook_url:
            if db_settings and db_settings.discord_webhook_url:
                webhook_url = db_settings.discord_webhook_url.strip()
            elif settings.discord_webhook_url:
                webhook_url = settings.discord_webhook_url.strip()

        if not webhook_url:
            if request.headers.get("HX-Request") == "true":
                return HTMLResponse(
                    content='<div class="p-3 bg-red-900/30 border border-red-500/40 rounded-lg text-xs text-red-300 font-medium">Please enter a Discord Webhook URL first.</div>',
                    status_code=200,
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
        if ok and provided_url:
            if not db_settings:
                db_settings = SystemSettings(id=1)
                session.add(db_settings)
            db_settings.discord_webhook_url = provided_url
            db_settings.discord_enabled = True
            await session.commit()
            await reload_settings_from_db(session)

    if request.headers.get("HX-Request") == "true":
        if ok:
            return HTMLResponse(
                content='<div class="p-3 bg-emerald-900/30 border border-emerald-500/40 rounded-lg text-xs text-emerald-300 font-medium">Discord test notification sent &amp; webhook saved!</div>',
                status_code=200,
            )
        return HTMLResponse(
            content='<div class="p-3 bg-red-900/30 border border-red-500/40 rounded-lg text-xs text-red-300 font-medium">Discord webhook delivery failed — please check the Webhook URL.</div>',
            status_code=200,
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
        db_settings.notify_on_errors = _to_bool(
            payload.get("notify_on_errors", db_settings.notify_on_errors)
        )

        await session.commit()
        await reload_settings_from_db(session)

    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(
            content='<div class="p-3 bg-emerald-900/40 border border-emerald-500/40 text-emerald-200 rounded-lg text-xs">Notification settings saved!</div>'
        )
    return JSONResponse({"ok": True})


def _render_simkl_device_card(auth_session: dict[str, Any]) -> str:
    """Render the HTMX RFC 8628 Device Authorization card without exposing device_code."""
    session_id = auth_session["session_id"]
    user_code = auth_session["user_code"]
    verification_uri = auth_session.get("verification_uri", "https://simkl.com/pin")
    verification_uri_complete = auth_session.get(
        "verification_uri_complete", f"{verification_uri}?user_code={user_code}"
    )
    interval = int(auth_session.get("interval", 5) or 5)
    return f"""
    <div id="simkl-poll-status" hx-get="/simkl/auth/poll?session_id={session_id}" hx-trigger="every {interval}s" hx-swap="outerHTML" class="bg-[#111115] border border-[#d40060]/60 rounded-xl p-5 text-center mt-3 space-y-3">
        <h4 class="text-white font-bold text-sm">Simkl AUTH V2 Device Authorization</h4>
        <p class="text-[#a1a1aa] text-xs">Click the button below or visit <a href="{verification_uri}" target="_blank" rel="noopener noreferrer" class="text-[#d40060] hover:underline font-semibold">{verification_uri}</a> and confirm your 8-character code:</p>
        <div class="text-2xl font-mono font-bold text-[#d40060] tracking-widest bg-[#1e1e24] border border-[#3f3f46] rounded-lg py-2.5 px-4 inline-block">{user_code}</div>
        <div>
            <a href="{verification_uri_complete}" target="_blank" rel="noopener noreferrer" class="inline-flex items-center gap-2 px-4 py-2 bg-[#d40060] hover:bg-[#a3004a] text-white rounded-lg text-xs font-bold transition-colors shadow-lg shadow-[#d40060]/20">
                Open Simkl &amp; Authorize ({user_code})
            </a>
        </div>
        <div class="text-xs text-amber-400 animate-pulse pt-1">
            Waiting for authorization on Simkl (polling every {interval}s)...
        </div>
    </div>
    """


@app.post("/simkl/auth/start")
async def start_simkl_auth(
    client_id: str = Form(""),
    provider_id: str | None = Form(None),
) -> HTMLResponse:
    """Start the Simkl AUTH V2 RFC 8628 Device Authorization Flow."""
    from app.services import simkl

    clean_client_id = (client_id or "").strip()
    if not clean_client_id:
        return HTMLResponse(
            content='<div class="p-3 bg-amber-900/30 border border-amber-500/40 rounded-lg text-amber-200 text-xs mt-2">Please enter your Simkl V2 Client ID first.</div>'
        )

    try:
        data = await simkl.request_device_code(clean_client_id, scope="media:read")
    except simkl.SimklOAuthError as e:
        if e.status_code in (401, 412) or e.error in (
            "invalid_client",
            "client_id_failed",
        ):
            return HTMLResponse(
                content=(
                    '<div class="p-4 bg-red-900/30 border border-red-500/40 rounded-xl text-red-200 text-xs mt-2 space-y-1.5">'
                    '<div class="font-bold text-red-300">Simkl AUTH V2 Client ID Rejected ('
                    f"{e.status_code} {e.error})</div>"
                    "<p>Simkl AUTH V2 Device Flow requires a public client application. Please register or update your app at "
                    '<a href="https://simkl.com/settings/developer/" target="_blank" rel="noopener noreferrer" class="underline font-semibold text-white">simkl.com/settings/developer</a> '
                    "with <strong>Application Type</strong> set to <strong>TV, devices &amp; command line</strong> or "
                    "<strong>Mobile, desktop &amp; browser apps</strong>.</p>"
                    "</div>"
                )
            )
        return HTMLResponse(
            content=f'<div class="p-3 bg-red-900/30 border border-red-500/40 rounded-lg text-red-300 text-xs mt-2">Error requesting PIN: {e}</div>'
        )
    except Exception as e:
        return HTMLResponse(
            content=f'<div class="p-3 bg-red-900/30 border border-red-500/40 rounded-lg text-red-300 text-xs mt-2">Error requesting PIN: {e}</div>'
        )

    if not data.get("user_code") or not data.get("device_code"):
        return HTMLResponse(
            content='<div class="p-3 bg-red-900/30 border border-red-500/40 rounded-lg text-red-300 text-xs mt-2">Invalid device authorization response from Simkl.</div>'
        )

    prov_id_int = (
        int(provider_id) if provider_id and str(provider_id).isdigit() else None
    )
    auth_session = simkl.create_device_auth_session(
        clean_client_id, data, provider_id=prov_id_int
    )
    return HTMLResponse(content=_render_simkl_device_card(auth_session))


@app.get("/simkl/auth/poll")
async def poll_simkl_auth(session_id: str = "") -> HTMLResponse:
    """Poll Simkl AUTH V2 token endpoint using server-side session_id without exposing device_code."""
    import json

    from sqlalchemy import select

    from app.db.models import Provider
    from app.services import simkl

    auth_session = simkl.get_device_auth_session(session_id)
    if auth_session is None:
        return HTMLResponse(
            content='<div class="p-3 bg-amber-900/30 border border-amber-500/40 rounded-lg text-amber-200 text-xs mt-2">Expired PIN — please click Get PIN &amp; Connect again.</div>'
        )

    try:
        token_data = await simkl.poll_device_token(
            auth_session["client_id"], auth_session["device_code"]
        )
    except simkl.SimklOAuthError as e:
        if e.error == "authorization_pending":
            return HTMLResponse(content=_render_simkl_device_card(auth_session))
        if e.error == "slow_down":
            auth_session["interval"] = int(auth_session.get("interval", 5) or 5) + 5
            return HTMLResponse(content=_render_simkl_device_card(auth_session))
        simkl.pop_device_auth_session(session_id)
        if e.error in ("expired_token", "access_denied"):
            return HTMLResponse(
                content=f'<div class="p-3 bg-amber-900/30 border border-amber-500/40 rounded-lg text-amber-200 text-xs mt-2">Simkl authorization ended ({e.error}). Please click Get PIN &amp; Connect to try again.</div>'
            )
        return HTMLResponse(
            content=f'<div class="p-3 bg-red-900/30 border border-red-500/40 rounded-lg text-red-300 text-xs mt-2">Error polling Simkl status: {e}</div>'
        )
    except Exception as e:
        simkl.pop_device_auth_session(session_id)
        return HTMLResponse(
            content=f'<div class="p-3 bg-red-900/30 border border-red-500/40 rounded-lg text-red-300 text-xs mt-2">Error polling status: {e}</div>'
        )

    simkl.pop_device_auth_session(session_id)
    client_id = str(auth_session["client_id"])
    access_token = str(token_data.get("access_token") or "")
    refresh_token = str(token_data.get("refresh_token") or "")
    token_expires_at = str(token_data.get("token_expires_at") or "")
    target_provider_id = auth_session.get("provider_id")

    # Persist to existing Simkl Provider row in SQLite if present, and revoke old token best-effort
    async with async_session_factory() as session:
        provider: Provider | None = None
        if target_provider_id:
            provider = await session.get(Provider, int(target_provider_id))
        if provider is None:
            stmt = select(Provider).where(Provider.type == "simkl")
            provider = (await session.execute(stmt)).scalars().first()

        if provider is not None:
            old_cfg = provider.simkl_config
            old_cid = str(
                provider.client_id or old_cfg.get("client_id") or client_id
            ).strip()
            old_tok = str(
                old_cfg.get("refresh_token")
                or provider.access_token
                or old_cfg.get("access_token")
                or ""
            ).strip()
            if old_tok and old_tok not in (access_token, refresh_token):
                await simkl.revoke_token(old_cid, old_tok)

            provider.client_id = client_id
            provider.access_token = access_token
            old_cfg["client_id"] = client_id
            old_cfg["access_token"] = access_token
            old_cfg["refresh_token"] = refresh_token
            old_cfg["token_expires_at"] = token_expires_at
            provider.config_json = json.dumps(old_cfg)
            await session.commit()

    return HTMLResponse(
        content=f"""
        <div class="p-3 bg-emerald-900/30 border border-emerald-500/40 rounded-lg text-emerald-300 text-xs font-bold mt-2 flex items-center justify-between">
            <span>Connected · AUTH V2 (Auto-Refresh Enabled)</span>
        </div>
        <script>
            (function() {{
                const atInput = document.getElementById('access_token') || document.getElementById('simkl-access-token');
                if (atInput) atInput.value = "{access_token}";
                const rtInput = document.getElementById('simkl-refresh-token');
                if (rtInput) rtInput.value = "{refresh_token}";
                const expInput = document.getElementById('simkl-token-expires-at');
                if (expInput) expInput.value = "{token_expires_at}";
                const badge = document.getElementById('simkl-modal-status-badge');
                if (badge) {{
                    badge.textContent = "Connected · AUTH V2 (Auto-Refresh)";
                    badge.className = "px-2.5 py-1 rounded-full text-xs font-bold bg-emerald-500/15 text-emerald-400 border border-emerald-500/30";
                }}
            }})();
        </script>
        """
    )


@app.get("/system/check/modal", response_class=HTMLResponse)
async def system_check_modal(request: Request):
    """Render the system check modal."""
    return templates.TemplateResponse(
        request=request, name="modals/system_check.html", context={}
    )


@app.get("/feedback/modal", response_class=HTMLResponse)
async def feedback_modal(
    request: Request, type: Literal["bug", "feature"] = "bug"
) -> Response:
    """Render the bug report / feature request modal with privacy-safe diagnostics."""
    from app.core.diagnostics import (
        GITHUB_NEW_ISSUE_URL,
        build_bug_markdown,
        build_feature_markdown,
        collect_feedback_diagnostics,
    )

    async with async_session_factory() as session:
        diag = await collect_feedback_diagnostics(session)
    return templates.TemplateResponse(
        request=request,
        name="modals/feedback_modal.html",
        context={
            "initial_tab": type,
            "diagnostics": diag,
            "bug_markdown": build_bug_markdown(diag),
            "feature_markdown": build_feature_markdown(diag),
            "github_issues_url": GITHUB_NEW_ISSUE_URL,
        },
    )


@app.get("/system/check/run", response_class=HTMLResponse)
async def run_system_check(request: Request):
    """Run concurrent diagnostic tests for configured services and core infrastructure."""
    import asyncio
    import html as html_escape
    import os
    import shutil
    import time

    import httpx
    from sqlalchemy import select, text

    from app.config import DEFAULT_USER_AGENT
    from app.db.models import NotificationChannel, Provider, SystemSettings
    from app.services import anilist, simkl

    async with async_session_factory() as session:
        providers = (await session.execute(select(Provider))).scalars().all()
        notifications = (
            (await session.execute(select(NotificationChannel))).scalars().all()
        )
        sys_settings = (
            (
                await session.execute(
                    select(SystemSettings).where(SystemSettings.id == 1)
                )
            )
            .scalars()
            .first()
        )

    # Active providers & notification channels
    active_providers = [p for p in providers if p.is_active]
    active_indexers = [
        p
        for p in active_providers
        if p.category == "indexer" or p.type in ("treasure_maps", "newznab")
    ]
    active_downloaders = [
        p for p in active_providers if p.category == "downloader" or p.type == "torbox"
    ]
    active_simkl = next(
        (p for p in active_providers if p.type == "simkl"),
        None,
    )
    active_tmdb = next(
        (p for p in active_providers if p.type == "tmdb"),
        None,
    )

    # Vital role warning banners
    vital_warnings: list[str] = []
    if not active_downloaders:
        vital_warnings.append(
            "No Usenet Downloader (TorBox) active. Downloads cannot be dispatched."
        )
    if not active_indexers:
        vital_warnings.append(
            "No Usenet Indexer active. Automated searches will yield no releases."
        )

    # Define individual probe coroutines
    async def probe_database() -> dict[str, Any]:
        start = time.perf_counter()
        try:
            async with async_session_factory() as s:
                await asyncio.wait_for(s.execute(text("SELECT 1")), timeout=5.0)
            latency = int((time.perf_counter() - start) * 1000)
            return {
                "name": "SQLite Database",
                "category": "Core Infrastructure",
                "ok": True,
                "msg": "Operational (SELECT 1 passed)",
                "latency_ms": latency,
            }
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {
                "name": "SQLite Database",
                "category": "Core Infrastructure",
                "ok": False,
                "msg": f"Query failed: {e}",
                "latency_ms": latency,
            }

    async def probe_storage() -> dict[str, Any]:
        start = time.perf_counter()
        try:
            # Check write permissions and disk space in /app/data, fallback to ./config or .
            candidate_paths = ["/app/data", "/app/config", "./config", "."]
            target_path = next((p for p in candidate_paths if os.path.exists(p)), ".")

            # Test write access
            test_file = os.path.join(target_path, ".diag_write_test")
            try:
                with open(test_file, "w") as f:
                    f.write("ok")
                if os.path.exists(test_file):
                    os.remove(test_file)
            except Exception as write_err:
                latency = int((time.perf_counter() - start) * 1000)
                return {
                    "name": "Persistent Storage",
                    "category": "Core Infrastructure",
                    "ok": False,
                    "msg": f"Read-only filesystem: {write_err}",
                    "latency_ms": latency,
                }

            total, used, free = shutil.disk_usage(target_path)
            free_gb = free / (1024**3)
            latency = int((time.perf_counter() - start) * 1000)
            return {
                "name": "Persistent Storage",
                "category": "Core Infrastructure",
                "ok": True,
                "msg": f"Writable ({free_gb:.1f} GB free)",
                "latency_ms": latency,
            }
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {
                "name": "Persistent Storage",
                "category": "Core Infrastructure",
                "ok": False,
                "msg": f"Storage probe failed: {e}",
                "latency_ms": latency,
            }

    async def probe_anilist() -> dict[str, Any]:
        start = time.perf_counter()
        try:
            query = {"query": "{ Page(perPage: 1) { media(type: ANIME) { id } } }"}
            async with httpx.AsyncClient(
                timeout=5.0, headers={"User-Agent": DEFAULT_USER_AGENT}
            ) as client:
                r = await client.post(anilist.ANILIST_URL, json=query)
                latency = int((time.perf_counter() - start) * 1000)
                ok = r.status_code == 200
                msg = "GraphQL API reachable" if ok else f"HTTP {r.status_code}"
                return {
                    "name": "AniList API",
                    "category": "Metadata Provider",
                    "ok": ok,
                    "msg": msg,
                    "latency_ms": latency,
                }
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {
                "name": "AniList API",
                "category": "Metadata Provider",
                "ok": False,
                "msg": str(e),
                "latency_ms": latency,
            }

    async def probe_simkl(p: Provider) -> dict[str, Any]:
        start = time.perf_counter()
        try:
            async with async_session_factory() as s:
                db_prov = await s.get(Provider, p.id)
                if not db_prov:
                    raise ValueError("Provider not found")
                cid, tok = await asyncio.wait_for(
                    simkl.ensure_valid_simkl_token(s, db_prov),
                    timeout=5.0,
                )
                r = await asyncio.wait_for(
                    simkl.check_simkl_user_settings(cid, tok),
                    timeout=5.0,
                )
                if r.status_code == 401 and db_prov.simkl_config.get("refresh_token"):
                    cid, tok = await asyncio.wait_for(
                        simkl.refresh_provider_simkl_token(s, db_prov),
                        timeout=5.0,
                    )
                    r = await asyncio.wait_for(
                        simkl.check_simkl_user_settings(cid, tok),
                        timeout=5.0,
                    )
            latency = int((time.perf_counter() - start) * 1000)
            ok = r.status_code == 200
            user_label = p.username or "Authenticated"
            if ok:
                try:
                    data = r.json()
                    user_label = (data.get("user") or {}).get("name") or user_label
                except Exception:
                    pass
            msg = f"User: {user_label}" if ok else f"HTTP {r.status_code}"
            return {
                "name": f"{p.name or 'Simkl'} (Watchlist)",
                "category": "Watchlist Provider",
                "ok": ok,
                "msg": msg,
                "latency_ms": latency,
            }
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {
                "name": f"{p.name or 'Simkl'} (Watchlist)",
                "category": "Watchlist Provider",
                "ok": False,
                "msg": str(e),
                "latency_ms": latency,
            }

    async def probe_torbox(p: Provider) -> dict[str, Any]:
        start = time.perf_counter()
        tb_key = (p.api_key or "").strip()
        tb_url = (p.api_url or "https://api.torbox.app/v1/api").strip()
        if not tb_key:
            return {
                "name": f"{p.name or 'TorBox'} (Downloader)",
                "category": "Usenet Downloader",
                "ok": False,
                "msg": "API Key missing",
                "latency_ms": 0,
            }
        try:
            base_url = tb_url.rstrip("/")
            endpoint = (
                f"{base_url}/user/me"
                if base_url.endswith("/api")
                else f"{base_url}/api/user/me"
            )
            async with httpx.AsyncClient(
                timeout=5.0, headers={"User-Agent": DEFAULT_USER_AGENT}
            ) as client:
                r = await client.get(
                    endpoint,
                    headers={"Authorization": f"Bearer {tb_key}"},
                )
            latency = int((time.perf_counter() - start) * 1000)
            ok = r.status_code == 200
            detail = "Account verified"
            if ok:
                try:
                    data = r.json().get("data") or {}
                    email = data.get("email")
                    plan = data.get("plan")
                    if email:
                        detail = f"{email} (Plan {plan})" if plan is not None else email
                except Exception:
                    pass
            msg = detail if ok else f"HTTP {r.status_code}"
            return {
                "name": f"{p.name or 'TorBox'} (Downloader)",
                "category": "Usenet Downloader",
                "ok": ok,
                "msg": msg,
                "latency_ms": latency,
            }
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {
                "name": f"{p.name or 'TorBox'} (Downloader)",
                "category": "Usenet Downloader",
                "ok": False,
                "msg": str(e),
                "latency_ms": latency,
            }

    async def probe_tmdb(p: Provider) -> dict[str, Any]:
        start = time.perf_counter()
        tmdb_key = (p.api_key or "").strip()
        if not tmdb_key:
            return {
                "name": f"{p.name or 'TheMovieDB'} (Metadata)",
                "category": "Metadata Provider",
                "ok": False,
                "msg": "API Key missing",
                "latency_ms": 0,
            }
        try:
            headers = {"User-Agent": DEFAULT_USER_AGENT}
            params = {}
            if tmdb_key.startswith("ey"):
                headers["Authorization"] = f"Bearer {tmdb_key}"
            else:
                params["api_key"] = tmdb_key
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    "https://api.themoviedb.org/3/configuration",
                    params=params,
                    headers=headers,
                )
            latency = int((time.perf_counter() - start) * 1000)
            ok = r.status_code == 200
            msg = "API configuration accessible" if ok else f"HTTP {r.status_code}"
            return {
                "name": f"{p.name or 'TheMovieDB'} (Metadata)",
                "category": "Metadata Provider",
                "ok": ok,
                "msg": msg,
                "latency_ms": latency,
            }
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {
                "name": f"{p.name or 'TheMovieDB'} (Metadata)",
                "category": "Metadata Provider",
                "ok": False,
                "msg": str(e),
                "latency_ms": latency,
            }

    async def probe_indexer(p: Provider) -> dict[str, Any]:
        start = time.perf_counter()
        key = (p.api_key or "").strip()
        url = (p.api_url or "").strip()
        if not url:
            url = "https://treasure-maps.com/api" if p.type == "treasure_maps" else ""
        if not url:
            return {
                "name": p.name or "Usenet Indexer",
                "category": "Usenet Indexer",
                "ok": False,
                "msg": "Missing API URL",
                "latency_ms": 0,
            }
        try:
            params = {"t": "caps"}
            if key:
                params["apikey"] = key
            async with httpx.AsyncClient(
                timeout=5.0, headers={"User-Agent": DEFAULT_USER_AGENT}
            ) as client:
                r = await client.get(url, params=params)
            latency = int((time.perf_counter() - start) * 1000)
            body = r.text.lower()
            ok = r.status_code == 200 and (
                "<caps" in body or "<server" in body or "<categories" in body
            )
            msg = (
                "Caps XML verified"
                if ok
                else (
                    f"HTTP {r.status_code}"
                    if r.status_code != 200
                    else "Invalid caps XML"
                )
            )
            return {
                "name": p.name or "Usenet Indexer",
                "category": "Usenet Indexer",
                "ok": ok,
                "msg": msg,
                "latency_ms": latency,
            }
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {
                "name": p.name or "Usenet Indexer",
                "category": "Usenet Indexer",
                "ok": False,
                "msg": str(e),
                "latency_ms": latency,
            }

    async def probe_discord(webhook_url: str) -> dict[str, Any]:
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=5.0, headers={"User-Agent": DEFAULT_USER_AGENT}
            ) as client:
                r = await client.get(webhook_url)
            latency = int((time.perf_counter() - start) * 1000)
            ok = r.status_code == 200
            detail = "Webhook verified"
            if ok:
                try:
                    data = r.json()
                    name = data.get("name")
                    if name:
                        detail = f"Webhook: {name}"
                except Exception:
                    pass
            msg = detail if ok else f"HTTP {r.status_code}"
            return {
                "name": "Discord Webhook",
                "category": "Notifications",
                "ok": ok,
                "msg": msg,
                "latency_ms": latency,
            }
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {
                "name": "Discord Webhook",
                "category": "Notifications",
                "ok": False,
                "msg": str(e),
                "latency_ms": latency,
            }

    async def probe_telegram(ch: NotificationChannel) -> dict[str, Any]:
        start = time.perf_counter()
        bot_token = (ch.bot_token or "").strip()
        try:
            async with httpx.AsyncClient(
                timeout=5.0, headers={"User-Agent": DEFAULT_USER_AGENT}
            ) as client:
                r = await client.get(f"https://api.telegram.org/bot{bot_token}/getMe")
            latency = int((time.perf_counter() - start) * 1000)
            ok = r.status_code == 200
            detail = "Bot verified"
            if ok:
                try:
                    data = r.json()
                    user = (data.get("result") or {}).get("username")
                    if user:
                        detail = f"Bot @{user}"
                except Exception:
                    pass
            msg = detail if ok else f"HTTP {r.status_code}"
            return {
                "name": f"Telegram Bot ({ch.name})",
                "category": "Notifications",
                "ok": ok,
                "msg": msg,
                "latency_ms": latency,
            }
        except Exception as e:
            latency = int((time.perf_counter() - start) * 1000)
            return {
                "name": f"Telegram Bot ({ch.name})",
                "category": "Notifications",
                "ok": False,
                "msg": str(e),
                "latency_ms": latency,
            }

    # Build probe coroutine list
    probes = [
        probe_database(),
        probe_storage(),
        probe_anilist(),
    ]

    if active_simkl and (
        active_simkl.client_id
        or active_simkl.access_token
        or active_simkl.simkl_config.get("client_id")
        or active_simkl.simkl_config.get("access_token")
    ):
        probes.append(probe_simkl(active_simkl))

    for tb in active_downloaders:
        probes.append(probe_torbox(tb))

    if active_tmdb and active_tmdb.api_key:
        probes.append(probe_tmdb(active_tmdb))

    for idx in active_indexers:
        probes.append(probe_indexer(idx))

    # Enabled notifications
    if (
        sys_settings
        and sys_settings.discord_enabled
        and sys_settings.discord_webhook_url
    ):
        probes.append(probe_discord(sys_settings.discord_webhook_url))

    for tg in notifications:
        if tg.type == "telegram" and tg.bot_token:
            probes.append(probe_telegram(tg))

    # Run all probes concurrently
    probe_outputs = await asyncio.gather(*probes, return_exceptions=True)

    results: list[dict[str, Any]] = []
    for out in probe_outputs:
        if isinstance(out, dict):
            results.append(out)
        elif isinstance(out, Exception):
            results.append(
                {
                    "name": "Diagnostic Task",
                    "category": "Unknown",
                    "ok": False,
                    "msg": f"Task error: {out}",
                    "latency_ms": 0,
                }
            )

    # Render results HTML fragment
    total_count = len(results)
    passed_count = sum(1 for r in results if r["ok"])
    failed_count = total_count - passed_count

    html_parts: list[str] = ['<div class="space-y-4">']

    # Render vital role warning banners
    for w in vital_warnings:
        html_parts.append(f"""
        <div class="flex items-start gap-3 p-3 bg-amber-950/40 border border-amber-600/40 text-amber-200 rounded-lg text-xs leading-relaxed animate-fade-in">
            <svg class="w-4 h-4 text-amber-400 shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>
            </svg>
            <div>
                <span class="font-bold block text-amber-300">Vital Role Inactive</span>
                <span>{html_escape.escape(w)}</span>
            </div>
        </div>
        """)

    # Render service cards
    html_parts.append('<div class="space-y-2.5">')
    for item in results:
        ok = item["ok"]
        icon = "✅" if ok else "❌"
        status_color = "text-emerald-400" if ok else "text-rose-400"
        bg_color = (
            "bg-[#141418] border-[#2d2d38]"
            if ok
            else "bg-rose-950/10 border-rose-900/30"
        )
        latency = item.get("latency_ms", 0)

        # Latency badge styling
        if latency < 200:
            lat_color = "bg-emerald-950/50 text-emerald-300 border-emerald-800/40"
        elif latency <= 800:
            lat_color = "bg-amber-950/50 text-amber-300 border-amber-800/40"
        else:
            lat_color = "bg-rose-950/50 text-rose-300 border-rose-800/40"

        html_parts.append(f"""
        <div class="flex items-center justify-between p-3 {bg_color} border rounded-lg transition-colors">
            <div class="flex items-center gap-3 min-w-0 pr-2">
                <span class="text-base shrink-0">{icon}</span>
                <div class="min-w-0">
                    <div class="flex items-center gap-2">
                        <span class="font-semibold text-white text-xs truncate">{html_escape.escape(item["name"])}</span>
                        <span class="text-[10px] text-[#71717a] border border-[#3f3f46]/50 rounded px-1.5 py-0.2 shrink-0">{html_escape.escape(item["category"])}</span>
                    </div>
                    <div class="text-[11px] {status_color} mt-0.5 truncate">{html_escape.escape(item["msg"])}</div>
                </div>
            </div>
            <div class="shrink-0">
                <span class="text-[10px] font-mono px-2 py-0.5 rounded border {lat_color}">{latency}ms</span>
            </div>
        </div>
        """)
    html_parts.append("</div>")

    # Summary bar
    summary_bg = "bg-[#18181f] border-[#2d2d38]"
    html_parts.append(f"""
    <div class="flex items-center justify-between px-3 py-2 {summary_bg} border rounded-lg text-xs text-[#a1a1aa] mt-2">
        <div class="flex items-center gap-2">
            <span>Probes: <strong class="text-white font-mono">{total_count}</strong></span>
            <span>•</span>
            <span class="text-emerald-400">Passed: <strong class="font-mono">{passed_count}</strong></span>
            {f'<span>•</span><span class="text-rose-400">Failed: <strong class="font-mono">{failed_count}</strong></span>' if failed_count > 0 else ""}
        </div>
        <div class="text-[10px] text-[#71717a] font-mono">
            Concurrent engine
        </div>
    </div>
    """)

    html_parts.append("</div>")
    return HTMLResponse(content="\n".join(html_parts))


@app.post("/sync", response_class=HTMLResponse)
async def manual_sync() -> HTMLResponse:
    """Trigger manual sync for all configured providers — synchronous, blocks until complete."""
    from app.core.automation import sync_all_providers

    await sync_all_providers()
    msg = "Watchlist sync complete."

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
    """,
        headers={"HX-Trigger": "refreshDashboard"},
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
                "scope_label": (
                    f"Item #{i.media_item_id}"
                    if i.media_item_id is not None
                    else "Global"
                ),
                "nzb_guid": i.nzb_guid,
                "nzb_title": i.nzb_title,
                "reason": i.reason,
                "created_at": i.created_at.isoformat(),
            }
            for i in items
        ]


@app.post("/api/blacklist")
async def api_create_blacklisted_release(request: Request) -> Response:
    """Manually blacklist a release (item-scoped or global)."""
    from app.core.transfer_poller import ensure_blacklisted_release
    from app.db.database import async_session_factory

    payload = await _parse_request_payload(request)
    raw_item_id = payload.get("media_item_id")
    media_item_id: int | None = None
    if raw_item_id not in (None, "", "null", "None"):
        try:
            media_item_id = int(raw_item_id)
        except (TypeError, ValueError):
            media_item_id = None

    nzb_guid = str(payload.get("nzb_guid") or payload.get("guid") or "").strip() or None
    nzb_title = str(payload.get("nzb_title") or payload.get("title") or "").strip()
    reason = (
        str(payload.get("reason") or "Manually blacklisted").strip()
        or "Manually blacklisted"
    )

    if not nzb_title and not nzb_guid:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "nzb_title or nzb_guid is required"},
        )

    async with async_session_factory() as session:
        entry, created = await ensure_blacklisted_release(
            session,
            media_item_id=media_item_id,
            nzb_guid=nzb_guid,
            nzb_title=nzb_title or (nzb_guid or "Unknown Release"),
            reason=reason,
        )
        await session.commit()
        entry_data = {
            "ok": True,
            "created": created,
            "id": entry.id,
            "media_item_id": entry.media_item_id,
            "nzb_guid": entry.nzb_guid,
            "nzb_title": entry.nzb_title,
            "reason": entry.reason,
        }

    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(
            content='<span class="px-2.5 py-1 text-xs font-semibold rounded bg-zinc-800 text-zinc-400 border border-zinc-700">Blacklisted</span>',
            status_code=200,
        )

    return JSONResponse(status_code=201 if created else 200, content=entry_data)


@app.get("/settings/blacklist/clear-modal")
async def settings_blacklist_clear_modal(request: Request) -> Response:
    """Render confirmation modal for clearing all blacklisted releases."""
    from sqlalchemy import func, select

    from app.db.database import async_session_factory
    from app.db.models import BlacklistedRelease

    async with async_session_factory() as session:
        count = (
            await session.execute(select(func.count()).select_from(BlacklistedRelease))
        ).scalar_one()

    return templates.TemplateResponse(
        request=request,
        name="modals/clear_blacklist.html",
        context={"total_count": count},
    )


@app.post("/api/blacklist/clear")
async def clear_all_blacklisted_releases(request: Request) -> Response:
    """Delete all blacklisted releases."""
    from typing import cast

    from sqlalchemy import CursorResult, delete

    from app.db.database import async_session_factory
    from app.db.models import BlacklistedRelease

    async with async_session_factory() as session:
        result = cast(
            CursorResult[Any], await session.execute(delete(BlacklistedRelease))
        )
        deleted_count = result.rowcount or 0
        await session.commit()

    is_htmx = request.headers.get("HX-Request") == "true"
    accept_header = request.headers.get("Accept", "")

    if is_htmx or "application/json" not in accept_header:
        if is_htmx:
            resp = templates.TemplateResponse(
                request=request,
                name="partials/blacklist_slice.html",
                context={"blacklisted_releases": []},
            )
            resp.headers["HX-Trigger"] = json.dumps({"closeModal": True})
            return resp

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "deleted_count": deleted_count,
            "message": "All blacklisted releases cleared successfully.",
        },
    )


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


@app.get("/settings/preset/new/modal")
async def settings_preset_new_modal(request: Request) -> Response:
    """Render empty Search Preset creation dialog modal."""
    return templates.TemplateResponse(
        request=request,
        name="modals/preset_modal.html",
        context={
            "preset": None,
            "custom_config": {},
        },
    )


@app.get("/settings/preset/{preset_id}/edit/modal")
async def settings_preset_edit_modal(preset_id: int, request: Request) -> Response:
    """Render pre-filled Search Preset edit dialog modal."""
    from fastapi.responses import JSONResponse

    from app.db.database import async_session_factory
    from app.services.preset_service import get_preset

    async with async_session_factory() as session:
        preset = await get_preset(session, preset_id)
        if preset is None:
            return JSONResponse(status_code=404, content={"error": "Preset not found"})
        return templates.TemplateResponse(
            request=request,
            name="modals/preset_modal.html",
            context={
                "preset": preset,
                "custom_config": preset.custom_config,
            },
        )


async def _render_presets_list_or_json(
    session: Any, request: Request, json_content: Any, status_code: int = 200
) -> Response:
    """Helper to return presets_list.html partial for HTMX requests or JSON response."""
    from fastapi.responses import JSONResponse

    from app.services.preset_service import list_presets

    is_htmx = request.headers.get("HX-Request", "").strip().lower() == "true"
    if is_htmx:
        presets = await list_presets(session)
        response = templates.TemplateResponse(
            request=request,
            name="partials/presets_list.html",
            context={"presets": presets},
            status_code=status_code,
        )
        # Close any open modal container via HX-Trigger
        response.headers["HX-Trigger"] = '{"closePresetModal": true}'
        return response
    return JSONResponse(status_code=status_code, content=json_content)


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
            return await _render_presets_list_or_json(
                session, request, preset.to_dict(), status_code=201
            )
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
        return await _render_presets_list_or_json(
            session, request, preset.to_dict(), status_code=200
        )


@app.post("/api/presets/{preset_id}/default")
@app.post("/api/presets/{preset_id}/set-default")
async def api_set_default_preset(preset_id: int, request: Request) -> Response:
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
        return await _render_presets_list_or_json(
            session, request, preset.to_dict(), status_code=200
        )


@app.delete("/api/presets/{preset_id}")
async def api_delete_preset(preset_id: int, request: Request) -> Response:
    """Delete a SearchPreset and promote another preset to default if needed."""
    from fastapi.responses import JSONResponse

    from app.db.database import async_session_factory
    from app.services.preset_service import delete_preset

    async with async_session_factory() as session:
        deleted = await delete_preset(session, preset_id)
        if not deleted:
            return JSONResponse(status_code=404, content={"error": "Preset not found"})
        await session.commit()
        return await _render_presets_list_or_json(
            session, request, {"deleted": True, "id": preset_id}, status_code=200
        )


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


@app.post("/api/items/{item_id}/toggle-auto-push")
async def api_toggle_auto_push(item_id: int) -> JSONResponse:
    """Flip `MediaItem.auto_push_episodes` for a Series/Anime."""
    from app.db.database import async_session_factory
    from app.db.models import MediaItem, MediaType

    async with async_session_factory() as session:
        item = await session.get(MediaItem, item_id)
        if item is None:
            return JSONResponse(
                status_code=404, content={"error": "MediaItem not found"}
            )
        if item.media_type == MediaType.MOVIE:
            return JSONResponse(
                status_code=400,
                content={"error": "Auto-push is not available for movies"},
            )
        item.auto_push_episodes = not item.auto_push_episodes
        await session.commit()
        enabled = bool(item.auto_push_episodes)

    return JSONResponse(
        {"auto_push_episodes": enabled},
        headers={"HX-Trigger": '{"showAutoSaveToast": "Saved"}'},
    )


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


_background_push_tasks: set[asyncio.Task[Any]] = set()


async def wait_for_background_pushes() -> None:
    """Await any in-flight background Auto-Push tasks (used by tests and graceful shutdown)."""
    while _background_push_tasks:
        pending = list(_background_push_tasks)
        await asyncio.gather(*pending, return_exceptions=True)


async def cancel_in_flight_push_tasks(item_id: int | None = None) -> int:
    """Cancel running in-flight background Auto-Push tasks, release their locks, and unregister them.

    If item_id is specified, only tasks and locks matching that item_id are cancelled.
    Otherwise, all in-flight tasks and locks are cancelled.
    Returns the count of cancelled tasks.
    """
    from app.core.search_lock import item_lock_manager
    from app.core.transfer_poller import transfer_poller

    if item_id is not None:
        target_tasks = [
            t
            for t in list(_background_push_tasks)
            if getattr(t, "item_id", None) == item_id and not t.done()
        ]
        target_item_ids = [item_id]
    else:
        target_tasks = [t for t in list(_background_push_tasks) if not t.done()]
        target_item_ids = list(transfer_poller.in_flight_pushes.keys())

    canceled_count = len(target_tasks)
    for task in target_tasks:
        task.cancel()

    if target_tasks:
        await asyncio.gather(*target_tasks, return_exceptions=True)

    for iid in target_item_ids:
        transfer_poller.unregister_in_flight_push(iid)
        item_lock_manager.release(iid)

    return canceled_count


async def _run_background_auto_push(item_id: int, payload: dict[str, Any]) -> None:
    """Execute Auto-Push Best asynchronously in the background while retaining lock & in-flight state."""
    from app.core.push_engine import execute_auto_push
    from app.core.search_lock import item_lock_manager
    from app.core.transfer_poller import count_downloading_entities, transfer_poller
    from app.db.database import async_session_factory

    try:
        async with async_session_factory() as session:
            await execute_auto_push(session, item_id, payload)
    except Exception as exc:
        logger.exception("Background Auto-Push failed for item %s: %s", item_id, exc)
    finally:
        transfer_poller.unregister_in_flight_push(item_id)
        item_lock_manager.release(item_id)
        try:
            async with async_session_factory() as session:
                remaining = await count_downloading_entities(session)
                if remaining == 0 and not transfer_poller.has_in_flight_pushes:
                    transfer_poller.sleep(0)
        except (Exception, asyncio.CancelledError) as exc:
            logger.debug("Post-push poller sleep check skipped: %s", exc)


@app.post("/api/items/{item_id}/push/auto")
async def api_push_item_auto(item_id: int, request: Request) -> Response:
    """Execute Auto-Push Best (`push_mode = 'auto'`) in the background protected by MediaItemLockManager."""
    from fastapi.responses import JSONResponse

    from app.core.push_engine import persist_sticky_preferences
    from app.core.search_lock import create_conflict_response, item_lock_manager
    from app.core.transfer_poller import transfer_poller, wake_transfer_poller
    from app.db.database import async_session_factory
    from app.db.models import MediaItem

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
            item = await session.get(MediaItem, item_id)
            if item is None:
                item_lock_manager.release(item_id)
                return JSONResponse(
                    status_code=404,
                    content={
                        "pushed": False,
                        "error": f"MediaItem {item_id} not found",
                    },
                )
            await persist_sticky_preferences(session, item, payload)
            await session.commit()
            item_title = item.title
    except Exception:
        item_lock_manager.release(item_id)
        raise

    transfer_poller.register_in_flight_push(item_id, item_title)
    wake_transfer_poller()
    task = asyncio.create_task(_run_background_auto_push(item_id, payload))
    setattr(task, "item_id", item_id)
    _background_push_tasks.add(task)
    task.add_done_callback(_background_push_tasks.discard)

    return JSONResponse(
        status_code=200,
        content={
            "status": "started",
            "pushed": True,
            "push_mode": "auto",
            "item_id": item_id,
            "title": item_title,
            "message": f"Auto-Push started for '{item_title}'.",
        },
    )


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


# ---------------------------------------------------------------------------
# Card 5: Active Pushes & Split Failure Recovery API (v3.0.0)
# ---------------------------------------------------------------------------


def _build_poller_state_header(
    *,
    poller_awake: bool,
    active_pushes_count: int,
    downloading_count: int,
    in_flight_count: int,
    refresh_dashboard: bool = False,
    refresh_active_pushes: bool = False,
) -> dict[str, str]:
    payload: dict[str, Any] = {
        "nzboxer:poller-state": {
            "pollerAwake": poller_awake,
            "activePushesCount": active_pushes_count,
            "downloadingCount": downloading_count,
            "inFlightCount": in_flight_count,
        }
    }
    if refresh_dashboard:
        payload["refreshDashboard"] = True
    if refresh_active_pushes:
        payload["refreshActivePushes"] = True
    return {"HX-Trigger": json.dumps(payload)}


@app.get("/api/pushes/active")
async def api_get_active_pushes(request: Request) -> Response:
    """Return active transfers (`DOWNLOADING`), recently completed transfers (`Ready on TorBox`), and unacknowledged failed pushes (`FAILED`)."""
    from app.core.transfer_poller import (
        count_downloading_entities,
        get_active_pushes,
        run_transfer_poller_tick,
        transfer_poller,
    )
    from app.services import torbox

    force = (request.query_params.get("force") or "").lower() in ("1", "true", "yes")
    if force:
        torbox.clear_usenet_cache()

    async with async_session_factory() as session:
        downloading_count = await count_downloading_entities(session)
        if downloading_count > 0:
            try:
                live_downloads = await torbox.get_usenet_downloads(
                    bypass_cache=False, session=session
                )
                await run_transfer_poller_tick(
                    session, pre_fetched_downloads=live_downloads
                )
                downloading_count = await count_downloading_entities(session)
            except Exception as exc:
                logger.debug("Live active pushes refresh skipped: %s", exc)
        pushes = await get_active_pushes(session)

    in_flight_titles = transfer_poller.in_flight_titles
    poller_awake = (
        transfer_poller.is_awake or downloading_count > 0 or bool(in_flight_titles)
    )
    headers = _build_poller_state_header(
        poller_awake=poller_awake,
        active_pushes_count=len(pushes),
        downloading_count=downloading_count,
        in_flight_count=len(in_flight_titles),
    )

    rate_limited_count = sum(1 for p in pushes if p.get("is_rate_limited"))

    accept = (request.headers.get("accept") or "").lower()
    is_hx = request.headers.get("HX-Request") == "true"
    partial_tpl = Path("templates/partials/active_pushes_table.html")
    if is_hx and "application/json" not in accept and partial_tpl.exists():
        return templates.TemplateResponse(
            request=request,
            name="partials/active_pushes_table.html",
            context={
                "active_pushes": pushes,
                "rate_limited_count": rate_limited_count,
                "downloading_count": downloading_count,
                "in_flight_titles": in_flight_titles,
                "poller_awake": poller_awake,
            },
            headers=headers,
        )

    return JSONResponse(
        status_code=200,
        content={
            "pushes": pushes,
            "active_count": len(pushes),
            "rate_limited_count": rate_limited_count,
            "downloading_count": downloading_count,
            "in_flight_titles": in_flight_titles,
            "poller_awake": poller_awake,
        },
        headers=headers,
    )


@app.post("/api/pushes/{push_id}/dismiss")
async def api_dismiss_failed_push(push_id: str, request: Request) -> Response:
    """Dismiss a failed, completed, or synthetic dry-run push row from Active Pushes."""
    from app.core.transfer_poller import (
        count_downloading_entities,
        dismiss_dry_run_push,
        dismiss_failed_push,
        get_active_pushes,
        transfer_poller,
    )

    if push_id.startswith("dry-run-") or not push_id.isdigit():
        ok = dismiss_dry_run_push(push_id)
        if not ok:
            return JSONResponse(
                status_code=404, content={"ok": False, "error": "Push row not found"}
            )
        async with async_session_factory() as session:
            pushes = await get_active_pushes(session)
            downloading_count = await count_downloading_entities(session)
    else:
        history_id = int(push_id)
        async with async_session_factory() as session:
            ok = await dismiss_failed_push(session, history_id)
            if not ok:
                return JSONResponse(
                    status_code=404,
                    content={"ok": False, "error": "Push row not found"},
                )
            pushes = await get_active_pushes(session)
            downloading_count = await count_downloading_entities(session)

    in_flight_titles = transfer_poller.in_flight_titles
    poller_awake = (
        transfer_poller.is_awake or downloading_count > 0 or bool(in_flight_titles)
    )
    headers = _build_poller_state_header(
        poller_awake=poller_awake,
        active_pushes_count=len(pushes),
        downloading_count=downloading_count,
        in_flight_count=len(in_flight_titles),
        refresh_active_pushes=True,
    )

    if request.headers.get("HX-Request") == "true":
        rate_limited_count = sum(1 for p in pushes if p.get("is_rate_limited"))
        partial_tpl = Path("templates/partials/active_pushes_table.html")
        if partial_tpl.exists():
            return templates.TemplateResponse(
                request=request,
                name="partials/active_pushes_table.html",
                context={
                    "active_pushes": pushes,
                    "rate_limited_count": rate_limited_count,
                    "downloading_count": downloading_count,
                    "in_flight_titles": in_flight_titles,
                    "poller_awake": poller_awake,
                },
                headers=headers,
            )
        return HTMLResponse(content="", status_code=200, headers=headers)

    return JSONResponse(
        status_code=200,
        content={"ok": True, "push_id": push_id, "remaining_pushes": len(pushes)},
        headers=headers,
    )


@app.post("/api/pushes/dismiss-all")
async def api_dismiss_all_active_pushes(request: Request) -> Response:
    """Dismiss all non-active rows (completed, failed, dry-run) from Card 5 Active Pushes."""
    from app.core.transfer_poller import (
        count_downloading_entities,
        dismiss_all_active_pushes,
        get_active_pushes,
        transfer_poller,
    )

    async with async_session_factory() as session:
        dismissed_count = await dismiss_all_active_pushes(session)
        pushes = await get_active_pushes(session)
        downloading_count = await count_downloading_entities(session)

    in_flight_titles = transfer_poller.in_flight_titles
    poller_awake = (
        transfer_poller.is_awake or downloading_count > 0 or bool(in_flight_titles)
    )
    headers = _build_poller_state_header(
        poller_awake=poller_awake,
        active_pushes_count=len(pushes),
        downloading_count=downloading_count,
        in_flight_count=len(in_flight_titles),
        refresh_active_pushes=True,
    )

    if request.headers.get("HX-Request") == "true":
        rate_limited_count = sum(1 for p in pushes if p.get("is_rate_limited"))
        partial_tpl = Path("templates/partials/active_pushes_table.html")
        if partial_tpl.exists():
            return templates.TemplateResponse(
                request=request,
                name="partials/active_pushes_table.html",
                context={
                    "active_pushes": pushes,
                    "rate_limited_count": rate_limited_count,
                    "downloading_count": downloading_count,
                    "in_flight_titles": in_flight_titles,
                    "poller_awake": poller_awake,
                },
                headers=headers,
            )
        return HTMLResponse(content="", status_code=200, headers=headers)

    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "dismissed_count": dismissed_count,
            "remaining_pushes": len(pushes),
        },
        headers=headers,
    )


@app.post("/api/pushes/{history_id}/delete-torbox")
async def api_active_push_delete_torbox(history_id: int, request: Request) -> Response:
    """Delete a completed transfer from TorBox via Active Pushes, mark row 'deleted', and reset target to SEARCHING."""
    from app.core.transfer_poller import (
        count_downloading_entities,
        get_active_pushes,
        manual_delete_from_torbox,
        transfer_poller,
    )

    async with async_session_factory() as session:
        res = await manual_delete_from_torbox(session, history_id=history_id)
        if res.get("status") == "not_found":
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "status": "not_found",
                    "error": "Push row not found",
                },
            )
        pushes = await get_active_pushes(session)
        downloading_count = await count_downloading_entities(session)

    in_flight_titles = transfer_poller.in_flight_titles
    poller_awake = (
        transfer_poller.is_awake or downloading_count > 0 or bool(in_flight_titles)
    )
    headers = _build_poller_state_header(
        poller_awake=poller_awake,
        active_pushes_count=len(pushes),
        downloading_count=downloading_count,
        in_flight_count=len(in_flight_titles),
        refresh_dashboard=True,
    )

    if request.headers.get("HX-Request") == "true":
        rate_limited_count = sum(1 for p in pushes if p.get("is_rate_limited"))
        partial_tpl = Path("templates/partials/active_pushes_table.html")
        if partial_tpl.exists():
            return templates.TemplateResponse(
                request=request,
                name="partials/active_pushes_table.html",
                context={
                    "active_pushes": pushes,
                    "rate_limited_count": rate_limited_count,
                    "downloading_count": downloading_count,
                    "in_flight_titles": in_flight_titles,
                    "poller_awake": poller_awake,
                },
                headers=headers,
            )
        return HTMLResponse(content="", status_code=200, headers=headers)

    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "status": "ok",
            "history_id": history_id,
            "deleted_torbox_ids": res.get("deleted_torbox_ids", []),
            "remaining_pushes": len(pushes),
        },
        headers=headers,
    )


@app.post("/api/pushes/{history_id}/cancel")
@app.delete("/api/pushes/{history_id}")
async def api_cancel_active_push(history_id: int, request: Request) -> Response:
    """Cancel and delete an active transfer on TorBox and remove it from Active Pushes."""
    from app.core.transfer_poller import (
        cancel_active_push,
        count_downloading_entities,
        get_active_pushes,
        transfer_poller,
    )

    async with async_session_factory() as session:
        ok = await cancel_active_push(session, history_id)
        if not ok:
            return JSONResponse(
                status_code=404, content={"ok": False, "error": "Push row not found"}
            )
        pushes = await get_active_pushes(session)
        downloading_count = await count_downloading_entities(session)

    in_flight_titles = transfer_poller.in_flight_titles
    poller_awake = (
        transfer_poller.is_awake or downloading_count > 0 or bool(in_flight_titles)
    )
    headers = _build_poller_state_header(
        poller_awake=poller_awake,
        active_pushes_count=len(pushes),
        downloading_count=downloading_count,
        in_flight_count=len(in_flight_titles),
    )

    if request.headers.get("HX-Request") == "true":
        rate_limited_count = sum(1 for p in pushes if p.get("is_rate_limited"))
        partial_tpl = Path("templates/partials/active_pushes_table.html")
        if partial_tpl.exists():
            return templates.TemplateResponse(
                request=request,
                name="partials/active_pushes_table.html",
                context={
                    "active_pushes": pushes,
                    "rate_limited_count": rate_limited_count,
                    "downloading_count": downloading_count,
                    "in_flight_titles": in_flight_titles,
                    "poller_awake": poller_awake,
                },
                headers=headers,
            )
        return HTMLResponse(content="", status_code=200, headers=headers)

    return JSONResponse(
        status_code=200,
        content={"ok": True, "history_id": history_id, "remaining_pushes": len(pushes)},
        headers=headers,
    )


@app.post("/api/pushes/{history_id}/blacklist")
async def api_blacklist_active_push(history_id: int, request: Request) -> Response:
    """Blacklist release from Active Pushes, cancel/delete on TorBox or dismiss failed row, and reset target to SEARCHING."""
    from app.core.transfer_poller import (
        blacklist_from_active_pushes,
        count_downloading_entities,
        get_active_pushes,
        transfer_poller,
    )

    async with async_session_factory() as session:
        ok = await blacklist_from_active_pushes(session, history_id)
        if not ok:
            return JSONResponse(
                status_code=404, content={"ok": False, "error": "Push row not found"}
            )
        pushes = await get_active_pushes(session)
        downloading_count = await count_downloading_entities(session)

    in_flight_titles = transfer_poller.in_flight_titles
    poller_awake = (
        transfer_poller.is_awake or downloading_count > 0 or bool(in_flight_titles)
    )
    headers = _build_poller_state_header(
        poller_awake=poller_awake,
        active_pushes_count=len(pushes),
        downloading_count=downloading_count,
        in_flight_count=len(in_flight_titles),
        refresh_dashboard=True,
    )

    if request.headers.get("HX-Request") == "true":
        rate_limited_count = sum(1 for p in pushes if p.get("is_rate_limited"))
        partial_tpl = Path("templates/partials/active_pushes_table.html")
        if partial_tpl.exists():
            return templates.TemplateResponse(
                request=request,
                name="partials/active_pushes_table.html",
                context={
                    "active_pushes": pushes,
                    "rate_limited_count": rate_limited_count,
                    "downloading_count": downloading_count,
                    "in_flight_titles": in_flight_titles,
                    "poller_awake": poller_awake,
                },
                headers=headers,
            )
        return HTMLResponse(content="", status_code=200, headers=headers)

    return JSONResponse(
        status_code=200,
        content={"ok": True, "history_id": history_id, "remaining_pushes": len(pushes)},
        headers=headers,
    )


@app.post("/api/pushes/abort-all")
async def api_abort_all_transfers(request: Request) -> Response:
    """Abort all active downloading and rate-limited transfers across TorBox and database."""
    from app.core.transfer_poller import (
        abort_all_active_transfers,
        count_downloading_entities,
        get_active_pushes,
        transfer_poller,
    )

    async with async_session_factory() as session:
        res = await abort_all_active_transfers(session)
        pushes = await get_active_pushes(session)
        downloading_count = await count_downloading_entities(session)

    in_flight_titles = transfer_poller.in_flight_titles
    poller_awake = (
        transfer_poller.is_awake or downloading_count > 0 or bool(in_flight_titles)
    )
    headers = _build_poller_state_header(
        poller_awake=poller_awake,
        active_pushes_count=len(pushes),
        downloading_count=downloading_count,
        in_flight_count=len(in_flight_titles),
        refresh_dashboard=True,
    )

    if request.headers.get("HX-Request") == "true":
        rate_limited_count = sum(1 for p in pushes if p.get("is_rate_limited"))
        partial_tpl = Path("templates/partials/active_pushes_table.html")
        if partial_tpl.exists():
            return templates.TemplateResponse(
                request=request,
                name="partials/active_pushes_table.html",
                context={
                    "active_pushes": pushes,
                    "rate_limited_count": rate_limited_count,
                    "downloading_count": downloading_count,
                    "in_flight_titles": in_flight_titles,
                    "poller_awake": poller_awake,
                },
                headers=headers,
            )
        return HTMLResponse(content="", status_code=200, headers=headers)

    return JSONResponse(
        status_code=200,
        content=res,
        headers=headers,
    )


@app.post("/api/pushes/resume-rate-limited")
async def api_resume_rate_limited_pushes(request: Request) -> Response:
    """Clear all TorBox and indexer cooldowns, wake the queue, and trigger an immediate resume tick."""
    from app.core.rate_limit_queue import (
        clear_all_rate_limit_cooldowns,
        rate_limit_queue,
        run_rate_limit_resume_tick,
    )
    from app.core.transfer_poller import (
        count_downloading_entities,
        get_active_pushes,
        transfer_poller,
    )

    clear_all_rate_limit_cooldowns()
    rate_limit_queue.wake()
    await run_rate_limit_resume_tick()

    if request.headers.get("HX-Request") == "true":
        async with async_session_factory() as session:
            pushes = await get_active_pushes(session)
            downloading_count = await count_downloading_entities(session)

        rate_limited_count = sum(1 for p in pushes if p.get("is_rate_limited"))
        in_flight_titles = transfer_poller.in_flight_titles
        poller_awake = (
            transfer_poller.is_awake or downloading_count > 0 or bool(in_flight_titles)
        )
        headers = _build_poller_state_header(
            poller_awake=poller_awake,
            active_pushes_count=len(pushes),
            downloading_count=downloading_count,
            in_flight_count=len(in_flight_titles),
            refresh_dashboard=True,
        )

        partial_tpl = Path("templates/partials/active_pushes_table.html")
        if partial_tpl.exists():
            return templates.TemplateResponse(
                request=request,
                name="partials/active_pushes_table.html",
                context={
                    "active_pushes": pushes,
                    "rate_limited_count": rate_limited_count,
                    "downloading_count": downloading_count,
                    "in_flight_titles": in_flight_titles,
                    "poller_awake": poller_awake,
                },
                headers=headers,
            )
        return HTMLResponse(content="", status_code=200, headers=headers)

    return JSONResponse(
        status_code=200,
        content={"status": "resumed", "cooldowns_cleared": True},
    )


@app.post("/api/pushes/poll")
async def api_trigger_transfer_poller_tick() -> Response:
    """Trigger an immediate transfer poller tick."""
    from app.core.transfer_poller import run_transfer_poller_tick

    async with async_session_factory() as session:
        res = await run_transfer_poller_tick(session)
    return JSONResponse(status_code=200, content=res)


# ---------------------------------------------------------------------------
# Card 4: Push History Ledger & Dual-Mode TorBox Deletion API (v3.0.0)
# ---------------------------------------------------------------------------


@app.get("/api/history")
async def api_get_push_history(request: Request) -> Response:
    """Return the Push History Ledger (DownloadHistory rows) ordered by most recent push."""
    from app.core.transfer_poller import (
        autoclear_history_ledger,
        get_push_history_ledger,
    )

    async with async_session_factory() as session:
        await autoclear_history_ledger(session)
        ledger = await get_push_history_ledger(session)

    return JSONResponse(
        status_code=200,
        content={"history": ledger, "total": len(ledger)},
    )


@app.post("/api/history/{history_id}/delete-torbox")
async def api_history_delete_torbox(history_id: int, request: Request) -> Response:
    """Delete transfer from TorBox via Push History Ledger row, mark row 'deleted', reset target to SEARCHING, and recalculate parent status."""
    from app.core.transfer_poller import manual_delete_from_torbox

    async with async_session_factory() as session:
        res = await manual_delete_from_torbox(session, history_id=history_id)
        if res.get("status") == "not_found":
            return JSONResponse(
                status_code=404,
                content={"status": "not_found", "error": "History entry not found"},
            )

    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(
            content='<span class="px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider rounded bg-zinc-500/15 text-zinc-400 border border-zinc-500/30">DELETED</span>',
            status_code=200,
            headers={"HX-Trigger": "refreshDashboard"},
        )

    return JSONResponse(status_code=200, content=res)


@app.post("/api/history/{history_id}/blacklist")
async def api_history_blacklist(history_id: int, request: Request) -> Response:
    """Blacklist release from Push History Ledger row, delete from TorBox if present, mark row 'deleted', and reset target to SEARCHING."""
    from app.core.transfer_poller import blacklist_from_history

    async with async_session_factory() as session:
        res = await blacklist_from_history(session, history_id=history_id)
        if res.get("status") == "not_found":
            return JSONResponse(
                status_code=404,
                content={"status": "not_found", "error": "History entry not found"},
            )

    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(
            content='<span class="px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider rounded bg-zinc-500/15 text-zinc-400 border border-zinc-500/30">DELETED</span>',
            status_code=200,
            headers={"HX-Trigger": "refreshDashboard"},
        )

    return JSONResponse(status_code=200, content=res)


@app.delete("/api/history/{history_id}")
async def api_delete_history_entry(history_id: int, request: Request) -> Response:
    """Delete a DownloadHistory ledger row from SQLite and reconcile parent status if zero transfers remain."""
    from app.core.transfer_poller import _recalculate_parent_status
    from app.db.models import DownloadHistory, MediaItem

    async with async_session_factory() as session:
        row = await session.get(DownloadHistory, history_id)
        if row is None:
            return JSONResponse(
                status_code=404,
                content={"deleted": False, "error": "History entry not found"},
            )
        media_item_id = row.media_item_id
        await session.delete(row)
        await session.flush()

        if media_item_id is not None:
            item = await session.get(MediaItem, media_item_id)
            if item is not None:
                await _recalculate_parent_status(session, item)

        await session.commit()

    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(
            content="",
            status_code=200,
            headers={"HX-Trigger": "refreshDashboard"},
        )

    return JSONResponse(
        status_code=200,
        content={"deleted": True, "id": history_id},
        headers={"HX-Trigger": "refreshDashboard"},
    )


@app.post("/api/history/batch-delete")
async def api_batch_delete_history_entries(request: Request) -> Response:
    """Batch delete DownloadHistory ledger rows from SQLite and reconcile parent statuses."""
    from app.core.transfer_poller import batch_delete_history_entries

    raw_ids: list[Any] = []
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            if isinstance(body, dict):
                raw_ids = body.get("history_ids", [])
            elif isinstance(body, list):
                raw_ids = body
        except Exception:
            raw_ids = []
    else:
        try:
            form = await request.form()
            raw_ids = form.getlist("history_ids")
            if not raw_ids and "history_ids" in form:
                val = form.get("history_ids")
                if val:
                    raw_ids = [val]
        except Exception:
            raw_ids = []

    history_ids: list[int] = []
    for item in raw_ids:
        try:
            history_ids.append(int(item))
        except (ValueError, TypeError):
            continue

    async with async_session_factory() as session:
        res = await batch_delete_history_entries(session, history_ids)

    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(
            content="",
            status_code=200,
            headers={"HX-Trigger": "refreshDashboard"},
        )

    return JSONResponse(
        status_code=200,
        content=res,
        headers={"HX-Trigger": "refreshDashboard"},
    )


@app.post("/api/history/clear")
async def api_clear_history_ledger(request: Request) -> Response:
    """Clear all DownloadHistory ledger rows from SQLite and reconcile parent statuses."""
    from app.core.transfer_poller import clear_history_ledger

    async with async_session_factory() as session:
        res = await clear_history_ledger(session)

    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(
            content="",
            status_code=200,
            headers={"HX-Trigger": "refreshDashboard"},
        )

    return JSONResponse(
        status_code=200,
        content=res,
        headers={"HX-Trigger": "refreshDashboard"},
    )


@app.post("/api/history/delete-dry-run")
async def api_delete_dry_run_history(request: Request) -> Response:
    """Delete all simulated dry-run DownloadHistory ledger rows from SQLite."""
    from app.core.transfer_poller import delete_dry_run_history

    async with async_session_factory() as session:
        res = await delete_dry_run_history(session)

    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(
            content="",
            status_code=200,
            headers={"HX-Trigger": "refreshDashboard"},
        )

    return JSONResponse(
        status_code=200,
        content=res,
        headers={"HX-Trigger": "refreshDashboard"},
    )


@app.post("/api/items/{item_id}/delete-torbox")
async def api_item_delete_torbox(
    item_id: int,
    request: Request,
    season_id: int | None = None,
    episode_id: int | None = None,
) -> Response:
    """Delete transfer from TorBox for a Movie, Season/Franchise Movie, or Episode from the Push Modal."""
    from app.core.push_engine import get_push_modal_context
    from app.core.transfer_poller import manual_delete_from_torbox

    payload = await _parse_request_payload(request)
    if season_id is None and payload.get("season_id") is not None:
        try:
            season_id = int(payload["season_id"])
        except (TypeError, ValueError):
            season_id = None
    if episode_id is None and payload.get("episode_id") is not None:
        try:
            episode_id = int(payload["episode_id"])
        except (TypeError, ValueError):
            episode_id = None

    async with async_session_factory() as session:
        res = await manual_delete_from_torbox(
            session,
            item_id=item_id,
            season_id=season_id,
            episode_id=episode_id,
        )
        if request.headers.get("HX-Request") == "true":
            ctx = await get_push_modal_context(session, item_id)
            if ctx is not None:
                return templates.TemplateResponse(
                    request=request,
                    name="modals/push_modal.html",
                    context=ctx,
                    headers={"HX-Trigger": "refreshDashboard"},
                )
            return HTMLResponse(
                content="",
                status_code=200,
                headers={"HX-Trigger": "refreshDashboard"},
            )

    return JSONResponse(status_code=200, content=res)


@app.post("/api/items/{item_id}/abort-transfer")
async def api_item_abort_transfer(
    item_id: int,
    request: Request,
    season_id: int | None = None,
    episode_id: int | None = None,
) -> Response:
    """Abort an active or rate-limited transfer for a Movie, Season, or Episode from the Push Modal."""
    from app.core.push_engine import get_push_modal_context
    from app.core.transfer_poller import abort_item_transfer

    payload = await _parse_request_payload(request)
    if season_id is None and payload.get("season_id") is not None:
        try:
            season_id = int(payload["season_id"])
        except (TypeError, ValueError):
            season_id = None
    if episode_id is None and payload.get("episode_id") is not None:
        try:
            episode_id = int(payload["episode_id"])
        except (TypeError, ValueError):
            episode_id = None

    async with async_session_factory() as session:
        res = await abort_item_transfer(
            session,
            item_id=item_id,
            season_id=season_id,
            episode_id=episode_id,
        )
        if request.headers.get("HX-Request") == "true":
            ctx = await get_push_modal_context(session, item_id)
            if ctx is not None:
                return templates.TemplateResponse(
                    request=request,
                    name="modals/push_modal.html",
                    context=ctx,
                    headers={"HX-Trigger": "refreshDashboard"},
                )
            return HTMLResponse(
                content="",
                status_code=200,
                headers={"HX-Trigger": "refreshDashboard"},
            )

    return JSONResponse(
        status_code=200,
        content=res,
        headers={"HX-Trigger": "refreshDashboard"},
    )
