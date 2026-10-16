import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.services.outage_classifier import OutageVerdict

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


def _search_form(**over):
    data = {"scope": "item", "kind": "movie", "title": "Test Movie", "tmdb_id": "1"}
    data.update(over)
    return data


def _mock_indexers(monkeypatch, behaviours: dict[str, bool]):
    """``behaviours`` maps indexer host -> True when it is down (503)."""
    from app.core import adhoc_search
    from app.db.models import Provider
    from app.services.indexers import newznab

    provs = [
        Provider(name=h, api_url=f"https://{h}/api", api_key="k") for h in behaviours
    ]

    async def active(_session):
        return provs

    async def search_movie(**kw):
        if behaviours[kw["api_url"].split("//")[1].split("/")[0]]:
            raise newznab.IndexerOutageError(
                "boom", kw["api_url"], OutageVerdict("outage", "HTTP 503")
            )
        return []

    monkeypatch.setattr(adhoc_search.provider_service, "get_active_indexers", active)
    monkeypatch.setattr(adhoc_search.newznab_client, "search_movie", search_movie)


def test_manual_search_all_indexers_down_shows_retry(monkeypatch):
    _mock_indexers(monkeypatch, {"a.example.com": True, "b.example.com": True})
    html = client.post("/api/search/manual/releases", data=_search_form()).text
    assert "Retry Search" in html
    assert "Indexer outage: a.example.com returned HTTP 503." in html
    assert "No items found" not in html


def test_manual_search_with_healthy_indexer_has_no_retry(monkeypatch):
    _mock_indexers(monkeypatch, {"a.example.com": True, "b.example.com": False})
    html = client.post("/api/search/manual/releases", data=_search_form()).text
    assert "Retry Search" not in html


@pytest.mark.asyncio
@pytest.mark.parametrize("down,expected", [(True, "HTTP 503"), (False, None)])
async def test_push_modal_search_reports_outage(
    db_session: AsyncSession, monkeypatch, down, expected
):
    from app.core.push_engine import execute_manual_search
    from app.services.indexers import newznab
    from tests.conftest import make_indexer_provider, make_movie

    item = await make_movie(db_session)
    await make_indexer_provider(db_session, api_url="https://a.example.com/api")
    await db_session.commit()

    async def search_movie(**kw):
        if down:
            raise newznab.IndexerOutageError(
                "boom", kw["api_url"], OutageVerdict("outage", "HTTP 503")
            )
        return []

    monkeypatch.setattr(newznab, "search_movie", search_movie)
    result = await execute_manual_search(db_session, item.id, {})
    got = result["search_outage"]
    assert (got["label"] if got else None) == expected
