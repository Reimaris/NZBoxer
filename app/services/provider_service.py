"""
Provider Service
================
Central service for managing and resolving third-party providers (Watchlist, Reading, Metadata, Downloader, Indexer).
"""

from __future__ import annotations

import logging
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Provider, ProviderCategory

logger = logging.getLogger(__name__)


async def get_active_downloader(db: AsyncSession) -> Provider | None:
    """Retrieve the single active downloader provider."""
    stmt = (
        select(Provider)
        .where(
            Provider.category == ProviderCategory.DOWNLOADER.value,
            Provider.is_active == True,  # noqa: E712
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def set_active_downloader(db: AsyncSession, provider_id: int) -> None:
    """Set the specified downloader as active and deactivate all other downloaders."""
    # Deactivate all downloaders
    await db.execute(
        update(Provider)
        .where(Provider.category == ProviderCategory.DOWNLOADER.value)
        .values(is_active=False)
    )
    # Activate the target downloader
    await db.execute(
        update(Provider)
        .where(
            Provider.category == ProviderCategory.DOWNLOADER.value,
            Provider.id == provider_id,
        )
        .values(is_active=True)
    )
    await db.commit()


async def get_active_indexers(db: AsyncSession) -> Sequence[Provider]:
    """Retrieve all active Usenet indexers ordered by priority ASC, id ASC."""
    stmt = (
        select(Provider)
        .where(
            Provider.category == ProviderCategory.INDEXER.value,
            Provider.is_active == True,  # noqa: E712
        )
        .order_by(Provider.priority.asc(), Provider.id.asc())
    )
    result = await db.execute(stmt)
    return result.scalars().all()


async def get_metadata_provider(
    db: AsyncSession, provider_type: str
) -> Provider | None:
    """Retrieve an active metadata provider by type (e.g. 'tmdb', 'anilist')."""
    stmt = (
        select(Provider)
        .where(
            Provider.category == ProviderCategory.METADATA.value,
            Provider.type == provider_type,
            Provider.is_active == True,  # noqa: E712
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_providers_by_category(
    db: AsyncSession, category: str | ProviderCategory
) -> Sequence[Provider]:
    """Retrieve all providers in a given category ordered by priority and name."""
    cat_val = (
        category.value if isinstance(category, ProviderCategory) else str(category)
    )
    stmt = (
        select(Provider)
        .where(Provider.category == cat_val)
        .order_by(Provider.priority.asc(), Provider.name.asc())
    )
    result = await db.execute(stmt)
    return result.scalars().all()
