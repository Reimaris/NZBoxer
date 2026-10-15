"""
Tests for Ticket 01: Simkl AUTH V2 RFC 8628 Device Flow, Auto-Refresh Lifecycle & Sync Cooldown.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import AsyncClient

from app.core.automation import (
    is_provider_periodic_sync_due,
    sync_all_providers,
)
from app.db.database import async_session_factory
from app.db.models import MediaItem, Provider
from app.services.watchlists import simkl
from app.version import APP_VERSION


@pytest.mark.asyncio
async def test_request_device_code_and_oauth_error_handling():
    """request_device_code sends POST /oauth2/device with V2 metadata and raises SimklOAuthError on 401/412."""
    captured_requests: list[dict[str, Any]] = []

    async def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        captured_requests.append({"url": url, **kwargs})
        return httpx.Response(
            200,
            json={
                "device_code": "simkl_dc_secret_device_code_123456789012345",
                "user_code": "ABCD-1234",
                "verification_uri": "https://simkl.com/pin",
                "verification_uri_complete": "https://simkl.com/pin?user_code=ABCD-1234",
                "expires_in": 900,
                "interval": 5,
            },
        )

    with patch("httpx.AsyncClient.post", side_effect=fake_post):
        res = await simkl.request_device_code("my_v2_client_id")

    assert res["device_code"] == "simkl_dc_secret_device_code_123456789012345"
    assert res["user_code"] == "ABCD-1234"
    assert (
        res["verification_uri_complete"] == "https://simkl.com/pin?user_code=ABCD-1234"
    )
    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req["url"] == "https://api.simkl.com/oauth2/device"
    assert req["params"] == {
        "client_id": "my_v2_client_id",
        "app-name": "nzboxer",
        "app-version": APP_VERSION,
    }
    assert req["headers"]["simkl-api-key"] == "my_v2_client_id"
    assert req["data"] == {"client_id": "my_v2_client_id", "scope": "media:read"}

    # Verify 412 client_id_failed raises SimklOAuthError
    async def fake_post_412(url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(
            412,
            json={
                "error": "client_id_failed",
                "error_description": "App is not configured for Device Flow",
            },
        )

    with patch("httpx.AsyncClient.post", side_effect=fake_post_412):
        with pytest.raises(simkl.SimklOAuthError) as exc_info:
            await simkl.request_device_code("bad_v1_client_id")
        assert exc_info.value.status_code == 412
        assert exc_info.value.error == "client_id_failed"


@pytest.mark.asyncio
async def test_pin_and_poll_endpoints_isolate_device_code_and_handle_rfc8628_states(
    async_client: AsyncClient,
):
    """POST /simkl/auth/pin and GET /simkl/auth/poll isolate device_code server-side and handle pending, slow_down, expired, and 200 OK."""
    async with async_session_factory() as session:
        prov = Provider(
            name="Simkl Main",
            type="simkl",
            category="watchlist",
            client_id="cid_v2",
            access_token="legacy_v1_tok",
            is_active=True,
        )
        session.add(prov)
        await session.commit()
        prov_id = prov.id

    device_payload = {
        "device_code": "simkl_dc_super_secret_do_not_leak_99999999",
        "user_code": "WXYZ-5678",
        "verification_uri": "https://simkl.com/pin",
        "verification_uri_complete": "https://simkl.com/pin?user_code=WXYZ-5678",
        "expires_in": 900,
        "interval": 5,
    }

    client = async_client
    if True:
        # 1. Start PIN flow
        with patch(
            "app.services.watchlists.simkl.request_device_code",
            new=AsyncMock(return_value=device_payload),
        ):
            pin_resp = await client.post(
                "/simkl/auth/start",
                data={"client_id": "cid_v2", "provider_id": str(prov_id)},
            )
        assert pin_resp.status_code == 200
        html = pin_resp.text
        assert "WXYZ-5678" in html
        assert "https://simkl.com/pin?user_code=WXYZ-5678" in html
        # Crucial security check: device_code must NEVER appear in HTML or polling URL
        assert "simkl_dc_super_secret_do_not_leak_99999999" not in html
        assert "/simkl/auth/poll?session_id=" in html
        assert 'hx-trigger="every 5s"' in html

        # Extract session_id from server store
        assert len(simkl._SIMKL_DEVICE_SESSIONS) == 1
        session_id = next(iter(simkl._SIMKL_DEVICE_SESSIONS.keys()))
        assert f"session_id={session_id}" in html

        # 2. Poll -> authorization_pending (keeps 5s interval)
        with patch(
            "app.services.watchlists.simkl.poll_device_token",
            new=AsyncMock(
                side_effect=simkl.SimklOAuthError(
                    400, "authorization_pending", "User has not authorized yet"
                )
            ),
        ):
            poll_pending = await client.get(f"/simkl/auth/poll?session_id={session_id}")
        assert poll_pending.status_code == 200
        assert 'hx-trigger="every 5s"' in poll_pending.text
        assert "simkl_dc_super_secret_do_not_leak_99999999" not in poll_pending.text

        # 3. Poll -> slow_down (increments interval by +5s -> 10s)
        with patch(
            "app.services.watchlists.simkl.poll_device_token",
            new=AsyncMock(
                side_effect=simkl.SimklOAuthError(
                    400, "slow_down", "Polling too quickly"
                )
            ),
        ):
            poll_slow = await client.get(f"/simkl/auth/poll?session_id={session_id}")
        assert poll_slow.status_code == 200
        assert 'hx-trigger="every 10s"' in poll_slow.text
        assert simkl._SIMKL_DEVICE_SESSIONS[session_id]["interval"] == 10

        # 4. Poll -> 200 OK (persists tokens to SQLite, revokes old token best-effort, stops polling)
        expires_iso = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        token_response = {
            "access_token": "simkl_at_fresh_v2_access_token_1234567890",
            "refresh_token": "simkl_rt_fresh_v2_refresh_token_123456789",
            "token_type": "Bearer",
            "expires_in": 604800,
            "scope": "media:read",
            "token_expires_at": expires_iso,
        }
        with (
            patch(
                "app.services.watchlists.simkl.poll_device_token",
                new=AsyncMock(return_value=token_response),
            ),
            patch(
                "app.services.watchlists.simkl.revoke_token",
                new=AsyncMock(return_value=True),
            ) as mock_revoke,
        ):
            poll_ok = await client.get(f"/simkl/auth/poll?session_id={session_id}")

        assert poll_ok.status_code == 200
        assert "Connected · AUTH V2 (Auto-Refresh" in poll_ok.text
        assert "hx-trigger" not in poll_ok.text
        assert "simkl_at_fresh_v2_access_token_1234567890" in poll_ok.text
        assert "simkl_rt_fresh_v2_refresh_token_123456789" in poll_ok.text
        # Session was popped
        assert session_id not in simkl._SIMKL_DEVICE_SESSIONS
        # Old legacy token was revoked best-effort
        mock_revoke.assert_awaited_once_with("cid_v2", "legacy_v1_tok")

    # Verify SQLite persistence
    async with async_session_factory() as session:
        updated_prov = await session.get(Provider, prov_id)
        assert updated_prov is not None
        assert updated_prov.access_token == "simkl_at_fresh_v2_access_token_1234567890"
        assert (
            updated_prov.watchlist_config["refresh_token"]
            == "simkl_rt_fresh_v2_refresh_token_123456789"
        )
        assert updated_prov.watchlist_config["token_expires_at"] == expires_iso
        assert updated_prov.is_authenticated is True
        assert updated_prov.auth_status_label == "Connected · AUTH V2 (Auto-Refresh)"


@pytest.mark.asyncio
async def test_pin_endpoint_401_and_412_actionable_developer_instructions(
    async_client: AsyncClient,
):
    """If POST /oauth2/device fails with 401 invalid_client or 412 client_id_failed, modal renders actionable instructions."""
    client = async_client
    if True:
        with patch(
            "app.services.watchlists.simkl.request_device_code",
            new=AsyncMock(
                side_effect=simkl.SimklOAuthError(
                    412, "client_id_failed", "Invalid application type"
                )
            ),
        ):
            resp = await client.post(
                "/simkl/auth/start", data={"client_id": "v1_web_client"}
            )
        assert resp.status_code == 200
        assert "TV, devices &amp; command line" in resp.text or (
            "TV, devices & command line" in resp.text
        )
        assert "Mobile, desktop &amp; browser apps" in resp.text or (
            "Mobile, desktop & browser apps" in resp.text
        )
        assert "simkl.com/settings/developer" in resp.text


@pytest.mark.asyncio
async def test_proactive_and_reactive_token_refresh_and_invalid_grant():
    """Proactive (<24h) and reactive (HTTP 401) refreshes update SQLite while preserving non-rotating refresh_token; 400 invalid_grant clears refresh_token."""
    soon_expiry = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    async with async_session_factory() as session:
        prov = Provider(
            name="Simkl V2",
            type="simkl",
            category="watchlist",
            client_id="cid_100",
            access_token="simkl_at_expiring_soon_token",
            config_json=json.dumps(
                {
                    "sync_interval_minutes": 60,
                    "sync_movies": True,
                    "sync_series": False,
                    "sync_anime": False,
                    "refresh_token": "simkl_rt_non_rotating_keep_me",
                    "token_expires_at": soon_expiry,
                }
            ),
            is_active=True,
        )
        session.add(prov)
        await session.commit()
        prov_id = prov.id

    # 1. Proactive refresh (< 24h remaining)
    future_expiry = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    with patch(
        "app.services.watchlists.simkl.refresh_access_token",
        new=AsyncMock(
            return_value={
                "access_token": "simkl_at_proactively_refreshed",
                "refresh_token": "simkl_rt_non_rotating_keep_me",
                "token_expires_at": future_expiry,
                "expires_in": 604800,
                "scope": "media:read",
            }
        ),
    ) as mock_refresh:
        async with async_session_factory() as session:
            p = await session.get(Provider, prov_id)
            assert p is not None
            cid, tok = await simkl.ensure_valid_simkl_token(session, p)
            assert cid == "cid_100"
            assert tok == "simkl_at_proactively_refreshed"
            mock_refresh.assert_awaited_once_with(
                "cid_100", "simkl_rt_non_rotating_keep_me"
            )

    # Verify persisted to SQLite and non-rotating refresh_token preserved
    async with async_session_factory() as session:
        p = await session.get(Provider, prov_id)
        assert p is not None
        assert p.access_token == "simkl_at_proactively_refreshed"
        assert p.watchlist_config["refresh_token"] == "simkl_rt_non_rotating_keep_me"
        assert p.watchlist_config["token_expires_at"] == future_expiry

    # 2. Reactive refresh on HTTP 401 during watchlist sync (retries once and succeeds)
    call_count = 0

    async def fake_get_watchlist_401_then_ok(
        media_type: str, client_id: str, access_token: str
    ) -> list[dict[str, Any]]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise simkl.SimklUnauthorizedError("HTTP 401 invalid_token")
        assert access_token == "simkl_at_reactively_refreshed"
        return []

    reactive_expiry = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    with (
        patch(
            "app.services.watchlists.simkl.get_watchlist",
            side_effect=fake_get_watchlist_401_then_ok,
        ),
        patch(
            "app.services.watchlists.simkl.refresh_access_token",
            new=AsyncMock(
                return_value={
                    "access_token": "simkl_at_reactively_refreshed",
                    "refresh_token": "simkl_rt_non_rotating_keep_me",
                    "token_expires_at": reactive_expiry,
                    "expires_in": 604800,
                    "scope": "media:read",
                }
            ),
        ) as mock_reactive_refresh,
    ):
        await sync_all_providers()

    assert call_count == 2
    mock_reactive_refresh.assert_awaited_once()

    async with async_session_factory() as session:
        p = await session.get(Provider, prov_id)
        assert p is not None
        assert p.access_token == "simkl_at_reactively_refreshed"
        assert p.watchlist_config["refresh_token"] == "simkl_rt_non_rotating_keep_me"

    # 3. Invalid grant (400 invalid_grant) clears refresh_token and transitions badge to Reconnect Required
    with (
        patch(
            "app.services.watchlists.simkl.get_watchlist",
            side_effect=simkl.SimklUnauthorizedError("HTTP 401 invalid_token"),
        ),
        patch(
            "app.services.watchlists.simkl.refresh_access_token",
            new=AsyncMock(
                side_effect=simkl.SimklOAuthError(
                    400, "invalid_grant", "Refresh token revoked"
                )
            ),
        ),
    ):
        await sync_all_providers()

    async with async_session_factory() as session:
        p = await session.get(Provider, prov_id)
        assert p is not None
        assert not p.watchlist_config.get("refresh_token")
        assert not p.watchlist_config.get("token_expires_at")
        assert p.is_authenticated is False
        assert p.auth_status_label == "Reconnect Required (Legacy V1 / Expired)"


@pytest.mark.asyncio
async def test_revoke_token_on_provider_delete_and_settings_badges(
    async_client: AsyncClient,
):
    """Deleting a Simkl provider calls POST /oauth2/revoke best-effort, and Settings/Modal render V2 status badges."""
    async with async_session_factory() as session:
        v2_prov = Provider(
            name="Simkl V2 Account",
            type="simkl",
            category="watchlist",
            username="v2user",
            client_id="cid_v2",
            access_token="simkl_at_valid_v2_token_12345",
            config_json=json.dumps(
                {
                    "sync_interval_minutes": 60,
                    "refresh_token": "simkl_rt_valid_v2_refresh_12345",
                    "token_expires_at": (
                        datetime.now(timezone.utc) + timedelta(days=5)
                    ).isoformat(),
                }
            ),
            is_active=True,
        )
        v1_prov = Provider(
            name="Simkl Legacy Account",
            type="simkl",
            category="watchlist",
            username="v1user",
            client_id="cid_v1",
            access_token="legacy_v1_token_no_refresh",
            config_json=json.dumps({"sync_interval_minutes": 60}),
            is_active=True,
        )
        session.add_all([v2_prov, v1_prov])
        await session.commit()
        v2_id = v2_prov.id
        v1_id = v1_prov.id

    client = async_client
    if True:
        # Verify Settings Tab 1 displays both badges
        settings_resp = await client.get("/settings")
        assert settings_resp.status_code == 200
        assert "Connected · AUTH V2 (Auto-Refresh)" in settings_resp.text
        assert "Reconnect Required (Legacy V1 / Expired)" in settings_resp.text

        # Verify Provider Edit Modal displays V2 badge & hidden refresh_token input
        modal_v2 = await client.get(f"/settings/provider/{v2_id}/edit")
        assert modal_v2.status_code == 200
        assert "Connected · AUTH V2 (Auto-Refresh)" in modal_v2.text
        assert "simkl_rt_valid_v2_refresh_12345" in modal_v2.text

        # Verify Provider Edit Modal for V1 provider displays Reconnect Required badge
        modal_v1 = await client.get(f"/settings/provider/{v1_id}/edit")
        assert modal_v1.status_code == 200
        assert "Reconnect Required (Legacy V1 / Expired)" in modal_v1.text

        # Delete V2 provider -> calls revoke_token best-effort
        with patch(
            "app.services.watchlists.simkl.revoke_token",
            new=AsyncMock(return_value=True),
        ) as mock_revoke:
            del_resp = await client.delete(f"/settings/provider/{v2_id}")
            assert del_resp.status_code == 200
            mock_revoke.assert_awaited_once_with(
                "cid_v2", "simkl_rt_valid_v2_refresh_12345"
            )


@pytest.mark.asyncio
async def test_simkl_sync_failure_persists_cooldown_and_rolls_back_partial_items():
    """When sync_all_providers() encounters a Simkl error, partial media items are rolled back and last_synced_at is committed for the 15m cooldown."""
    async with async_session_factory() as session:
        prov = Provider(
            name="Simkl Failing",
            type="simkl",
            category="watchlist",
            client_id="cid_fail",
            access_token="simkl_at_tok",
            config_json=json.dumps(
                {
                    "sync_interval_minutes": 60,
                    "sync_movies": True,
                    "sync_series": True,
                    "sync_anime": False,
                    "last_synced_at": None,
                }
            ),
            is_active=True,
        )
        session.add(prov)
        await session.commit()
        prov_id = prov.id

    # Movies fetch succeeds, then Shows fetch fails with SimklError (e.g. 412)
    async def fake_watchlist_movies_ok_shows_fail(
        media_type: str, client_id: str, access_token: str
    ) -> list[dict[str, Any]]:
        if media_type == "movies":
            return [
                {
                    "movie": {
                        "title": "Uncommitted Partial Movie",
                        "year": 2025,
                        "ids": {"simkl": 999111, "tmdb": "888111"},
                    }
                }
            ]
        raise simkl.SimklError("412 Precondition Failed: client_id_failed")

    with (
        patch(
            "app.services.watchlists.simkl.get_watchlist",
            side_effect=fake_watchlist_movies_ok_shows_fail,
        ),
        patch(
            "app.services.tmdb.get_movie_details",
            new=AsyncMock(return_value={"release_date": "2025-01-01"}),
        ),
    ):
        await sync_all_providers()

    async with async_session_factory() as session:
        from sqlalchemy import select

        # 1. m55-05: all categories are fetched before any ingest, so a failing fetch leaves nothing partially ingested
        items = (await session.execute(select(MediaItem))).scalars().all()
        assert len(items) == 0

        # 2. Provider last_synced_at must have been persisted so the periodic interval is anchored to it
        updated_prov = await session.get(Provider, prov_id)
        assert updated_prov is not None
        assert updated_prov.watchlist_config.get("last_synced_at") is not None
        now = datetime.now(timezone.utc)
        assert is_provider_periodic_sync_due(updated_prov, now=now) is False
        assert (
            is_provider_periodic_sync_due(updated_prov, now=now + timedelta(minutes=61))
            is True
        )


@pytest.mark.asyncio
async def test_poll_expired_token_and_system_check_reactive_refresh(
    async_client: AsyncClient,
):
    """GET /simkl/auth/poll stops polling on expired_token/access_denied, and GET /system/check/run performs reactive 401 refresh."""
    sess = simkl.create_device_auth_session(
        "cid_exp",
        {
            "device_code": "simkl_dc_expired_test",
            "user_code": "EXPD-0001",
            "verification_uri": "https://simkl.com/pin",
            "verification_uri_complete": "https://simkl.com/pin?user_code=EXPD-0001",
            "expires_in": 900,
            "interval": 5,
        },
    )
    sid = sess["session_id"]

    client = async_client
    if True:
        with patch(
            "app.services.watchlists.simkl.poll_device_token",
            new=AsyncMock(
                side_effect=simkl.SimklOAuthError(
                    400, "expired_token", "Device code expired"
                )
            ),
        ):
            resp = await client.get(f"/simkl/auth/poll?session_id={sid}")
        assert resp.status_code == 200
        assert "expired_token" in resp.text
        assert "hx-trigger" not in resp.text
        assert sid not in simkl._SIMKL_DEVICE_SESSIONS

        # Seed Simkl V2 provider for system check reactive 401 refresh test
        async with async_session_factory() as session:
            prov = Provider(
                name="Simkl SysCheck",
                type="simkl",
                category="watchlist",
                client_id="cid_sys",
                access_token="simkl_at_stale",
                config_json=json.dumps(
                    {
                        "refresh_token": "simkl_rt_valid_sys",
                        "token_expires_at": (
                            datetime.now(timezone.utc) + timedelta(days=3)
                        ).isoformat(),
                    }
                ),
                is_active=True,
            )
            session.add(prov)
            await session.commit()

        get_calls = 0

        async def fake_check_settings(
            client_id: str, access_token: str
        ) -> httpx.Response:
            nonlocal get_calls
            get_calls += 1
            if get_calls == 1:
                assert access_token == "simkl_at_stale"
                return httpx.Response(401, json={"error": "invalid_token"})
            assert access_token == "simkl_at_refreshed_sys"
            return httpx.Response(200, json={"user": {"name": "tester"}})

        new_exp = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        with (
            patch(
                "app.services.watchlists.simkl.check_simkl_user_settings",
                side_effect=fake_check_settings,
            ),
            patch(
                "app.services.watchlists.simkl.refresh_access_token",
                new=AsyncMock(
                    return_value={
                        "access_token": "simkl_at_refreshed_sys",
                        "refresh_token": "simkl_rt_valid_sys",
                        "token_expires_at": new_exp,
                        "expires_in": 604800,
                        "scope": "media:read",
                    }
                ),
            ) as mock_sys_refresh,
        ):
            chk_resp = await client.get("/system/check/run")

        assert chk_resp.status_code == 200
        assert get_calls == 2
        mock_sys_refresh.assert_awaited_once_with("cid_sys", "simkl_rt_valid_sys")
        assert "Simkl" in chk_resp.text
        assert "User: tester" in chk_resp.text
