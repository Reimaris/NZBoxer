import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.testclient import TestClient

from app.main import app
from app.db.models import SystemSettings, Provider, NotificationChannel
from app.config import settings

client = TestClient(app)

@pytest.mark.asyncio
async def test_get_settings_page(db_session: AsyncSession):
    """Test that the settings page renders successfully."""
    # First, let's create a dummy settings row
    db_settings = SystemSettings(
        id=1,
        scoring_settings={"scoring": {"resolution": {"1080p": 50}}}
    )
    db_session.add(db_settings)
    await db_session.commit()

    response = client.get("/settings")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Settings" in response.text

@pytest.mark.asyncio
async def test_post_global_settings_valid(db_session: AsyncSession):
    """Test saving valid global settings."""
    db_settings = SystemSettings(id=1)
    db_session.add(db_settings)
    await db_session.commit()

    response = client.post(
        "/settings/global",
        data={
            "tmdb_api_key": "tmdb123",
            "treasure_maps_url": "https://example.com",
            "treasure_maps_api_key": "map123",
            "torbox_api_key": "torbox123",
            "scan_interval_multiplier": 2,
            "sh_max_retries": 5,
            "sh_max_time_hours": 24.0,
            "sh_auto_retry": False,
            "sh_retry_wait_hours": 12.0,
            # Scoring
            "res_2160p": 100,
            "res_1080p": 50,
            "res_720p": 10,
            "codec_h265": 30,
            "codec_h264": 20,
            "source_remux": 100,
            "source_bluray": 80,
            "source_webdl": 60,
            "source_webrip": 50,
            "cutoffs_target": 250,
            "cutoffs_upgrade": 30
        }
    )

    assert response.status_code == 200
    assert "Settings saved successfully" in response.text

    # Verify DB was updated
    await db_session.refresh(db_settings)
    assert db_settings.torbox_api_key == "torbox123"
    assert db_settings.scan_interval_multiplier == 2
    assert db_settings.scoring_settings["scoring"]["resolution"]["1080p"] == 50

    # Verify global cache was updated
    assert settings.torbox_api_key == "torbox123"
