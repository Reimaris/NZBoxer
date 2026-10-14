"""Setup status: which prerequisites are still missing to run the app (m64)."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Provider, ProviderCategory
from app.services import tmdb


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
