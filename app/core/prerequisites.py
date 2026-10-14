"""Setup status: which prerequisites are still missing to run the app (m64)."""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import async_session_factory
from app.db.models import Provider, ProviderCategory
from app.services import tmdb

logger = logging.getLogger(__name__)

# Background jobs honour the guard unless a test switches it off (m64).
JOB_GUARD_ENABLED: bool = True
_last_blocked: dict[str, tuple[str, ...]] = {}


@dataclass(frozen=True)
class SetupStatus:
    missing_core: tuple[str, ...]  # subset of ("tmdb", "indexer", "downloader")
    has_watchlist: bool

    @property
    def core_ready(self) -> bool:
        return not self.missing_core

    @property
    def watchlist_ready(self) -> bool:
        return self.core_ready and self.has_watchlist


def _provider_configured(p: Provider) -> bool:
    """Active provider with credentials (``api_key``, or ``api_url`` for SABnzbd)."""
    if not p.is_active:
        return False
    if (p.type or "").lower() == "sabnzbd":
        return bool((p.api_url or "").strip())
    return bool((p.api_key or "").strip())


async def get_setup_status(session: AsyncSession) -> SetupStatus:
    providers = (await session.execute(select(Provider))).scalars().all()
    missing: list[str] = []
    if not await tmdb.resolve_api_key(session):
        missing.append("tmdb")
    for category in (ProviderCategory.INDEXER, ProviderCategory.DOWNLOADER):
        if not any(
            p.category == category.value and _provider_configured(p) for p in providers
        ):
            missing.append(category.value)
    has_watchlist = any(
        p.category == ProviderCategory.WATCHLIST.value and p.sync_eligible
        for p in providers
    )
    return SetupStatus(tuple(missing), has_watchlist)


async def setup_blocks(job: str, *, watchlist: bool = False) -> SetupStatus | None:
    """Return the status when *job* must be skipped (core missing, or no watchlist if ``watchlist``)."""
    if not JOB_GUARD_ENABLED:
        return None
    async with async_session_factory() as session:
        status = await get_setup_status(session)
    missing = status.missing_core
    if not missing and watchlist and not status.has_watchlist:
        missing = ("watchlist",)
    if not missing:
        _last_blocked.pop(job, None)
        return None
    level = logging.INFO if _last_blocked.get(job) != missing else logging.DEBUG
    _last_blocked[job] = missing
    logger.log(
        level,
        "[GUARD] Setup incomplete (missing: %s): %s skipped",
        ", ".join(missing),
        job,
    )
    return status
