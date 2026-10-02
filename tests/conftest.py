"""
Shared Test Fixtures & Domain Factories
=======================================
Provides a centralized in-memory SQLite database, session factory, ASGI HTTP test
client, state resets, and reusable domain entity fixtures for all tests.

Domain Factory Helpers (importable async functions)
----------------------------------------------------
- VALID_NZB_BYTES          : Minimal NZB XML bytes that pass Layer 2 inspection.
- make_movie(session, **overrides) -> MediaItem
- make_series(session, *, seasons, episodes_per_season, episode_statuses, **overrides)
      -> tuple[MediaItem, list[Season], list[Episode]]
- make_history(session, *, item, season, episode, **overrides) -> DownloadHistory
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db.database import close_db, get_engine, get_session_factory, init_db
from app.db.models import (
    Base,
    DownloadHistory,
    Episode,
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    MediaType,
    Provider,
    SearchPreset,
    Season,
    SeasonStatus,
)
from app.main import app
from app.services import simkl, torbox

# Override DB URL for tests
settings.database_url = "sqlite+aiosqlite:///:memory:"

# ---------------------------------------------------------------------------
# Shared constant: minimal NZB XML that passes Layer 2 fake detection
# ---------------------------------------------------------------------------
VALID_NZB_BYTES: bytes = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
    b'  <file poster="test" date="1700000000"'
    b'        subject="Test.Release.1080p.WEB-DL.mkv (1/1)">'
    b"    <groups><group>alt.binaries.test</group></groups>"
    b"    <segments>"
    b'      <segment bytes="500000000" number="1">seg1@test</segment>'
    b"    </segments>"
    b"  </file>"
    b"</nzb>"
)


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def reset_torbox_state():
    torbox.set_cooldown(0.0)
    torbox.clear_usenet_cache()
    simkl._SIMKL_DEVICE_SESSIONS.clear()
    yield
    torbox.set_cooldown(0.0)
    torbox.clear_usenet_cache()
    simkl._SIMKL_DEVICE_SESSIONS.clear()


@pytest.fixture(autouse=True)
async def setup_db():
    await init_db(settings.database_url)
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF;")
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await conn.exec_driver_sql("PRAGMA foreign_keys = ON;")
    yield
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF;")
        await conn.run_sync(Base.metadata.drop_all)
    await close_db()


@pytest.fixture
def session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the shared async session factory bound to the in-memory test database."""
    return get_session_factory()


@pytest.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Yield a managed AsyncSession bound to the shared in-memory test database."""
    async with session_factory() as session:
        yield session


@pytest.fixture
async def async_client() -> AsyncGenerator[AsyncClient, None]:
    """Yield a shared ASGI HTTPX AsyncClient targeting the FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def watchlist_provider(db_session: AsyncSession) -> Provider:
    """Seed and return an active Simkl watchlist Provider."""
    provider = Provider(
        name="Simkl",
        type="simkl",
        category="watchlist",
        is_active=True,
    )
    db_session.add(provider)
    await db_session.commit()
    await db_session.refresh(provider)
    return provider


@pytest.fixture
async def default_search_preset(db_session: AsyncSession) -> SearchPreset:
    """Seed and return the default 'Default (Best)' SearchPreset."""
    preset = SearchPreset(
        name="Default (Best)",
        is_default=True,
        primary_language="en",
        fallback_language=None,
        video_quality_mode="best",
        audio_quality_mode="best",
        allow_season_packs=False,
        prefer_season_packs=False,
        custom_config_json="{}",
    )
    db_session.add(preset)
    await db_session.commit()
    await db_session.refresh(preset)
    return preset


# ---------------------------------------------------------------------------
# Domain Entity Factory Helpers
# ---------------------------------------------------------------------------


async def _ensure_watchlist_provider(
    session: AsyncSession, provider_id: int | None
) -> int:
    """Return *provider_id* unchanged, or seed and return a default watchlist Provider's id."""
    if provider_id is not None:
        return provider_id
    from sqlalchemy import select

    existing = (
        (
            await session.execute(
                select(Provider).where(Provider.category == "watchlist").limit(1)
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing.id
    provider = Provider(
        name="Simkl", type="simkl", category="watchlist", is_active=True
    )
    session.add(provider)
    await session.flush()
    return provider.id  # type: ignore[return-value]


async def make_movie(session: AsyncSession, **overrides: Any) -> MediaItem:
    """Create, flush, and return a ``MediaItem`` with ``media_type=MOVIE``.

    Sensible defaults (all overridable via keyword arguments):

    .. code-block:: python

        title="Test Movie"
        year=2024
        status=MediaStatus.SEARCHING
        simkl_id=10001
        provider_id=<auto-seeded watchlist provider>
    """
    provider_id = await _ensure_watchlist_provider(
        session, overrides.pop("provider_id", None)
    )
    defaults: dict[str, Any] = dict(
        provider_id=provider_id,
        simkl_id=10001,
        title="Test Movie",
        year=2024,
        media_type=MediaType.MOVIE,
        status=MediaStatus.SEARCHING,
    )
    defaults.update(overrides)
    item = MediaItem(**defaults)
    session.add(item)
    await session.flush()
    return item


async def make_series(
    session: AsyncSession,
    *,
    seasons: list[dict[str, Any]] | int = 1,
    episodes_per_season: int = 0,
    episode_statuses: list[EpisodeStatus] | None = None,
    **overrides: Any,
) -> tuple[MediaItem, list[Season], list[Episode]]:
    """Create, flush, and return a ``MediaItem`` (SHOW or ANIME), its Season rows,
    and any requested Episode rows.

    Parameters
    ----------
    seasons:
        • ``int`` — create *N* seasons with sequential ``season_number``,
          ``watch_order``, and ``type_number`` (1-based), all ``monitored=True``,
          ``status=SeasonStatus.SEARCHING``.
        • ``list[dict]`` — each dict is passed directly as keyword arguments to
          ``Season(...)``.  Missing keys fall back to the per-season defaults above.
    episodes_per_season:
        How many ``Episode`` rows to create per season (skipped when 0).
    episode_statuses:
        Optional list of ``EpisodeStatus`` values used round-robin across all
        episodes when *episodes_per_season* > 0.  Defaults to ``SEARCHING``.
    **overrides:
        Forwarded to ``MediaItem(...)`` after applying defaults.

    Returns
    -------
    tuple[MediaItem, list[Season], list[Episode]]
        The parent item, a list of ``Season`` objects (in creation order), and a
        flat list of all ``Episode`` objects (all seasons, in creation order).
    """
    provider_id = await _ensure_watchlist_provider(
        session, overrides.pop("provider_id", None)
    )
    item_defaults: dict[str, Any] = dict(
        provider_id=provider_id,
        simkl_id=20001,
        title="Test Series",
        year=2024,
        media_type=MediaType.SHOW,
        status=MediaStatus.SEARCHING,
    )
    item_defaults.update(overrides)
    item = MediaItem(**item_defaults)
    session.add(item)
    await session.flush()

    # Normalise *seasons* argument
    season_specs: list[dict[str, Any]]
    if isinstance(seasons, int):
        season_specs = [{"season_number": n} for n in range(1, seasons + 1)]
    else:
        season_specs = list(seasons)

    season_objs: list[Season] = []
    episode_objs: list[Episode] = []
    statuses = episode_statuses or [EpisodeStatus.SEARCHING]

    for idx, spec in enumerate(season_specs, start=1):
        season_defaults: dict[str, Any] = dict(
            media_item_id=item.id,
            season_number=idx,
            watch_order=idx,
            type_number=idx,
            entry_type="season",
            title=f"Season {idx}",
            monitored=True,
            status=SeasonStatus.SEARCHING,
            episode_count=episodes_per_season if episodes_per_season else None,
        )
        season_defaults.update(spec)
        # Ensure media_item_id is always correct even when caller passes season_number
        season_defaults["media_item_id"] = item.id
        s = Season(**season_defaults)
        session.add(s)
        await session.flush()
        season_objs.append(s)

        for ep_num in range(1, episodes_per_season + 1):
            ep_status = statuses[(ep_num - 1) % len(statuses)]
            ep = Episode(
                season_id=s.id,
                episode_number=ep_num,
                monitored=True,
                status=ep_status,
            )
            session.add(ep)
            episode_objs.append(ep)

    if episode_objs:
        await session.flush()

    return item, season_objs, episode_objs


async def make_history(
    session: AsyncSession,
    *,
    item: MediaItem | None = None,
    season: Season | None = None,
    episode: Episode | None = None,
    **overrides: Any,
) -> DownloadHistory:
    """Create, flush, and return a ``DownloadHistory`` row.

    At least one of *item*, *season*, or *episode* should be provided so the
    history row is linked to a real entity.  All fields accept ``**overrides``.

    Sensible defaults:

    .. code-block:: python

        nzb_title="Test.Release.2024.1080p.WEB-DL.H264-GRP"
        nzb_guid="guid-hist-default"
        torbox_id="tb-9000"
        score=8500.0
        size_bytes=8 * 1024 ** 3  # 8 GB
        status_detail="completed"
        is_dismissed=True
        push_mode="auto"
    """
    defaults: dict[str, Any] = dict(
        media_item_id=item.id if item is not None else None,
        season_id=season.id if season is not None else None,
        episode_id=episode.id if episode is not None else None,
        nzb_title="Test.Release.2024.1080p.WEB-DL.H264-GRP",
        nzb_guid="guid-hist-default",
        torbox_id="tb-9000",
        score=8500.0,
        size_bytes=8 * 1024**3,
        status_detail="completed",
        is_dismissed=True,
        push_mode="auto",
    )
    defaults.update(overrides)
    hist = DownloadHistory(**defaults)
    session.add(hist)
    await session.flush()
    return hist
