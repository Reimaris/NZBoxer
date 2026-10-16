"""
Tests for TorBox Service and Logging Config
===========================================
Validates TorBox ID parsing (including usenetdownload_id, torrentdownload_id)
and logging start/end format.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

from app.core.logging_config import log_process_end, log_process_start
from app.services.downloaders import torbox


@pytest.mark.asyncio
async def test_send_nzb_file_usenetdownload_id():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {
        "success": True,
        "data": {
            "hash": "0123456789abcdef0123456789abcdef",
            "usenetdownload_id": 1234567,
            "auth_id": "00000000-0000-0000-0000-000000000001",
        },
    }

    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = await torbox.send_nzb_file(
            nzb_bytes=b"<nzb></nzb>",
            filename="test.nzb",
            api_key="test-api-key",
        )

    assert result.get("hash") == "0123456789abcdef0123456789abcdef"
    assert result.get("id") == 1234567


@pytest.mark.asyncio
async def test_send_nzb_file_with_password():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {
        "success": True,
        "data": {
            "hash": "0123456789abcdef0123456789abcdef",
            "usenetdownload_id": 2334188,
        },
    }

    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await torbox.send_nzb_file(
            nzb_bytes=b"<nzb></nzb>",
            filename="protected.nzb",
            api_key="test-api-key",
            password="my_archive_password",
        )

        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        assert kwargs.get("data") == {"password": "my_archive_password"}

    assert result.get("id") == 2334188


@pytest.mark.asyncio
async def test_send_nzb_file_without_password_omits_data_field():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {
        "success": True,
        "data": {
            "hash": "0123456789abcdef0123456789abcdef",
            "usenetdownload_id": 2334189,
        },
    }

    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        result = await torbox.send_nzb_file(
            nzb_bytes=b"<nzb></nzb>",
            filename="plain.nzb",
            api_key="test-api-key",
            password=None,
        )

        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        assert kwargs.get("data") is None

    assert result.get("id") == 2334189


@pytest.mark.asyncio
async def test_send_magnet_link_torrentdownload_id():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {
        "success": True,
        "data": {
            "hash": "magnet123hash",
            "torrentdownload_id": 789012,
        },
    }

    with patch("httpx.AsyncClient.post", return_value=mock_response):
        result = await torbox.send_magnet_link(
            magnet_url="magnet:?xt=urn:btih:magnet123hash",
            api_key="test-api-key",
        )

    assert result.get("hash") == "magnet123hash"
    assert result.get("id") == 789012


def test_logging_process_start_end():
    logger = logging.getLogger("test_logger")
    records = []

    class MemoryHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = MemoryHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        log_process_start(logger, "Test Engine")
        log_process_end(logger, "Test Engine")
        logged_text = "\n".join(r.getMessage() for r in records)
        assert "===== [START] Test Engine " in logged_text
        assert "===== [END] Test Engine " in logged_text
        assert all("\n" not in r.getMessage() for r in records)
    finally:
        logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_html_sanitization_truncation():
    raw_html = (
        "<html><body><h1>502 Bad Gateway</h1><p>Cloudflare</p>"
        + " " * 300
        + "</body></html>"
    )
    clean = torbox._sanitize_error_body(raw_html)
    assert "502 Bad Gateway Cloudflare" in clean
    assert "<" not in clean
    assert ">" not in clean
    assert len(clean) <= 200


@pytest.mark.asyncio
async def test_torbox_retries_and_network_error():
    import httpx

    # Create a mock 502 response
    mock_response = httpx.Response(502, request=httpx.Request("POST", "http://test"))

    # We simulate a 502 error being raised on every call
    def mock_post(*args, **kwargs):
        raise httpx.HTTPStatusError(
            "502 Error", request=mock_response.request, response=mock_response
        )

    with patch("httpx.AsyncClient.post", side_effect=mock_post) as mock_post_method:
        with patch("asyncio.sleep") as mock_sleep:
            with pytest.raises(torbox.DownloaderOutageError):
                await torbox.send_nzb_file(
                    nzb_bytes=b"<nzb></nzb>",
                    filename="test.nzb",
                    api_key="test-api-key",
                )

            # Should have retried up to max_retries (3 times total)
            assert mock_post_method.call_count == 3
            assert mock_sleep.call_count == 2
