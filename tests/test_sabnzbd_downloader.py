"""SABnzbd client tests (m57-04), HTTP mocked via httpx.MockTransport."""

from types import SimpleNamespace

import httpx
import pytest

from app.services.downloaders import sabnzbd
from app.services.downloaders.base import DownloaderNetworkError, TransferState
from app.services.downloaders.factory import get_downloader_client

SAB_ADDFILE_OK = {"status": True, "nzo_ids": ["SABnzbd_nzo_x1y2z3"]}
SAB_ADDFILE_ERR = {"status": False, "error": "API Key Incorrect"}
SAB_QUEUE = {
    "queue": {
        "kbpersec": "2048.0",
        "slots": [
            {
                "nzo_id": "SABnzbd_nzo_x1y2z3",
                "filename": "Show.S01E01.1080p.WEB-DL",
                "status": "Downloading",
                "percentage": "42",
                "mb": "1500.00",
                "mbleft": "870.00",
                "timeleft": "0:04:10",
                "cat": "tv",
            }
        ],
    }
}
SAB_HISTORY = {
    "history": {
        "slots": [
            {
                "nzo_id": "SABnzbd_nzo_ok1",
                "name": "Movie.2024.2160p",
                "status": "Completed",
                "fail_message": "",
                "storage": "/downloads/complete/movies/Movie.2024.2160p",
                "bytes": 8000000000,
            },
            {
                "nzo_id": "SABnzbd_nzo_bad1",
                "name": "Broken.Release",
                "status": "Failed",
                "fail_message": "Aborted, cannot be completed - https://sabnzbd.org/not-complete",
                "storage": "",
                "bytes": 0,
            },
        ]
    }
}
SAB_DELETE_OK = {"status": True}


def make_client(monkeypatch, handler, api_url="http://sab:8080/sabnzbd"):
    monkeypatch.setattr(
        sabnzbd,
        "_client_factory",
        lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )
    provider = SimpleNamespace(type="sabnzbd", api_url=api_url, api_key="sekret")
    return get_downloader_client(provider)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://h:8080/sabnzbd", "http://h:8080/sabnzbd/api"),
        ("http://h:8080/sabnzbd/api", "http://h:8080/sabnzbd/api"),
        (" http://h:8080/sabnzbd/ ", "http://h:8080/sabnzbd/api"),
        (None, ""),
    ],
)
def test_api_url_normalized(raw, expected):
    assert sabnzbd.normalize_api_url(raw) == expected


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("category", "password"), [("tv", "secret"), ("", None), (None, "")]
)
async def test_upload_fields(monkeypatch, category, password):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        seen["body"] = request.content
        return httpx.Response(200, json=SAB_ADDFILE_OK)

    client = make_client(monkeypatch, handler)
    result = await client.upload_nzb(b"<nzb/>", "Show.S01E01.nzb", password, category)
    assert result.success and result.transfer_id == "SABnzbd_nzo_x1y2z3"
    assert seen["params"] == {"mode": "addfile", "output": "json"}
    body = seen["body"]
    assert b'name="nzbname"\r\n\r\nShow.S01E01\r\n' in body
    assert b'name="apikey"' in body and b'filename="Show.S01E01.nzb"' in body
    assert (b'name="cat"' in body) is bool(category)
    assert (b'name="nzbpassword"' in body) is bool(password)


@pytest.mark.anyio
async def test_upload_failures(monkeypatch):
    client = make_client(
        monkeypatch, lambda r: httpx.Response(200, json=SAB_ADDFILE_ERR)
    )
    result = await client.upload_nzb(b"x", "a.nzb")
    assert not result.success and result.detail == "API Key Incorrect"

    client = make_client(
        monkeypatch, lambda r: httpx.Response(200, json={"status": True, "nzo_ids": []})
    )
    result = await client.upload_nzb(b"x", "a.nzb")
    assert result.detail == "SABnzbd accepted request but returned no nzo_id"


@pytest.mark.anyio
async def test_test_connection(monkeypatch):
    def ok(request: httpx.Request) -> httpx.Response:
        if request.url.params["mode"] == "version":
            return httpx.Response(200, json={"version": "4.3.3"})
        return httpx.Response(200, json=SAB_QUEUE)

    assert await make_client(monkeypatch, ok).test_connection() == (
        True,
        "SABnzbd 4.3.3",
    )

    def bad_key(request: httpx.Request) -> httpx.Response:
        if request.url.params["mode"] == "version":
            return httpx.Response(200, json={"version": "4.3.3"})
        return httpx.Response(200, json=SAB_ADDFILE_ERR)

    assert await make_client(monkeypatch, bad_key).test_connection() == (
        False,
        "SABnzbd API key rejected",
    )

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    ok_, msg = await make_client(monkeypatch, down).test_connection()
    assert not ok_ and msg.startswith("SABnzbd unreachable")
    assert "sekret" not in msg


@pytest.mark.anyio
async def test_state_mapping_queue_and_history(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        mode = request.url.params["mode"]
        if mode == "queue":
            return httpx.Response(200, json=SAB_QUEUE)
        assert request.url.params["limit"] == "200"
        return httpx.Response(200, json=SAB_HISTORY)

    transfers = {
        t.transfer_id: t
        for t in await make_client(monkeypatch, handler).get_transfers()
    }
    q = transfers["SABnzbd_nzo_x1y2z3"]
    assert (q.state, q.progress_pct, q.eta_seconds) == (
        TransferState.DOWNLOADING,
        42.0,
        250,
    )
    assert q.speed_bytes == 2048 * 1024 and q.size_bytes == 1500 * 1024 * 1024
    ok = transfers["SABnzbd_nzo_ok1"]
    assert (ok.state, ok.progress_pct, ok.size_bytes) == (
        TransferState.COMPLETED,
        100.0,
        8000000000,
    )
    bad = transfers["SABnzbd_nzo_bad1"]
    assert bad.state == TransferState.FAILED and "not-complete" in (
        bad.fail_message or ""
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("Queued", TransferState.QUEUED),
        ("Paused", TransferState.QUEUED),
        ("Propagating", TransferState.QUEUED),
        ("Fetching", TransferState.QUEUED),
        ("Grabbing", TransferState.DOWNLOADING),
        ("Checking", TransferState.UNPACKING),
        ("Repairing", TransferState.UNPACKING),
        ("Verifying", TransferState.UNPACKING),
        ("Extracting", TransferState.UNPACKING),
    ],
)
async def test_queue_status_table(monkeypatch, status, expected):
    slot = {
        "nzo_id": "n1",
        "filename": "x",
        "status": status,
        "percentage": "5",
        "mb": "1",
        "timeleft": "0:00:00",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["mode"] == "queue":
            return httpx.Response(
                200, json={"queue": {"kbpersec": "0", "slots": [slot]}}
            )
        return httpx.Response(200, json={"history": {"slots": []}})

    (t,) = await make_client(monkeypatch, handler).get_transfers()
    assert t.state == expected


@pytest.mark.anyio
async def test_history_postprocessing_and_queue_wins(monkeypatch):
    hist = {
        "history": {
            "slots": [
                {"nzo_id": "n1", "name": "dup", "status": "Completed", "bytes": 1},
                {"nzo_id": "n2", "name": "pp", "status": "Extracting", "bytes": 1},
            ]
        }
    }
    queue = {
        "queue": {
            "kbpersec": "0",
            "slots": [
                {
                    "nzo_id": "n1",
                    "filename": "dup",
                    "status": "Queued",
                    "percentage": "0",
                    "mb": "1",
                    "timeleft": "0:00:00",
                }
            ],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=queue if request.url.params["mode"] == "queue" else hist
        )

    transfers = {
        t.transfer_id: t
        for t in await make_client(monkeypatch, handler).get_transfers()
    }
    assert transfers["n1"].state == TransferState.QUEUED
    assert transfers["n2"].state == TransferState.UNPACKING


@pytest.mark.anyio
async def test_get_transfers_network_error(monkeypatch):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(DownloaderNetworkError):
        await make_client(monkeypatch, down).get_transfers()


@pytest.mark.anyio
async def test_get_transfer_files(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["mode"] == "get_files"
        assert request.url.params["value"] == "n1"
        return httpx.Response(
            200, json={"files": [{"filename": "a.mkv", "bytes": "10"}]}
        )

    (f,) = await make_client(monkeypatch, handler).get_transfer_files("n1")
    assert (f.name, f.size_bytes) == ("a.mkv", 10)


@pytest.mark.anyio
async def test_delete_queue_hit_skips_history(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        return httpx.Response(200, json=SAB_DELETE_OK)

    assert await make_client(monkeypatch, handler).delete_transfer("n1")
    assert [c["mode"] for c in calls] == ["queue"]
    assert calls[0]["del_files"] == "1" and calls[0]["name"] == "delete"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("history_ok", "delete_files"), [(True, True), (False, True), (True, False)]
)
async def test_delete_queue_miss_falls_through(monkeypatch, history_ok, delete_files):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        ok = request.url.params["mode"] == "history" and history_ok
        return httpx.Response(200, json={"status": ok})

    assert (
        await make_client(monkeypatch, handler).delete_transfer("n1", delete_files)
        is history_ok
    )
    assert [c["mode"] for c in calls] == ["queue", "history"]
    assert all(("del_files" in c) is delete_files for c in calls)


@pytest.mark.parametrize(
    ("host", "port", "ssl", "url"),
    [
        ("sab.local", 8080, False, "http://sab.local:8080/sabnzbd/api"),
        (" 10.0.0.2 ", 9443, True, "https://10.0.0.2:9443/sabnzbd/api"),
    ],
)
def test_sabnzbd_api_url_round_trip(host, port, ssl, url):
    assert sabnzbd.build_sabnzbd_api_url(host, port, ssl) == url
    assert sabnzbd.parse_sabnzbd_api_url(url) == (host.strip(), port, ssl)


@pytest.mark.parametrize(
    ("api_url", "expected"),
    [
        (None, ("", 8080, False)),
        ("", ("", 8080, False)),
        ("http://sab/sabnzbd/api", ("sab", 8080, False)),
        ("https://sab/sabnzbd/api", ("sab", 443, True)),
    ],
)
def test_parse_sabnzbd_api_url_defaults(api_url, expected):
    assert sabnzbd.parse_sabnzbd_api_url(api_url) == expected
