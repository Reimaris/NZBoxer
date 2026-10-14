"""MyAnimeList client: PKCE OAuth and anime list (m56, ADR-130).

Read-only. Only ``myanimelist.net`` and ``api.myanimelist.net`` are contacted.
No ``client_secret`` is ever sent (public PKCE client).
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx

from app.config import DEFAULT_USER_AGENT

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import Provider

logger = logging.getLogger(__name__)

MAL_AUTHORIZE_URL = "https://myanimelist.net/v1/oauth2/authorize"
MAL_TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"
MAL_USER_URL = "https://api.myanimelist.net/v2/users/@me"
MAL_ANIMELIST_URL = "https://api.myanimelist.net/v2/users/@me/animelist"
REFRESH_BUFFER = timedelta(hours=24)
_TIMEOUT = 20.0
STATE_TTL = timedelta(minutes=15)

# state -> (code_verifier, provider_id, expires_at)
_AUTH_STATES: dict[str, tuple[str, int, datetime]] = {}


def _prune_states() -> None:
    now = datetime.now(timezone.utc)
    for key in [k for k, v in _AUTH_STATES.items() if v[2] <= now]:
        _AUTH_STATES.pop(key, None)


def create_auth_state(code_verifier: str, provider_id: int) -> str:
    """Remember a PKCE verifier under a random OAuth ``state`` (15 min TTL)."""
    _prune_states()
    state = secrets.token_urlsafe(24)
    _AUTH_STATES[state] = (
        code_verifier,
        provider_id,
        datetime.now(timezone.utc) + STATE_TTL,
    )
    return state


def pop_auth_state(state: str) -> tuple[str, int] | None:
    """Consume *state*; ``None`` if unknown or expired."""
    _prune_states()
    entry = _AUTH_STATES.pop(state, None)
    return (entry[0], entry[1]) if entry else None


class MalUnauthorizedError(Exception):
    """Raised by :func:`get_animelist` on HTTP 401 (token rejected)."""


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)``; MAL uses method ``plain``."""
    verifier = secrets.token_urlsafe(64)
    return verifier, verifier


def build_authorize_url(
    client_id: str, code_challenge: str, state: str, redirect_uri: str
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "code_challenge": code_challenge,
            "code_challenge_method": "plain",
            "state": state,
            "redirect_uri": redirect_uri,
        }
    )
    return f"{MAL_AUTHORIZE_URL}?{query}"


def _with_expiry(data: dict[str, Any]) -> dict[str, Any]:
    expires_in = int(data.get("expires_in") or 2592000)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    return {**data, "token_expires_at": expires_at.isoformat()}


async def _token_request(form: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            MAL_TOKEN_URL, data=form, headers={"User-Agent": DEFAULT_USER_AGENT}
        )
        resp.raise_for_status()
        return _with_expiry(resp.json())


async def exchange_code(
    client_id: str, code: str, code_verifier: str, redirect_uri: str
) -> dict[str, Any]:
    return await _token_request(
        {
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        }
    )


async def refresh_access_token(client_id: str, refresh_token: str) -> dict[str, Any]:
    return await _token_request(
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    )


def _expiring_soon(token_expires_at: Any) -> bool:
    if not isinstance(token_expires_at, str) or not token_expires_at:
        return True
    try:
        dt = datetime.fromisoformat(token_expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt - datetime.now(timezone.utc) < REFRESH_BUFFER


async def ensure_valid_mal_token(
    session: AsyncSession, provider: Provider, force: bool = False
) -> str:
    """Return a usable access token, refreshing (and persisting) when needed.

    Raises:
        WatchlistFetchError: no refresh token available or the refresh failed.
    """
    from app.services.watchlist_providers import WatchlistFetchError

    cfg = provider.watchlist_config
    token = str(provider.access_token or "")
    if token and not force and not _expiring_soon(cfg.get("token_expires_at")):
        return token
    refresh = str(cfg.get("refresh_token") or "")
    client_id = str(provider.client_id or cfg.get("client_id") or "")
    if not refresh or not client_id:
        if token and not force:
            return token
        raise WatchlistFetchError("MyAnimeList reconnect required")
    try:
        data = await refresh_access_token(client_id, refresh)
    except (httpx.HTTPError, ValueError) as exc:
        raise WatchlistFetchError("MyAnimeList reconnect required") from exc
    try:
        raw = json.loads(provider.config_json or "{}")
    except ValueError:
        raw = {}
    raw["refresh_token"] = data.get("refresh_token") or refresh
    raw["token_expires_at"] = data["token_expires_at"]
    provider.access_token = str(data["access_token"])
    provider.config_json = json.dumps(raw)
    session.add(provider)
    await session.commit()
    return str(provider.access_token)


async def get_animelist(access_token: str) -> list[dict[str, Any]]:
    """Fetch the full anime list (all statuses), following ``paging.next``.

    Raises:
        MalUnauthorizedError: HTTP 401.
        WatchlistFetchError: rate limit or other HTTP failure.
    """
    from app.services.watchlist_providers import WatchlistFetchError

    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    url: str | None = MAL_ANIMELIST_URL
    params: dict[str, Any] | None = {
        "fields": "list_status,num_episodes,media_type,main_picture",
        "limit": 100,
        "nsfw": "true",
    }
    items: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            while url:
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code == 401:
                    raise MalUnauthorizedError("MyAnimeList token rejected")
                if resp.status_code == 429:
                    retry = resp.headers.get("Retry-After", "unknown")
                    raise WatchlistFetchError(
                        f"MyAnimeList rate limited (Retry-After: {retry})"
                    )
                resp.raise_for_status()
                body = resp.json()
                items.extend(body.get("data") or [])
                url = (body.get("paging") or {}).get("next")
                params = None
    except httpx.HTTPStatusError as exc:
        raise WatchlistFetchError(
            f"MyAnimeList request failed (HTTP {exc.response.status_code})"
        ) from exc
    except (httpx.RequestError, ValueError) as exc:
        raise WatchlistFetchError(
            f"MyAnimeList request failed: {type(exc).__name__}"
        ) from exc
    return items


async def check_account(access_token: str) -> tuple[bool, str]:
    """Diagnostics probe: is the MAL access token accepted?"""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(MAL_USER_URL, headers=headers)
    except httpx.RequestError as exc:
        return False, f"MyAnimeList request failed: {type(exc).__name__}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    try:
        name = (resp.json() or {}).get("name") or "unknown"
    except ValueError:
        name = "unknown"
    return True, f"User: {name}"
