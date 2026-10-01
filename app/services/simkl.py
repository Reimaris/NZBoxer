"""
Simkl API Client (AUTH V2 — RFC 8628 Device Flow & Token Lifecycle)
===================================================================
Handles Simkl OAuth 2.0 Device Authorization (`POST /oauth2/device` & `POST /oauth2/token`),
server-side `device_code` session isolation, non-rotating `refresh_token` management,
best-effort RFC 7009 token revocation (`POST /oauth2/revoke`), and watchlist synchronization.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, TypedDict

import httpx

from app.config import DEFAULT_USER_AGENT
from app.core.rate_limiter import RateLimiter
from app.version import APP_VERSION

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import Provider

logger = logging.getLogger(__name__)
_limiter = RateLimiter(1.0)

SIMKL_BASE_URL = "https://api.simkl.com"
SIMKL_APP_NAME = "nzboxer"
SIMKL_APP_VERSION = APP_VERSION
SIMKL_DEFAULT_SCOPE = "media:read"
PROACTIVE_REFRESH_BUFFER_SECONDS = 86400  # 24 hours


class SimklDeviceAuthResponse(TypedDict, total=False):
    """Typed payload returned by POST https://api.simkl.com/oauth2/device."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


class SimklDeviceAuthSession(TypedDict, total=False):
    """Server-side in-memory record keyed by session_id (UUID4)."""

    session_id: str
    client_id: str
    provider_id: int | None
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    interval: int
    created_at: datetime
    expires_at: datetime


class SimklTokenResponse(TypedDict, total=False):
    """Typed payload returned by POST https://api.simkl.com/oauth2/token."""

    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str
    scope: str
    token_expires_at: str


class SimklError(Exception):
    """Base exception for Simkl API errors."""


class SimklUnauthorizedError(SimklError):
    """Raised when Simkl responds with HTTP 401 (invalid_token / user_token_failed)."""


class SimklOAuthError(SimklError):
    """Domain exception encapsulating RFC 6749 §5.2 / RFC 8628 §3.5 OAuth error responses."""

    def __init__(
        self,
        status_code: int,
        error: str,
        error_description: str = "",
    ) -> None:
        self.status_code = status_code
        self.error = error
        self.error_description = error_description
        msg = f"Simkl OAuth error ({status_code} {error})"
        if error_description:
            msg = f"{msg}: {error_description}"
        super().__init__(msg)


# Server-side in-memory store for active RFC 8628 device authorization sessions
_SIMKL_DEVICE_SESSIONS: dict[str, dict[str, Any]] = {}


def _build_headers(client_id: str, access_token: str | None = None) -> dict[str, str]:
    """Build standard Simkl AUTH V2 HTTP headers."""
    headers: dict[str, str] = {
        "simkl-api-key": client_id,
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def _build_base_params(client_id: str) -> dict[str, str]:
    """Build standard Simkl AUTH V2 query parameters."""
    return {
        "client_id": client_id,
        "app-name": SIMKL_APP_NAME,
        "app-version": SIMKL_APP_VERSION,
    }


def _parse_oauth_error(response: httpx.Response) -> SimklOAuthError:
    """Extract RFC 6749 / RFC 8628 error and error_description from a non-200 HTTP response."""
    err_code = f"http_{response.status_code}"
    err_desc = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            err_code = str(
                payload.get("error") or payload.get("result") or err_code
            ).strip()
            err_desc = str(
                payload.get("error_description") or payload.get("message") or ""
            ).strip()
    except Exception:
        err_desc = (response.text or "")[:200].strip()

    return SimklOAuthError(
        status_code=response.status_code,
        error=err_code,
        error_description=err_desc,
    )


def _prune_expired_sessions(now: datetime | None = None) -> None:
    """Remove expired device authorization sessions from memory."""
    if now is None:
        now = datetime.now(timezone.utc)
    expired_keys = [
        sid
        for sid, sess in _SIMKL_DEVICE_SESSIONS.items()
        if isinstance(sess.get("expires_at"), datetime) and sess["expires_at"] <= now
    ]
    for sid in expired_keys:
        _SIMKL_DEVICE_SESSIONS.pop(sid, None)


def create_device_auth_session(
    client_id: str,
    device_payload: dict[str, Any],
    provider_id: int | None = None,
) -> dict[str, Any]:
    """Store a device_code server-side under a short-lived opaque session_id (15m TTL)."""
    now = datetime.now(timezone.utc)
    _prune_expired_sessions(now)
    session_id = uuid.uuid4().hex
    expires_in = int(device_payload.get("expires_in", 900) or 900)
    interval = int(device_payload.get("interval", 5) or 5)
    user_code = str(device_payload.get("user_code") or "")
    verification_uri = str(
        device_payload.get("verification_uri") or "https://simkl.com/pin"
    )
    verification_uri_complete = str(
        device_payload.get("verification_uri_complete")
        or f"{verification_uri}?user_code={user_code}"
    )

    session_record: dict[str, Any] = {
        "session_id": session_id,
        "client_id": client_id,
        "provider_id": provider_id,
        "device_code": str(device_payload.get("device_code") or ""),
        "user_code": user_code,
        "verification_uri": verification_uri,
        "verification_uri_complete": verification_uri_complete,
        "interval": interval,
        "created_at": now,
        "expires_at": now + timedelta(seconds=expires_in),
    }
    _SIMKL_DEVICE_SESSIONS[session_id] = session_record
    return session_record


def get_device_auth_session(
    session_id: str, now: datetime | None = None
) -> dict[str, Any] | None:
    """Retrieve an active device auth session by session_id, returning None if missing or expired."""
    if not session_id:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    _prune_expired_sessions(now)
    return _SIMKL_DEVICE_SESSIONS.get(session_id)


def pop_device_auth_session(session_id: str) -> dict[str, Any] | None:
    """Remove and return a device auth session by session_id."""
    return _SIMKL_DEVICE_SESSIONS.pop(session_id, None)


async def request_device_code(
    client_id: str, scope: str = SIMKL_DEFAULT_SCOPE
) -> dict[str, Any]:
    """Initiate Simkl AUTH V2 RFC 8628 Device Flow via POST /oauth2/device."""
    url = f"{SIMKL_BASE_URL}/oauth2/device"
    params = _build_base_params(client_id)
    headers = _build_headers(client_id)
    form_data = {"client_id": client_id, "scope": scope}

    await _limiter.wait()
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            url, params=params, headers=headers, data=form_data
        )
        if response.status_code != 200:
            raise _parse_oauth_error(response)

        data = response.json()
        user_code = str(data.get("user_code") or "")
        verification_uri = str(data.get("verification_uri") or "https://simkl.com/pin")
        verification_uri_complete = str(
            data.get("verification_uri_complete")
            or f"{verification_uri}?user_code={user_code}"
        )
        return {
            "device_code": str(data.get("device_code") or ""),
            "user_code": user_code,
            "verification_uri": verification_uri,
            "verification_uri_complete": verification_uri_complete,
            "expires_in": int(data.get("expires_in", 900) or 900),
            "interval": int(data.get("interval", 5) or 5),
        }


async def poll_device_token(client_id: str, device_code: str) -> dict[str, Any]:
    """Poll Simkl AUTH V2 token endpoint for RFC 8628 device_code exchange."""
    url = f"{SIMKL_BASE_URL}/oauth2/token"
    params = _build_base_params(client_id)
    headers = _build_headers(client_id)
    form_data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": client_id,
        "device_code": device_code,
    }

    await _limiter.wait()
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            url, params=params, headers=headers, data=form_data
        )
        if response.status_code != 200:
            raise _parse_oauth_error(response)

        data = response.json()
        expires_in = int(data.get("expires_in", 604800) or 604800)
        token_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ).isoformat()
        return {
            **data,
            "expires_in": expires_in,
            "token_expires_at": token_expires_at,
        }


async def refresh_access_token(client_id: str, refresh_token: str) -> dict[str, Any]:
    """Exchange a Simkl AUTH V2 refresh_token for a new 7-day access_token via POST /oauth2/token."""
    url = f"{SIMKL_BASE_URL}/oauth2/token"
    params = _build_base_params(client_id)
    headers = _build_headers(client_id)
    form_data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }

    await _limiter.wait()
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            url, params=params, headers=headers, data=form_data
        )
        if response.status_code != 200:
            raise _parse_oauth_error(response)

        data = response.json()
        expires_in = int(data.get("expires_in", 604800) or 604800)
        token_expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ).isoformat()
        preserved_refresh = str(data.get("refresh_token") or refresh_token)
        return {
            "access_token": str(data["access_token"]),
            "refresh_token": preserved_refresh,
            "token_expires_at": token_expires_at,
            "expires_in": expires_in,
            "scope": str(data.get("scope") or SIMKL_DEFAULT_SCOPE),
            "token_type": str(data.get("token_type") or "Bearer"),
        }


async def revoke_token(client_id: str, token: str) -> bool:
    """Best-effort non-blocking RFC 7009 token revocation at POST /oauth2/revoke."""
    if not client_id or not token:
        return False

    url = f"{SIMKL_BASE_URL}/oauth2/revoke"
    params = _build_base_params(client_id)
    headers = _build_headers(client_id)
    form_data = {"client_id": client_id, "token": token}

    try:
        await _limiter.wait()
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url, params=params, headers=headers, data=form_data
            )
            return response.status_code == 200
    except Exception as e:  # noqa: BLE001
        logger.warning("Best-effort Simkl token revocation failed: %s", e)
        return False


def is_token_expiring_soon(
    token_expires_at: str | None,
    buffer_seconds: int = PROACTIVE_REFRESH_BUFFER_SECONDS,
    now: datetime | None = None,
) -> bool:
    """Return True if token_expires_at is None, unparseable, or within buffer_seconds (default 24h)."""
    if not token_expires_at or not isinstance(token_expires_at, str):
        return True
    try:
        expires_dt = datetime.fromisoformat(token_expires_at.replace("Z", "+00:00"))
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    return (expires_dt - now).total_seconds() < buffer_seconds


async def refresh_provider_simkl_token(
    session: AsyncSession, provider: Provider
) -> tuple[str, str]:
    """Force an immediate refresh_token exchange for a Simkl Provider and persist to SQLite."""
    cfg = provider.simkl_config
    client_id = str(provider.client_id or cfg.get("client_id") or "").strip()
    refresh_token = str(cfg.get("refresh_token") or "").strip()

    if not client_id or not refresh_token:
        return client_id, str(
            provider.access_token or cfg.get("access_token") or ""
        ).strip()

    try:
        token_data = await refresh_access_token(client_id, refresh_token)
    except SimklOAuthError as exc:
        if exc.error == "invalid_grant" or exc.status_code == 400:
            logger.warning(
                "Simkl refresh_token invalid_grant for provider %s; clearing refresh_token and marking Reconnect Required.",
                provider.name,
            )
            cfg["refresh_token"] = None
            cfg["token_expires_at"] = None
            provider.config_json = json.dumps(cfg)
            await session.commit()
        raise

    new_access = str(token_data["access_token"])
    new_refresh = str(token_data.get("refresh_token") or refresh_token)
    new_expires_at = str(token_data["token_expires_at"])

    provider.client_id = client_id
    provider.access_token = new_access
    cfg["client_id"] = client_id
    cfg["access_token"] = new_access
    cfg["refresh_token"] = new_refresh
    cfg["token_expires_at"] = new_expires_at
    provider.config_json = json.dumps(cfg)
    await session.commit()

    return client_id, new_access


async def ensure_valid_simkl_token(
    session: AsyncSession,
    provider: Provider,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Ensure the Simkl provider has a valid access_token, refreshing proactively if <24h remain."""
    cfg = provider.simkl_config
    client_id = str(provider.client_id or cfg.get("client_id") or "").strip()
    access_token = str(provider.access_token or cfg.get("access_token") or "").strip()
    refresh_token = str(cfg.get("refresh_token") or "").strip()
    token_expires_at = cfg.get("token_expires_at")

    if refresh_token and (
        not access_token or is_token_expiring_soon(token_expires_at, now=now)
    ):
        return await refresh_provider_simkl_token(session, provider)

    return client_id, access_token


async def request_pin(client_id: str) -> dict[str, Any]:
    """Backwards-compatible alias forwarding to Simkl AUTH V2 request_device_code."""
    return await request_device_code(client_id)


async def check_pin(client_id: str, device_code: str) -> dict[str, Any]:
    """Backwards-compatible alias forwarding to Simkl AUTH V2 poll_device_token."""
    return await poll_device_token(client_id, device_code)


async def get_watchlist(
    media_type: str, client_id: str, access_token: str
) -> list[dict[str, Any]]:
    """Fetch the 'plantowatch' and 'watching' list for movies, shows, or anime.

    Args:
        media_type: 'movies', 'shows', or 'anime'.
        client_id: Simkl Client ID
        access_token: Simkl Access Token

    Returns:
        List of dictionaries containing item metadata from Simkl.
    """
    if not client_id or not access_token:
        logger.warning("Simkl API keys missing; returning empty watchlist.")
        return []

    params = _build_base_params(client_id)
    headers = _build_headers(client_id, access_token)
    headers["Content-Type"] = "application/json"

    statuses = ["plantowatch", "watching"]
    all_items: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        for status in statuses:
            url = f"{SIMKL_BASE_URL}/sync/all-items/{media_type}/{status}"
            await _limiter.wait()
            try:
                response = await client.get(url, params=params, headers=headers)
                if response.status_code == 401:
                    raise SimklUnauthorizedError(
                        f"Simkl returned 401 Unauthorized for {media_type} {status}"
                    )
                response.raise_for_status()
                data = response.json()
                items = data.get(media_type, []) if isinstance(data, dict) else []
                all_items.extend(items)
            except SimklUnauthorizedError:
                raise
            except Exception as e:
                logger.error("Simkl API error for %s %s: %s", media_type, status, e)
                raise SimklError(
                    f"Simkl sync failed for {media_type} {status}: {e}"
                ) from e

    return all_items


async def check_simkl_user_settings(
    client_id: str, access_token: str
) -> httpx.Response:
    """Query GET /users/settings on Simkl with standard AUTH V2 params and headers."""
    url = f"{SIMKL_BASE_URL}/users/settings"
    params = _build_base_params(client_id)
    headers = _build_headers(client_id, access_token)
    await _limiter.wait()
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.get(url, params=params, headers=headers)


fetch_watchlist = get_watchlist
