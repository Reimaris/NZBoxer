"""Premiumize client tests (m57-05), HTTP mocked via httpx.MockTransport."""

from types import SimpleNamespace

import httpx
import pytest

from app.core.rate_limiter import RollingWindowRateLimiter
from app.services.downloaders import premiumize
from app.services.downloaders.base import TransferState
from app.services.downloaders.factory import get_downloader_client

PMZ_CREATE_OK = {"status": "success", "id": "AbC123", "name": "x.nzb", "type": "nzb"}
PMZ_FAIR_USE = {"status": "error", "message": "Fair use limit reached"}
PMZ_TRANSFER_LIST = {
    "status": "success",
    "transfers": [
        {
            "id": "AbC123",
            "name": "Show.S01E01.1080p",
            "status": "running",
            "progress": 0.5,
            "message": None,
            "folder_id": None,
            "file_id": None,
        },
        {
            "id": "Fin456",
            "name": "Movie.2024",
            "status": "finished",
            "progress": 1,
            "message": "Finished",
            "folder_id": "FoLd789",
            "file_id": None,
        },
        {
            "id": "Err000",
            "name": "Dead.NZB",
            "status": "error",
            "progress": 0,
            "message": "Not enough propagation",
            "folder_id": None,
            "file_id": None,
        },
    ],
}
PMZ_FOLDER_LIST = {
    "status": "success",
    "content": [
        {"id": "F1", "name": "Movie.2024.mkv", "type": "file", "size": 8000000000}
    ],
}


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    premiumize.clear_cooldown()
    monkeypatch.setattr(
        premiumize, "_send_limiter", RollingWindowRateLimiter(60, 3600.0)
    )
    yield
    premiumize.clear_cooldown()


def make_client(monkeypatch, handler):
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(
        premiumize,
        "_client_factory",
        lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(recording), timeout=timeout
        ),
    )
    provider = SimpleNamespace(type="premiumize", api_key="pmz-secret", api_url=None)
    return get_downloader_client(provider), calls  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_connection(monkeypatch):
    client, _ = make_client(
        monkeypatch,
        lambda r: httpx.Response(200, json={"status": "success", "limit_used": 0.12}),
    )
    assert await client.test_connection() == (
        True,
        "Premiumize account OK (12% fair-use used)",
    )
    client, _ = make_client(
        monkeypatch,
        lambda r: httpx.Response(200, json={"status": "error", "message": "bad key"}),
    )
    assert await client.test_connection() == (False, "bad key")


@pytest.mark.anyio
async def test_upload_ok_and_password_warning(monkeypatch, caplog):
    client, calls = make_client(
        monkeypatch, lambda r: httpx.Response(200, json=PMZ_CREATE_OK)
    )
    with caplog.at_level("WARNING"):
        result = await client.upload_nzb(b"<nzb/>", "x.nzb", password="pw")
    assert result.success and result.transfer_id == "AbC123"
    assert "ignoring password" in caplog.text
    assert (
        calls[0].url.path == "/api/transfer/create"
        and b'name="apikey"' in calls[0].content
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [httpx.Response(200, json=PMZ_FAIR_USE), httpx.Response(429, json={})],
)
async def test_fair_use_and_429_set_cooldown(monkeypatch, response):
    client, calls = make_client(monkeypatch, lambda r: response)
    result = await client.upload_nzb(b"x", "x.nzb")
    assert (
        result.is_rate_limited and result.cooldown_seconds == 900 and not result.success
    )
    assert premiumize.get_cooldown_remaining() > 0
    again = await client.upload_nzb(b"x", "x.nzb")
    assert again.is_rate_limited
    assert len(calls) == 1  # second call short-circuited


@pytest.mark.anyio
async def test_upload_error(monkeypatch):
    client, _ = make_client(
        monkeypatch,
        lambda r: httpx.Response(200, json={"status": "error", "message": "nope"}),
    )
    result = await client.upload_nzb(b"x", "x.nzb")
    assert not result.success and not result.is_rate_limited and result.detail == "nope"


@pytest.mark.anyio
async def test_transfer_state_mapping(monkeypatch):
    client, _ = make_client(
        monkeypatch, lambda r: httpx.Response(200, json=PMZ_TRANSFER_LIST)
    )
    t = {x.transfer_id: x for x in await client.get_transfers()}
    assert (t["AbC123"].state, t["AbC123"].progress_pct) == (
        TransferState.DOWNLOADING,
        50.0,
    )
    assert (t["Fin456"].state, t["Fin456"].folder_id) == (
        TransferState.COMPLETED,
        "FoLd789",
    )
    assert (t["Err000"].state, t["Err000"].fail_message) == (
        TransferState.FAILED,
        "Not enough propagation",
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("waiting", TransferState.QUEUED),
        ("queued", TransferState.QUEUED),
        ("pending", TransferState.QUEUED),
        ("seeding", TransferState.DOWNLOADING),
        ("timeout", TransferState.FAILED),
        ("banned", TransferState.FAILED),
        ("deleted", TransferState.FAILED),
    ],
)
async def test_status_table(monkeypatch, status, expected):
    body = {
        "status": "success",
        "transfers": [{"id": "a", "name": "n", "status": status, "progress": 0}],
    }
    client, _ = make_client(monkeypatch, lambda r: httpx.Response(200, json=body))
    assert (await client.get_transfers())[0].state == expected


@pytest.mark.anyio
async def test_get_transfer_files(monkeypatch):
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/transfer/list"):
            return httpx.Response(200, json=PMZ_TRANSFER_LIST)
        assert r.url.params["id"] == "FoLd789"
        return httpx.Response(200, json=PMZ_FOLDER_LIST)

    client, _ = make_client(monkeypatch, handler)
    (f,) = await client.get_transfer_files("Fin456")
    assert (f.name, f.size_bytes) == ("Movie.2024.mkv", 8000000000)
    assert await client.get_transfer_files("AbC123") == []  # no folder yet


@pytest.mark.anyio
async def test_folder_recursion_depth_capped(monkeypatch):
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/transfer/list"):
            return httpx.Response(200, json=PMZ_TRANSFER_LIST)
        return httpx.Response(
            200, json={"content": [{"id": "sub", "name": "d", "type": "folder"}]}
        )

    client, calls = make_client(monkeypatch, handler)
    assert await client.get_transfer_files("Fin456") == []
    assert sum(c.url.path.endswith("/folder/list") for c in calls) == 3


@pytest.mark.anyio
async def test_delete_calls_transfer_and_item(monkeypatch):
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/transfer/list"):
            return httpx.Response(200, json=PMZ_TRANSFER_LIST)
        return httpx.Response(200, json={"status": "success"})

    client, calls = make_client(monkeypatch, handler)
    assert await client.delete_transfer("Fin456")
    paths = [c.url.path for c in calls]
    assert "/api/transfer/delete" in paths and "/api/item/delete" in paths
    assert b"FoLd789" in calls[-1].content

    client, calls = make_client(monkeypatch, handler)
    assert await client.delete_transfer("Fin456", delete_files=False)
    assert [c.url.path for c in calls] == ["/api/transfer/delete"]


@pytest.mark.anyio
async def test_delete_item_falls_back_to_folder_delete(monkeypatch):
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("/transfer/list"):
            return httpx.Response(200, json=PMZ_TRANSFER_LIST)
        if r.url.path.endswith("/item/delete"):
            return httpx.Response(
                200, json={"status": "error", "message": "Item not found"}
            )
        return httpx.Response(200, json={"status": "success"})

    client, calls = make_client(monkeypatch, handler)
    assert await client.delete_transfer("Fin456")
    assert calls[-1].url.path == "/api/folder/delete"


@pytest.mark.anyio
async def test_delete_failure_returns_false(monkeypatch):
    client, _ = make_client(
        monkeypatch,
        lambda r: httpx.Response(200, json={"status": "error", "message": "x"}),
    )
    assert await client.delete_transfer("zzz", delete_files=False) is False
