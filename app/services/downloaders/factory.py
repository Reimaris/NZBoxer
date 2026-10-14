"""Downloader client factory (m57, ADR-131).

Client modules are imported lazily so the factory works before every
adapter module exists and avoids import cycles with ``app.services.torbox``.
"""

from __future__ import annotations

import json
from importlib import import_module

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MediaType, Provider, ProviderCategory
from app.services.downloaders.base import BaseDownloader
from app.services.provider_service import get_active_downloader

_CLIENTS: dict[str, tuple[str, str]] = {
    "torbox": ("app.services.downloaders.torbox", "TorBoxDownloader"),
    "sabnzbd": ("app.services.downloaders.sabnzbd", "SabnzbdDownloader"),
    "premiumize": ("app.services.downloaders.premiumize", "PremiumizeDownloader"),
}

_SAB_CATEGORY_DEFAULTS = {
    "category_movie": "movies",
    "category_series": "tv",
    "category_anime": "anime",
}


def get_downloader_client(provider: Provider) -> BaseDownloader:
    """provider.type -> client. Raises ValueError for unknown types."""
    entry = _CLIENTS.get(provider.type)
    if entry is None:
        raise ValueError(f"Unsupported downloader type: {provider.type}")
    module_name, class_name = entry
    client_cls = getattr(import_module(module_name), class_name)
    client: BaseDownloader = client_cls(provider)
    return client


async def get_active_downloader_client(session: AsyncSession) -> BaseDownloader | None:
    """Client for the active downloader; None if none is active."""
    provider = await get_active_downloader(session)
    return get_downloader_client(provider) if provider else None


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
    try:
        raw = json.loads(provider.config_json or "{}")
    except ValueError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    media = media_type.value if isinstance(media_type, MediaType) else str(media_type)
    if is_anime or media == MediaType.ANIME.value:
        key = "category_anime"
    elif media == MediaType.MOVIE.value:
        key = "category_movie"
    else:
        key = "category_series"
    value = raw.get(key)
    if not isinstance(value, str):
        value = _SAB_CATEGORY_DEFAULTS[key]
    return value or None
