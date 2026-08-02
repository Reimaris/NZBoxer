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
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import reload_settings_from_db, scoring_config, settings
from app.db.database import async_session_factory, close_db, init_db

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifecycle manager."""
    # 1. Initialize Database
    await init_db(settings.database_url)

    # 1.5 Load settings from DB into memory
    async with async_session_factory() as session:
        await reload_settings_from_db(session)

    # 2. Setup and Start APScheduler
    interval_minutes = scoring_config.get("automation", {}).get("search_interval_minutes", 30)
    
    # We will import automation here to avoid circular imports if automation relies on DB setup
    from app.core.automation import run_automation_cycle
    
    scheduler.add_job(
        run_automation_cycle,
        "interval",
        minutes=interval_minutes,
        id="automation_cycle",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("APScheduler started. Automation cycle set to %d minutes.", interval_minutes)

    # Yield control to the FastAPI application
    yield

    # 3. Shutdown gracefully
    scheduler.shutdown(wait=False)
    logger.info("APScheduler shut down.")
    await close_db()


# FastAPI App
app = FastAPI(
    title="NZBoxer",
    description="Automated Usenet NZB search and scoring.",
    version="0.1.0",
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
    from app.db.models import MediaItem
    
    async with async_session_factory() as session:
        stmt = select(MediaItem).order_by(MediaItem.created_at.desc()).options(selectinload(MediaItem.seasons))
        result = await session.execute(stmt)
        items = result.scalars().all()

    return templates.TemplateResponse(
        request=request, name="dashboard.html", context={"items": items}
    )

@app.get("/items/{item_id}", response_class=HTMLResponse)
async def item_detail(request: Request, item_id: int):
    """Detailed view for a single item (shows seasons if it's a series)."""
    from sqlalchemy.orm import selectinload

    from app.db.database import async_session_factory
    from app.db.models import MediaItem
    
    async with async_session_factory() as session:
        item = await session.get(MediaItem, item_id, options=[selectinload(MediaItem.seasons), selectinload(MediaItem.download_history)])
        
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
        stmt = select(Season).where(Season.media_item_id == item_id, Season.season_number == season_number)
        result = await session.execute(stmt)
        season = result.scalar_one_or_none()
        
        if season:
            season.monitored = not season.monitored
            # Return updated button html
            is_monitored = season.monitored
            await session.commit()
            
            color = "bg-green-600" if is_monitored else "bg-gray-600"
            text = "Monitored" if is_monitored else "Ignored"
            return HTMLResponse(content=f'<button hx-post="/items/{item_id}/seasons/{season_number}/toggle" hx-swap="outerHTML" class="{color} text-white px-3 py-1 rounded text-sm">{text}</button>')
            
    return HTMLResponse(content="Error", status_code=400)

@app.post("/sync")
async def sync_watchlist():
    """Trigger manual Simkl sync."""
    from app.core.automation import sync_simkl_watchlist
    await sync_simkl_watchlist()
    return HTMLResponse(content='<div class="p-4 bg-green-900 text-green-100 rounded">Sync complete! Reload the page.</div>')

@app.get("/search")
async def search_nzbs(item_id: int):
    """Manual NZB search returning HTMX partial of results."""
    # TODO: Run search, parse, score, and return results HTML
    return HTMLResponse(content="<div>Search Results...</div>")

@app.post("/download")
async def trigger_download(nzb_url: str):
    """Send an NZB to TorBox manually."""
    # TODO: Send to TorBox
    return HTMLResponse(content="<div>Sent to TorBox!</div>")

@app.get("/api/status")
async def get_status():
    """Health check and scheduler status."""
    return {
        "status": "ok",
        "scheduler_running": scheduler.running,
        "jobs": [job.id for job in scheduler.get_jobs()]
    }

@app.get("/settings", response_class=HTMLResponse)
async def get_settings_page(request: Request):
    """Render the settings form."""
    from sqlalchemy import select

    from app.db.models import SystemSettings
    
    async with async_session_factory() as session:
        stmt = select(SystemSettings).where(SystemSettings.id == 1)
        result = await session.execute(stmt)
        db_settings = result.scalar_one_or_none()
        
    return templates.TemplateResponse(
        request=request, name="settings.html", context={"db_settings": db_settings}
    )

from fastapi import Form


@app.post("/settings", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    simkl_client_id: str = Form(""),
    simkl_access_token: str = Form(""),
    tmdb_api_key: str = Form(""),
    treasure_maps_url: str = Form(""),
    treasure_maps_api_key: str = Form(""),
    torbox_api_key: str = Form(""),
    scoring_config_yaml: str = Form("")
):
    """Save settings and update global cache."""
    import yaml
    from sqlalchemy import select

    from app.db.models import SystemSettings
    
    # Validate YAML before saving
    try:
        yaml.safe_load(scoring_config_yaml)
    except yaml.YAMLError as e:
        return HTMLResponse(content=f'<div class="p-4 mb-4 bg-red-900 text-red-100 rounded">Invalid YAML: {e}</div>')

    async with async_session_factory() as session:
        stmt = select(SystemSettings).where(SystemSettings.id == 1)
        result = await session.execute(stmt)
        db_settings = result.scalar_one_or_none()
        
        if db_settings:
            db_settings.simkl_client_id = simkl_client_id
            db_settings.simkl_access_token = simkl_access_token
            db_settings.tmdb_api_key = tmdb_api_key
            db_settings.treasure_maps_url = treasure_maps_url
            db_settings.treasure_maps_api_key = treasure_maps_api_key
            db_settings.torbox_api_key = torbox_api_key
            db_settings.scoring_config_yaml = scoring_config_yaml
            
            await session.commit()
            await reload_settings_from_db(session)
            
    return HTMLResponse(content='<div class="p-4 mb-4 bg-green-900 text-green-100 rounded">Settings saved successfully!</div>')
