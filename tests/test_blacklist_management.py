import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BlacklistedRelease,
    MediaItem,
    MediaStatus,
    MediaType,
    Provider,
)
from app.main import app

client = TestClient(app)


@pytest.mark.asyncio
async def test_blacklist_endpoints(db_session: AsyncSession):
    # Setup test data
    provider = Provider(name="Test Prov", type="simkl")
    db_session.add(provider)
    await db_session.flush()

    media = MediaItem(
        provider_id=provider.id,
        simkl_id="123",
        title="Test Movie",
        media_type=MediaType.MOVIE,
        status=MediaStatus.SEARCHING,
    )
    db_session.add(media)
    await db_session.flush()

    blacklist1 = BlacklistedRelease(
        media_item_id=media.id,
        nzb_title="Test.Movie.2024.1080p.WEB-DL-GROUP1",
        reason="Removed by TorBox (DMCA)",
    )
    blacklist2 = BlacklistedRelease(
        media_item_id=media.id,
        nzb_title="Test.Movie.2024.2160p.WEB-DL-GROUP2",
        reason="Invalid Archive",
    )
    db_session.add_all([blacklist1, blacklist2])
    await db_session.commit()

    # Test GET /api/blacklist
    response = client.get("/api/blacklist")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    titles = [item["nzb_title"] for item in data]
    assert "Test.Movie.2024.1080p.WEB-DL-GROUP1" in titles
    assert "Test.Movie.2024.2160p.WEB-DL-GROUP2" in titles

    # Find ID of blacklist1 to delete
    b1_id = next(
        item["id"]
        for item in data
        if item["nzb_title"] == "Test.Movie.2024.1080p.WEB-DL-GROUP1"
    )

    # Test DELETE /api/blacklist/{id}
    del_response = client.delete(f"/api/blacklist/{b1_id}")
    assert del_response.status_code == 200
    assert del_response.text == ""

    # Verify deletion in db
    response_after = client.get("/api/blacklist")
    data_after = response_after.json()
    assert len(data_after) == 1
    assert data_after[0]["nzb_title"] == "Test.Movie.2024.2160p.WEB-DL-GROUP2"


@pytest.mark.asyncio
async def test_blacklist_ui_rendering(db_session: AsyncSession):
    # Setup test data
    provider = Provider(name="Test Prov", type="simkl")
    db_session.add(provider)
    await db_session.flush()

    media = MediaItem(
        provider_id=provider.id,
        simkl_id="456",
        title="UI Test Movie",
        media_type=MediaType.MOVIE,
        status=MediaStatus.SEARCHING,
    )
    db_session.add(media)
    await db_session.flush()

    blacklist1 = BlacklistedRelease(
        media_item_id=media.id,
        nzb_title="UITest.Movie.2024.1080p",
        reason="Failed verification",
    )
    db_session.add(blacklist1)
    await db_session.commit()

    # Test GET /settings renders the blacklist item
    response = client.get("/settings")
    assert response.status_code == 200
    html = response.text

    assert "Blacklisted Releases" in html
    assert "UITest.Movie.2024.1080p" in html
    assert "Failed verification" in html
    assert f'hx-delete="/api/blacklist/{blacklist1.id}"' in html
