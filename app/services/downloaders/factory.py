"""Downloader client factory (m57, ADR-131).

Client modules are imported lazily so the factory works before every
adapter module exists and avoids import cycles with the adapter modules.
"""

from __future__ import annotations

from importlib import import_module

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MediaType, Provider, ProviderCategory
from app.services.downloaders.base import BaseDownloader
from app.services.provider_service import get_active_downloader

# provider-specific code (allowlisted)
_CLIENTS: dict[str, tuple[str, str]] = {
    "torbox": ("app.services.downloaders.torbox", "TorBoxDownloader"),
    "sabnzbd": ("app.services.downloaders.sabnzbd", "SabnzbdDownloader"),
    "premiumize": ("app.services.downloaders.premiumize", "PremiumizeDownloader"),
}


def downloader_class(downloader_type: str) -> type[BaseDownloader]:
    """Adapter class for a downloader type (no provider row needed)."""
    entry = _CLIENTS.get(downloader_type)
    if entry is None:
        raise ValueError(f"Unsupported downloader type: {downloader_type}")
    module_name, class_name = entry
    cls: type[BaseDownloader] = getattr(import_module(module_name), class_name)
    return cls


def downloader_classes() -> dict[str, type[BaseDownloader]]:
    """All registered adapter classes keyed by downloader type."""
    return {t: downloader_class(t) for t in _CLIENTS}


def get_downloader_client(
    provider: Provider, session: AsyncSession | None = None
) -> BaseDownloader:
    """provider.type -> client. Raises ValueError for unknown types."""
    client: BaseDownloader = downloader_class(provider.type)(provider)  # type: ignore[call-arg]
    if session is not None:
        client.session = session
    return client


async def get_active_downloader_client(session: AsyncSession) -> BaseDownloader | None:
    """Client for the active downloader; None if none is active."""
    provider = await get_active_downloader(session)
    return get_downloader_client(provider, session) if provider else None


async def get_downloader_client_for_type(
    session: AsyncSession, downloader_type: str
) -> BaseDownloader | None:
    """Client for the (active-or-inactive) provider row of that type."""
    stmt = (
        select(Provider)
        .where(
            Provider.category == ProviderCategory.DOWNLOADER.value,
            Provider.type == downloader_type,
        )
        .order_by(Provider.is_active.desc(), Provider.priority.asc(), Provider.id)
        .limit(1)
    )
    provider = (await session.execute(stmt)).scalars().first()
    return get_downloader_client(provider) if provider else None


def category_for_media(
    provider: Provider, media_type: MediaType | str, is_anime: bool
) -> str | None:
    """Map a push target to a SABnzbd category (anime > series > movie).

    Returns None for non-SABnzbd providers or when the configured value is ''.
    """
    if provider.type != "sabnzbd":
        return None
    config = provider.sabnzbd_config
    media = media_type.value if isinstance(media_type, MediaType) else str(media_type)
    if is_anime or media == MediaType.ANIME.value:
        key = "category_anime"
    elif media == MediaType.MOVIE.value:
        key = "category_movie"
    else:
        key = "category_series"
    return config[key] or None
