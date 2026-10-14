"""Plex Cloud client: PIN auth and Discover watchlist (m56, ADR-130).

Read-only. Only ``plex.tv`` and ``discover.provider.plex.tv`` are contacted.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import DEFAULT_USER_AGENT

logger = logging.getLogger(__name__)

PLEX_PRODUCT = "NZBoxer"
PLEX_PINS_URL = "https://plex.tv/api/v2/pins"
PLEX_USER_URL = "https://plex.tv/api/v2/user"
PLEX_WATCHLIST_URL = "https://discover.provider.plex.tv/library/sections/watchlist/all"
PAGE_SIZE = 50
_TIMEOUT = 20.0
SESSION_TTL = timedelta(minutes=15)

_PIN_SESSIONS: dict[str, dict[str, Any]] = {}


def create_pin_session(
    pin: dict[str, Any], client_identifier: str, provider_id: int
) -> dict[str, Any]:
    """Store a PIN server-side under an opaque session_id (15 min TTL)."""
    now = datetime.now(timezone.utc)
    _prune_expired_sessions(now)
    session_id = uuid.uuid4().hex
    record: dict[str, Any] = {
        "session_id": session_id,
        "pin_id": int(pin["id"]),
        "code": str(pin["code"]),
        "client_identifier": client_identifier,
        "provider_id": provider_id,
        "expires_at": now + SESSION_TTL,
    }
    _PIN_SESSIONS[session_id] = record
    return record


def get_pin_session(session_id: str) -> dict[str, Any] | None:
    """Return an active PIN session, ``None`` if missing or expired."""
    if not session_id:
        return None
    _prune_expired_sessions(datetime.now(timezone.utc))
    return _PIN_SESSIONS.get(session_id)


def pop_pin_session(session_id: str) -> dict[str, Any] | None:
    return _PIN_SESSIONS.pop(session_id, None)


def _prune_expired_sessions(now: datetime) -> None:
    for sid in [s for s, r in _PIN_SESSIONS.items() if r["expires_at"] <= now]:
        _PIN_SESSIONS.pop(sid, None)


def build_auth_url(client_identifier: str, code: str) -> str:
    return (
        f"https://app.plex.tv/auth#?clientID={client_identifier}&code={code}"
        f"&context[device][product]={PLEX_PRODUCT}"
    )


def _headers(client_identifier: str, token: str | None = None) -> dict[str, str]:
    headers = {
        "X-Plex-Client-Identifier": client_identifier,
        "X-Plex-Product": PLEX_PRODUCT,
        "Accept": "application/json",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


async def create_pin(client_identifier: str) -> dict[str, Any]:
    """Create a strong PIN; returns ``{"id": int, "code": str}``."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            PLEX_PINS_URL,
            params={"strong": "true"},
            headers=_headers(client_identifier),
        )
        resp.raise_for_status()
        data = resp.json()
    return {"id": int(data["id"]), "code": str(data["code"])}


async def poll_pin(pin_id: int, code: str, client_identifier: str) -> str | None:
    """Return the ``authToken`` once the PIN is claimed, ``None`` while pending."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{PLEX_PINS_URL}/{pin_id}",
            params={"code": code},
            headers=_headers(client_identifier),
        )
        resp.raise_for_status()
        token = resp.json().get("authToken")
    return str(token) if token else None


async def get_watchlist(
    auth_token: str, client_identifier: str
) -> list[dict[str, Any]]:
    """Fetch every Discover watchlist item (paginated, size 50).

    Raises:
        WatchlistFetchError: 401 (token invalid), rate limit or HTTP failure.
    """
    from app.services.watchlist_providers import WatchlistFetchError

    items: list[dict[str, Any]] = []
    start = 0
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            while True:
                resp = await client.get(
                    PLEX_WATCHLIST_URL,
                    params={
                        "includeGuids": "1",
                        "X-Plex-Container-Start": start,
                        "X-Plex-Container-Size": PAGE_SIZE,
                    },
                    headers=_headers(client_identifier, auth_token),
                )
                if resp.status_code == 401:
                    raise WatchlistFetchError("Plex token invalid — reconnect required")
                if resp.status_code == 429:
                    retry = resp.headers.get("Retry-After", "unknown")
                    raise WatchlistFetchError(
                        f"Plex rate limited (Retry-After: {retry})"
                    )
                resp.raise_for_status()
                try:
                    container = resp.json().get("MediaContainer") or {}
                except ValueError as exc:
                    raise WatchlistFetchError(
                        "Plex returned an invalid response"
                    ) from exc
                page = container.get("Metadata") or []
                items.extend(page)
                total = int(container.get("totalSize") or 0)
                start += PAGE_SIZE
                if len(page) < PAGE_SIZE or (total and start >= total):
                    break
    except httpx.HTTPStatusError as exc:
        raise WatchlistFetchError(
            f"Plex request failed (HTTP {exc.response.status_code})"
        ) from exc
    except httpx.RequestError as exc:
        raise WatchlistFetchError(f"Plex request failed: {type(exc).__name__}") from exc
    return items


async def check_account(auth_token: str, client_identifier: str) -> tuple[bool, str]:
    """Diagnostics probe: is the Plex auth token still valid?"""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                PLEX_USER_URL, headers=_headers(client_identifier, auth_token)
            )
    except httpx.RequestError as exc:
        return False, f"Plex request failed: {type(exc).__name__}"
    if resp.status_code == 401:
        return False, "Reconnect required"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    try:
        name = (resp.json() or {}).get("username") or "unknown"
    except ValueError:
        name = "unknown"
    return True, f"Plex account: {name}"
