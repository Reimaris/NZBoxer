import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BlacklistedRelease,
    MediaItem,
    MediaStatus,
    MediaType,
    Provider,
)


@pytest.mark.asyncio
async def test_settings_blacklist_clear_modal(async_client: AsyncClient):
    """Verify GET /settings/blacklist/clear-modal returns 200 and renders dialog."""
    response = await async_client.get(
        "/settings/blacklist/clear-modal",
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    html = response.text
    assert "Clear Blacklisted Releases" in html
    assert "Clear All Releases" in html
    assert 'hx-post="/api/blacklist/clear"' in html


@pytest.mark.asyncio
async def test_api_clear_blacklist_json(
    db_session: AsyncSession, async_client: AsyncClient
):
    """Seed BlacklistedRelease records, call POST /api/blacklist/clear, assert rows deleted."""
    provider = Provider(name="Test Prov", type="simkl")
    db_session.add(provider)
    await db_session.flush()

    media = MediaItem(
        provider_id=provider.id,
        simkl_id="991",
        title="Clear Movie",
        media_type=MediaType.MOVIE,
        status=MediaStatus.SEARCHING,
    )
    db_session.add(media)
    await db_session.flush()

    b1 = BlacklistedRelease(
        media_item_id=media.id,
        nzb_title="Clear.Movie.1",
        reason="DMCA",
    )
    b2 = BlacklistedRelease(
        media_item_id=None,
        nzb_title="Clear.Movie.Global",
        reason="Global DMCA",
    )
    db_session.add_all([b1, b2])
    await db_session.commit()

    # Call POST /api/blacklist/clear with JSON accept
    response = await async_client.post(
        "/api/blacklist/clear",
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["deleted_count"] == 2

    # Verify database is empty
    remaining = (await db_session.execute(select(BlacklistedRelease))).scalars().all()
    assert len(remaining) == 0


@pytest.mark.asyncio
async def test_api_clear_blacklist_htmx(
    db_session: AsyncSession, async_client: AsyncClient
):
    """Verify HTMX request triggers closeModal and returns partial/empty state."""
    b1 = BlacklistedRelease(
        media_item_id=None,
        nzb_title="Clear.Movie.HTMX",
        reason="Bad archive",
    )
    db_session.add(b1)
    await db_session.commit()

    response = await async_client.post(
        "/api/blacklist/clear",
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "closeModal" in response.headers.get("HX-Trigger", "")
    html = response.text
    assert "No releases are currently blacklisted." in html
    assert "0 Blacklisted" in html


@pytest.mark.asyncio
async def test_api_clear_blacklist_idempotent_empty(
    db_session: AsyncSession, async_client: AsyncClient
):
    """Calling clear when 0 releases exist returns status ok and deleted_count 0 without error."""
    response = await async_client.post(
        "/api/blacklist/clear",
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["deleted_count"] == 0
