"""Watchlist settings UI / auth route tests (m56)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.db.models import Provider
from app.services.watchlists import plex
from tests.conftest import make_watchlist_provider

TOKEN = "plex-secret-token"


@pytest.fixture(autouse=True)
def _clear_plex_sessions():
    plex._PIN_SESSIONS.clear()


@pytest.mark.parametrize("data", [{}, {"provider_id": "999"}, {"provider_id": "x"}])
async def test_plex_start_requires_saved_provider(async_client, data):
    resp = await async_client.post("/settings/provider/plex/auth/start", data=data)
    assert resp.status_code == 400
    assert "Save the Plex provider first." in resp.text


async def test_plex_start_and_poll_saves_token(async_client, db_session, monkeypatch):
    prov = await make_watchlist_provider(db_session, "plex")
    await db_session.commit()
    monkeypatch.setattr(
        plex, "create_pin", AsyncMock(return_value={"id": 5, "code": "WXYZ"})
    )
    poll_pin = AsyncMock(side_effect=[None, TOKEN])
    monkeypatch.setattr(plex, "poll_pin", poll_pin)

    resp = await async_client.post(
        "/settings/provider/plex/auth/start", data={"provider_id": str(prov.id)}
    )
    assert resp.status_code == 200
    assert "WXYZ" in resp.text and 'hx-trigger="every 3s"' in resp.text
    assert "https://app.plex.tv/auth#?clientID=" in resp.text
    assert "context[device][product]=NZBoxer" in resp.text
    (session_id,) = plex._PIN_SESSIONS

    pending = await async_client.get(
        "/settings/provider/plex/auth/poll", params={"session_id": session_id}
    )
    assert 'hx-trigger="every 3s"' in pending.text

    done = await async_client.get(
        "/settings/provider/plex/auth/poll", params={"session_id": session_id}
    )
    assert "Connected" in done.text and TOKEN not in done.text
    assert TOKEN not in resp.text + pending.text
    assert session_id not in plex._PIN_SESSIONS

    await db_session.refresh(prov)
    assert prov.access_token == TOKEN
    cid = json.loads(prov.config_json)["plex_client_identifier"]
    assert poll_pin.await_args.args == (5, "WXYZ", cid)

    # client identifier is generated once
    await async_client.post(
        "/settings/provider/plex/auth/start", data={"provider_id": str(prov.id)}
    )
    await db_session.refresh(prov)
    assert json.loads(prov.config_json)["plex_client_identifier"] == cid


async def test_plex_poll_unknown_session_is_expired(async_client):
    resp = await async_client.get(
        "/settings/provider/plex/auth/poll", params={"session_id": "nope"}
    )
    assert "Expired PIN" in resp.text


MAL_STATUS = "/settings/provider/mal/auth/status"


@pytest.fixture
async def mal_provider(db_session):
    prov = await make_watchlist_provider(db_session, "mal", client_id="mal-cid")
    await db_session.commit()
    return prov


async def test_mal_start_requires_provider(async_client):
    resp = await async_client.post("/settings/provider/mal/auth/start", data={})
    assert resp.status_code == 400


async def test_mal_start_builds_pkce_link_and_status_poller(async_client, mal_provider):
    resp = await async_client.post(
        "/settings/provider/mal/auth/start",
        data={"provider_id": str(mal_provider.id), "client_id": "mal-cid"},
    )
    text = resp.text
    assert resp.status_code == 200
    assert "code_challenge_method=plain" in text and "client_secret" not in text
    assert 'target="_blank" rel="noopener noreferrer"' in text
    assert "http://test/settings/provider/mal/auth/callback" in text
    assert f'hx-get="{MAL_STATUS}?provider_id={mal_provider.id}"' in text
    assert 'hx-trigger="every 3s"' in text


async def test_mal_callback_bad_state_400(async_client):
    resp = await async_client.get(
        "/settings/provider/mal/auth/callback", params={"code": "c", "state": "bad"}
    )
    assert resp.status_code == 400


async def test_mal_callback_saves_tokens_and_status_flips(
    async_client, db_session, mal_provider, monkeypatch
):
    from app.services.watchlists import mal

    exchange = AsyncMock(
        return_value={
            "access_token": "mal-a",
            "refresh_token": "mal-r",
            "token_expires_at": "2099-01-01T00:00:00+00:00",
        }
    )
    monkeypatch.setattr(mal, "exchange_code", exchange)
    params = {"provider_id": str(mal_provider.id)}

    pending = await async_client.get(MAL_STATUS, params=params)
    assert 'hx-trigger="every 3s"' in pending.text

    state = mal.create_auth_state("verifier-1", mal_provider.id)
    resp = await async_client.get(
        "/settings/provider/mal/auth/callback", params={"code": "abc", "state": state}
    )
    assert resp.status_code == 200 and "Connected — you may close this tab" in resp.text
    assert exchange.await_args.args[:3] == ("mal-cid", "abc", "verifier-1")

    await db_session.refresh(mal_provider)
    assert mal_provider.access_token == "mal-a"
    assert mal_provider.watchlist_config["refresh_token"] == "mal-r"

    done = await async_client.get(MAL_STATUS, params=params)
    assert "✓ Connected" in done.text and "hx-trigger" not in done.text

    # state is single-use
    replay = await async_client.get(
        "/settings/provider/mal/auth/callback", params={"code": "abc", "state": state}
    )
    assert replay.status_code == 400


# --- m56-12: Settings provider cards, modals, Sync button ---------------------

CARD_TYPES = ["simkl", "anilist", "plex", "mal"]


async def test_settings_renders_four_watchlist_cards(async_client, db_session):
    provs = {
        t: await make_watchlist_provider(db_session, t, name=f"WL {t}")
        for t in CARD_TYPES
    }
    provs["plex"].access_token = "plex-tok"
    provs["simkl"].access_token = "legacy"  # token without refresh ⇒ reconnect
    provs["mal"].config_json = json.dumps(
        {"last_synced_at": "2026-01-02T03:04:05+00:00", "last_error": "boom-err"}
    )
    await db_session.commit()

    html = (await async_client.get("/settings")).text
    for t, p in provs.items():
        assert f'id="provider-card-{p.id}"' in html
        assert f'id="provider-sync-btn-{p.id}"' in html
        assert f'hx-post="/settings/provider/{p.id}/sync"' in html

    def badge(pid: int) -> str:
        start = html.index(f'id="provider-status-badge-{pid}"')
        return html[start : html.index("</span>", start)]

    assert "Connected" in badge(provs["plex"].id)
    assert "Not Connected" not in badge(provs["plex"].id)
    assert "Not Connected" in badge(provs["mal"].id)
    assert "Reconnect Required" in badge(provs["simkl"].id)
    assert "boom-err" in html and "2026-01-02" in html


async def test_new_modal_fields_per_type(async_client):
    anilist = (await async_client.get("/settings/provider/new?type=anilist")).text
    assert 'name="username"' in anilist and 'name="access_token"' in anilist
    plex_html = (await async_client.get("/settings/provider/new?type=plex")).text
    assert 'name="access_token"' in plex_html
    mal_html = (await async_client.get("/settings/provider/new?type=mal")).text
    assert 'name="client_id"' in mal_html
    assert "http://test/settings/provider/mal/auth/callback" in mal_html
    for body in (anilist, plex_html, mal_html):
        assert 'name="sync_interval_minutes"' in body


async def test_edit_modal_does_not_echo_secrets(async_client, db_session):
    plex_prov = await make_watchlist_provider(db_session, "plex", access_token=TOKEN)
    await db_session.commit()
    html = (await async_client.get(f"/settings/provider/{plex_prov.id}/edit")).text
    assert TOKEN not in html
    assert "/settings/provider/plex/auth/start" in html


async def test_save_blank_token_preserves_existing_and_normalizes_interval(
    async_client, db_session
):
    prov = await make_watchlist_provider(
        db_session,
        "anilist",
        access_token="keep-me",
        username="u1",
        config_json=json.dumps({"last_synced_at": "x", "sync_interval_minutes": 60}),
    )
    await db_session.commit()
    resp = await async_client.post(
        f"/settings/provider/{prov.id}",
        data={
            "name": "AL",
            "provider_type": "anilist",
            "category": "watchlist",
            "username": "u2",
            "access_token": "",
            "sync_interval_minutes": "3",
            "is_active": "true",
        },
    )
    assert resp.status_code == 200
    await db_session.refresh(prov)
    assert prov.access_token == "keep-me"
    assert prov.username == "u2"
    cfg = prov.watchlist_config
    assert (
        cfg["sync_interval_minutes"] == 5
    )  # clamped by normalize_sync_interval_minutes
    assert cfg["last_synced_at"] == "x"


async def test_save_new_mal_provider(async_client, db_session):
    from sqlalchemy import select

    resp = await async_client.post(
        "/settings/provider",
        data={
            "name": "MAL",
            "provider_type": "mal",
            "client_id": "cid",
            "sync_interval_minutes": "30",
            "is_active": "true",
        },
    )
    assert resp.status_code == 200
    row = (
        await db_session.execute(select(Provider).where(Provider.type == "mal"))
    ).scalar_one()
    assert row.category == "watchlist" and row.client_id == "cid"
    assert row.watchlist_config["sync_interval_minutes"] == 30


async def test_provider_sync_route_and_busy(async_client, db_session, monkeypatch):
    from app.core import automation

    prov = await make_watchlist_provider(db_session, "plex")
    await db_session.commit()
    ok = AsyncMock(
        return_value={"status": "ok", "ingested": 1, "added": 1, "pruned": 0}
    )
    monkeypatch.setattr(automation, "sync_single_provider", ok)
    resp = await async_client.post(f"/settings/provider/{prov.id}/sync")
    assert resp.status_code == 200
    assert resp.headers["HX-Trigger"] == "refreshDashboard"
    ok.assert_awaited_once_with(prov.id)
    assert "sync" in resp.text.lower()

    busy = AsyncMock(return_value={"status": "busy"})
    monkeypatch.setattr(automation, "sync_single_provider", busy)
    resp = await async_client.post(f"/settings/provider/{prov.id}/sync")
    assert "A sync is already running." in resp.text

    err = AsyncMock(return_value={"status": "error", "error": "nope"})
    monkeypatch.setattr(automation, "sync_single_provider", err)
    resp = await async_client.post(f"/settings/provider/{prov.id}/sync")
    assert "nope" in resp.text
