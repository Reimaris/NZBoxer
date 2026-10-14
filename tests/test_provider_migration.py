"""
Tests for Unified Provider Model and Legacy Keys Migration
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.database import init_db
from app.db.models import Provider, ProviderCategory


@pytest.mark.asyncio
async def test_provider_model_columns(db_session: AsyncSession) -> None:
    """Verify that Provider model has category, api_key, api_url, priority, and is_active columns."""
    provider = Provider(
        category=ProviderCategory.INDEXER.value,
        type="newznab",
        name="Test Indexer",
        api_key="test_key_123",
        api_url="https://test.indexer.com/api",
        priority=2,
        is_active=True,
    )
    db_session.add(provider)
    await db_session.commit()
    await db_session.refresh(provider)

    assert provider.id is not None
    assert provider.category == "indexer"
    assert provider.type == "newznab"
    assert provider.api_key == "test_key_123"
    assert provider.api_url == "https://test.indexer.com/api"
    assert provider.priority == 2
    assert provider.is_active is True


@pytest.mark.asyncio
async def test_existing_providers_category_backfill(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Verify that existing providers with missing or default categories get properly backfilled."""
    db_file = tmp_path / "test_backfill.db"  # type: ignore[operator]
    db_url = f"sqlite+aiosqlite:///{db_file}"

    await init_db(db_url)
    engine = create_async_engine(db_url)
    session_maker = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    async with session_maker() as session:
        # Create un-categorized or mismatched providers
        p1 = Provider(type="tmdb", name="TMDB Meta", category="watchlist")
        p2 = Provider(type="torbox", name="TorBox DL", category="watchlist")
        p3 = Provider(type="anilist", name="AniList Meta", category="watchlist")
        session.add_all([p1, p2, p3])
        await session.commit()

    # Re-run init_db to trigger backfill
    await init_db(db_url)

    async with session_maker() as session:
        result = await session.execute(select(Provider))
        providers = result.scalars().all()
        p_dict = {p.type: p for p in providers}

        assert p_dict["tmdb"].category == ProviderCategory.METADATA.value
        assert p_dict["torbox"].category == ProviderCategory.DOWNLOADER.value
        assert p_dict["anilist"].category == ProviderCategory.WATCHLIST.value

    await engine.dispose()
