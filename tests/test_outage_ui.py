import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app

client = TestClient(app)


@pytest.mark.asyncio
async def test_init_db_creates_outage_columns(db_session: AsyncSession):
    """init_db creates notify_on_outages and outage_attempts columns."""
    ss = (await db_session.execute(sa_text("PRAGMA table_info(system_settings)"))).all()
    assert "notify_on_outages" in {r[1] for r in ss}
    dh = (
        await db_session.execute(sa_text("PRAGMA table_info(download_history)"))
    ).all()
    assert "outage_attempts" in {r[1] for r in dh}


def test_settings_renders_outage_toggle_unchecked_by_default():
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert 'name="notify_on_outages"' in resp.text
    assert "Notify on Service Outages &amp; Retries" in resp.text or (
        "Notify on Service Outages & Retries" in resp.text
    )
    assert (
        'js:{"notify_on_outages": event.target.checked ? "true" : "false"}' in resp.text
    )
    idx = resp.text.index('name="notify_on_outages"')
    tag = resp.text[idx : resp.text.index(">", idx)]
    assert "checked" not in tag.replace("event.target.checked", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("sent,expected", [("true", 1), ("false", 0)])
async def test_outage_toggle_autosaves(
    db_session: AsyncSession, sent: str, expected: int
):
    resp = client.post(
        "/settings/auto-save",
        data={"notify_on_outages": sent},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    val = (
        await db_session.execute(
            sa_text("SELECT notify_on_outages FROM system_settings WHERE id = 1")
        )
    ).scalar()
    assert int(val) == expected
