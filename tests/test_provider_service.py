"""
Tests for Provider Service and Dynamic Credential Resolution
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Provider, ProviderCategory
from app.services import provider_service, tmdb
from app.services.downloaders import torbox


@pytest.mark.asyncio
async def test_provider_service_helpers(db_session: AsyncSession) -> None:
    # 1. Setup providers
    tb1 = Provider(
        category=ProviderCategory.DOWNLOADER.value,
        type="torbox",
        name="TorBox Primary",
        api_key="tb_key_1",
        is_active=True,
    )
    tb2 = Provider(
        category=ProviderCategory.DOWNLOADER.value,
        type="torbox",
        name="TorBox Secondary",
        api_key="tb_key_2",
        is_active=False,
    )
    idx1 = Provider(
        category=ProviderCategory.INDEXER.value,
        type="newznab",
        name="Treasure Maps (Priority 2)",
        api_key="tm_key_2",
        api_url="https://tm2.com/api",
        priority=2,
        is_active=True,
    )
    idx2 = Provider(
        category=ProviderCategory.INDEXER.value,
        type="newznab",
        name="Newznab Fast (Priority 1)",
        api_key="tm_key_1",
        api_url="https://tm1.com/api",
        priority=1,
        is_active=True,
    )
    idx_inactive = Provider(
        category=ProviderCategory.INDEXER.value,
        type="newznab",
        name="Inactive Indexer",
        api_key="tm_key_inactive",
        api_url="https://inactive.com/api",
        priority=1,
        is_active=False,
    )
    tmdb_p = Provider(
        category=ProviderCategory.METADATA.value,
        type="tmdb",
        name="TMDB",
        api_key="tmdb_test_key",
        is_active=True,
    )

    db_session.add_all([tb1, tb2, idx1, idx2, idx_inactive, tmdb_p])
    await db_session.commit()

    # 2. Test get_active_downloader
    active_downloader = await provider_service.get_active_downloader(db_session)
    assert active_downloader is not None
    assert active_downloader.name == "TorBox Primary"
    assert active_downloader.api_key == "tb_key_1"

    # 3. Test set_active_downloader (switching active downloader)
    await provider_service.set_active_downloader(db_session, tb2.id)
    await db_session.refresh(tb1)
    await db_session.refresh(tb2)
    assert tb1.is_active is False
    assert tb2.is_active is True

    new_active_dl = await provider_service.get_active_downloader(db_session)
    assert new_active_dl is not None
    assert new_active_dl.name == "TorBox Secondary"

    # 4. Test get_active_indexers (sorted by priority ASC, id ASC)
    active_indexers = await provider_service.get_active_indexers(db_session)
    assert len(active_indexers) == 2
    assert active_indexers[0].name == "Newznab Fast (Priority 1)"
    assert active_indexers[0].priority == 1
    assert active_indexers[1].name == "Treasure Maps (Priority 2)"
    assert active_indexers[1].priority == 2

    # 5. Test get_metadata_provider
    meta = await provider_service.get_metadata_provider(db_session, "tmdb")
    assert meta is not None
    assert meta.api_key == "tmdb_test_key"


@pytest.mark.asyncio
async def test_dynamic_key_resolvers(db_session: AsyncSession) -> None:
    # Test torbox API key resolution
    tb = Provider(
        category=ProviderCategory.DOWNLOADER.value,
        type="torbox",
        name="TorBox Active",
        api_key="dynamic_tb_key",
        is_active=True,
    )
    db_session.add(tb)
    await db_session.commit()

    resolved_tb_key = await torbox.resolve_api_key(session=db_session)
    assert resolved_tb_key == "dynamic_tb_key"

    # Test tmdb API key resolution
    tmdb_p = Provider(
        category=ProviderCategory.METADATA.value,
        type="tmdb",
        name="TMDB",
        api_key="dynamic_tmdb_key",
        is_active=True,
    )
    db_session.add(tmdb_p)
    await db_session.commit()

    resolved_tmdb_key = await tmdb.resolve_api_key(session=db_session)
    assert resolved_tmdb_key == "dynamic_tmdb_key"


def test_metadata_types_pinned_to_tmdb() -> None:
    assert provider_service._METADATA_TYPES == frozenset({"tmdb"})
