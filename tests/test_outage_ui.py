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


@pytest.fixture
def nzbgeek_outage():
    from app.core.outage import clear_outages, record_outage
    from app.services.indexers import newznab

    newznab._indexer_names["indexer.example.com"] = "NZBgeek"
    record_outage("indexer", "indexer.example.com", "NZBgeek", "HTTP 503", 1)
    yield
    newznab._indexer_names.pop("indexer.example.com", None)
    clear_outages()


RED = "bg-red-950/90 border border-red-600/70 text-red-100 shadow-lg shadow-red-950/40"


def test_banner_present_during_outage_only_on_dashboard(nzbgeek_outage):
    dash = client.get("/").text
    assert RED in dash
    assert 'data-outage-banner="true"' in dash
    assert "Service Outage Active: NZBgeek returned HTTP 503" in dash
    assert "⚡ Resume Queue" in dash
    assert "data-outage-banner" not in client.get("/settings").text
    api = client.get("/api/outages/banner")
    assert api.status_code == 200 and RED in api.text


def test_banner_absent_without_outage():
    assert "data-outage-banner" not in client.get("/").text
    assert client.get("/api/outages/banner").text.strip() == ""


@pytest.mark.asyncio
async def test_card5_shows_outage_badge(db_session: AsyncSession, nzbgeek_outage):
    from app.core.transfer_poller import get_active_pushes
    from tests.conftest import make_history, make_movie

    item = await make_movie(db_session)
    await make_history(
        db_session,
        item=item,
        is_dismissed=False,
        status_detail="paused: indexer outage (HTTP 503)",
        outage_attempts=1,
    )
    await db_session.commit()
    (push,) = await get_active_pushes(db_session)
    assert push["status"].startswith("Paused — Indexer Outage (resumes ")
    html = client.get("/").text
    assert "Paused — Indexer Outage (resumes " in html
    assert "Paused: NZBgeek server error (HTTP 503)" in html
