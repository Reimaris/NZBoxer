"""Provider routes for downloader types (m57-10, spec §3.7 / §4.10)."""

import json
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Provider
from app.services import provider_service
from tests.conftest import make_downloader_provider, make_history

SAB_FORM = {
    "provider_type": "sabnzbd",
    "category": "downloader",
    "name": "SAB",
    "api_key": "k",
    "api_url": "http://localhost:8080/sabnzbd",
    "category_movie": "films",
    "category_series": "",
    "category_anime": "anime",
}


async def _providers(session: AsyncSession) -> list[Provider]:
    session.expire_all()
    return list((await session.execute(select(Provider))).scalars().all())


@pytest.mark.asyncio
async def test_create_sab_stores_categories_and_preserves_empty(
    async_client: AsyncClient, db_session: AsyncSession
) -> None:
    resp = await async_client.post("/settings/provider", data=SAB_FORM)
    assert resp.status_code == 200
    (p,) = await _providers(db_session)
    assert p.type == "sabnzbd" and p.category == "downloader"
    assert json.loads(p.config_json) == {
        "category_movie": "films",
        "category_series": "",
        "category_anime": "anime",
    }
    assert p.sabnzbd_config["category_series"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"api_url": ""},
        {"api_url": "ftp://host/sabnzbd"},
        {"api_key": ""},
        {"provider_type": "premiumize", "api_key": ""},
        {"provider_type": "torbox", "api_key": " "},
    ],
)
async def test_invalid_downloader_form_returns_422(
    async_client: AsyncClient, db_session: AsyncSession, overrides: dict[str, str]
) -> None:
    resp = await async_client.post("/settings/provider", data={**SAB_FORM, **overrides})
    assert resp.status_code == 422
    assert resp.text.strip()
    assert await _providers(db_session) == []


@pytest.mark.asyncio
async def test_create_premiumize_ok(
    async_client: AsyncClient, db_session: AsyncSession
) -> None:
    form = {"provider_type": "premiumize", "name": "PM", "api_key": "abc"}
    assert (await async_client.post("/settings/provider", data=form)).status_code == 200
    (p,) = await _providers(db_session)
    assert (p.type, p.category) == ("premiumize", "downloader")


@pytest.mark.asyncio
async def test_activating_one_deactivates_others(
    async_client: AsyncClient, db_session: AsyncSession
) -> None:
    tb = await make_downloader_provider(db_session, "torbox")
    await db_session.commit()
    form = {**SAB_FORM, "is_active": "true"}
    assert (await async_client.post("/settings/provider", data=form)).status_code == 200
    by_type = {p.type: p for p in await _providers(db_session)}
    assert by_type["sabnzbd"].is_active and not by_type["torbox"].is_active
    assert tb.id != by_type["sabnzbd"].id


@pytest.mark.asyncio
async def test_activation_flash_for_foreign_transfers(
    async_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_downloader_provider(db_session, "torbox")
    await make_history(
        db_session,
        downloader_type="torbox",
        status_detail="downloading",
        is_dismissed=False,
    )
    await db_session.commit()
    resp = await async_client.post(
        "/settings/provider", data={**SAB_FORM, "is_active": "true"}
    )
    assert "1 running transfer(s) stay on TorBox and keep being tracked." in resp.text


@pytest.mark.asyncio
async def test_count_foreign_downloading(db_session: AsyncSession) -> None:
    await make_history(
        db_session, downloader_type="torbox", status_detail="downloading"
    )
    await make_history(
        db_session, downloader_type="sabnzbd", status_detail="downloading"
    )
    await make_history(db_session, downloader_type="torbox", status_detail="completed")
    await db_session.commit()
    counts = await provider_service.count_foreign_downloading(db_session, "sabnzbd")
    assert counts == {"torbox": 1}


@pytest.mark.asyncio
async def test_test_route_dispatches_to_client(async_client: AsyncClient) -> None:
    async def fake(self: object) -> tuple[bool, str]:
        return True, "Connected"

    with patch(
        "app.services.downloaders.sabnzbd.SabnzbdDownloader.test_connection", fake
    ):
        resp = await async_client.post("/api/providers/test", data=SAB_FORM)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "message": "Connected"}


@pytest.mark.asyncio
async def test_test_route_validation_and_unknown_type(
    async_client: AsyncClient,
) -> None:
    bad = await async_client.post(
        "/api/providers/test", data={**SAB_FORM, "api_url": "nope"}
    )
    assert bad.status_code == 422
    assert bad.json()["ok"] is False
    unknown = await async_client.post(
        "/api/providers/test", data={**SAB_FORM, "provider_type": "bogus"}
    )
    assert unknown.status_code == 422


@pytest.mark.asyncio
async def test_modal_offers_all_downloader_types(async_client: AsyncClient) -> None:
    html = (await async_client.get("/settings/provider/new")).text
    for marker in ("'sabnzbd', 'downloader'", "'premiumize', 'downloader'"):
        assert marker in html
    assert 'name="category_movie"' in html and "movies" in html
