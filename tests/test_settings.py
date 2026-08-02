from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import scoring_config, settings
from app.db.models import SystemSettings
from app.main import app

# Create a synchronous test client
client = TestClient(app)

@pytest.fixture
def mock_session_factory(db_session: AsyncSession):
    @asynccontextmanager
    async def _mock_factory():
        yield db_session
    
    with patch("app.main.async_session_factory", side_effect=_mock_factory):
        yield

@pytest.mark.asyncio
async def test_get_settings_page(db_session: AsyncSession, mock_session_factory):
    """Test that the settings page renders successfully."""
    # First, let's create a dummy settings row
    db_settings = SystemSettings(
        id=1,
        simkl_client_id="test_client_id",
        scoring_config_yaml="scoring:\n  resolution:\n    1080p: 50"
    )
    db_session.add(db_settings)
    await db_session.commit()
    
    response = client.get("/settings")
    assert response.status_code == 200
    assert "test_client_id" in response.text
    assert "1080p: 50" in response.text

@pytest.mark.asyncio
async def test_post_settings_valid(db_session: AsyncSession, mock_session_factory):
    """Test saving valid settings."""
    db_settings = SystemSettings(id=1)
    db_session.add(db_settings)
    await db_session.commit()

    yaml_payload = "scoring:\n  resolution:\n    4k: 100\n"

    response = client.post(
        "/settings",
        data={
            "simkl_client_id": "new_client_id",
            "simkl_access_token": "new_token",
            "tmdb_api_key": "tmdb123",
            "treasure_maps_url": "https://example.com",
            "treasure_maps_api_key": "map123",
            "torbox_api_key": "torbox123",
            "scoring_config_yaml": yaml_payload
        }
    )
    
    assert response.status_code == 200
    assert "Settings saved successfully" in response.text

    # Verify DB was updated
    await db_session.refresh(db_settings)
    assert db_settings.simkl_client_id == "new_client_id"
    assert db_settings.torbox_api_key == "torbox123"
    assert db_settings.scoring_config_yaml == yaml_payload

    # Verify global cache was updated
    assert settings.simkl_client_id == "new_client_id"
    assert scoring_config["scoring"]["resolution"]["4k"] == 100

@pytest.mark.asyncio
async def test_post_settings_invalid_yaml(db_session: AsyncSession, mock_session_factory):
    """Test saving invalid YAML rejects the update."""
    db_settings = SystemSettings(id=1, simkl_client_id="old_id")
    db_session.add(db_settings)
    await db_session.commit()

    response = client.post(
        "/settings",
        data={
            "simkl_client_id": "new_id",
            "scoring_config_yaml": "scoring:\n  - this is invalid yaml\n    bad_indentation: true"
        }
    )
    
    assert response.status_code == 200
    assert "Invalid YAML" in response.text

    # Verify DB was NOT updated
    await db_session.refresh(db_settings)
    assert db_settings.simkl_client_id == "old_id"
