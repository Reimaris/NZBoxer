"""Downloader upload outage/auth results across adapters (ADR-158), HTTP via MockTransport."""

from types import SimpleNamespace

import httpx
import pytest

from app.core.rate_limiter import RollingWindowRateLimiter
from app.services.downloaders import premiumize, sabnzbd, torbox
from app.services.downloaders.factory import get_downloader_client


def _connect_error(request):
    raise httpx.ConnectError("down", request=request)


def _upload_ok(request):
    return httpx.Response(200, json={"status": "success", "id": "1"})


@pytest.fixture
def make_client(monkeypatch):
    """Return build(adapter, handler) -> downloader client wired to a MockTransport."""
    real = httpx.AsyncClient

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(torbox.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        torbox, "_auto_send_limiter", RollingWindowRateLimiter(50, 3600.0)
    )
    monkeypatch.setattr(
        premiumize, "_send_limiter", RollingWindowRateLimiter(60, 3600.0)
    )
    premiumize.clear_cooldown()

    def build(adapter: str, handler):
        def factory(*args, **kwargs):
            kwargs.pop("headers", None)
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)

        if adapter == "torbox":
            monkeypatch.setattr(torbox.httpx, "AsyncClient", factory)
            provider = SimpleNamespace(type="torbox", api_key="k", api_url=None)
        elif adapter == "sabnzbd":
            monkeypatch.setattr(sabnzbd.httpx, "AsyncClient", factory)
            provider = SimpleNamespace(
                type="sabnzbd", api_url="http://sab:8080/sabnzbd", api_key="k"
            )
        else:
            monkeypatch.setattr(premiumize.httpx, "AsyncClient", factory)
            provider = SimpleNamespace(type="premiumize", api_key="k", api_url=None)
        return get_downloader_client(provider)  # type: ignore[arg-type]

    yield build
    premiumize.clear_cooldown()


ADAPTERS = ["torbox", "sabnzbd", "premiumize"]


@pytest.mark.anyio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_502_is_outage(make_client, adapter):
    client = make_client(adapter, lambda r: httpx.Response(502))
    res = await client.upload_nzb(b"x", "a.nzb")
    assert (res.success, res.is_outage, res.outage_label) == (False, True, "HTTP 502")
    assert not res.is_auth_error and not res.is_rate_limited


@pytest.mark.anyio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_connect_error_is_outage(make_client, adapter):
    client = make_client(adapter, _connect_error)
    res = await client.upload_nzb(b"x", "a.nzb")
    assert res.is_outage and res.outage_label == "ConnectError"


@pytest.mark.anyio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_401_is_auth_error(make_client, adapter):
    client = make_client(adapter, lambda r: httpx.Response(401))
    res = await client.upload_nzb(b"x", "a.nzb")
    assert res.is_auth_error and not res.is_outage and not res.success


@pytest.mark.anyio
@pytest.mark.parametrize("adapter", ["torbox", "premiumize"])
async def test_429_stays_rate_limited(make_client, adapter):
    client = make_client(adapter, lambda r: httpx.Response(429))
    res = await client.upload_nzb(b"x", "a.nzb")
    assert res.is_rate_limited and not res.is_outage
    torbox.clear_cooldown()
