import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import reload_settings_from_db, settings
from app.db.models import SystemSettings

FIELDS = [
    ("air_delay_hours", 6, 2, 7),
    ("upgrade_window_hours", 72, 48, 13),
    ("max_upgrade_checks", 5, 3, 4),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "valid", "default", "invalid"), FIELDS)
async def test_auto_save_persists_and_reloads(
    async_client: AsyncClient,
    db_session: AsyncSession,
    name: str,
    valid: int,
    default: int,
    invalid: int,
):
    resp = await async_client.post("/settings/auto-save", data={name: str(valid)})
    assert resp.status_code < 400
    await reload_settings_from_db(db_session)
    assert getattr(settings, name) == valid

    await async_client.post("/settings/auto-save", data={name: str(invalid)})
    await async_client.post("/settings/auto-save", data={name: "abc"})
    db_session.expire_all()
    row = await db_session.get(SystemSettings, 1)
    assert row is not None
    assert getattr(row, name) == default
    await reload_settings_from_db(db_session)
    assert getattr(settings, name) == default


@pytest.mark.asyncio
async def test_settings_page_has_air_upgrade_selects(async_client: AsyncClient):
    resp = await async_client.get("/settings")
    assert resp.status_code == 200
    for name, *_ in FIELDS:
        assert f'name="{name}"' in resp.text


@pytest.mark.asyncio
async def test_settings_page_labels_max_upgrades_per_episode(async_client: AsyncClient):
    resp = await async_client.get("/settings")
    assert "Max Upgrades per Episode" in resp.text
    assert 'name="max_upgrade_checks"' in resp.text
