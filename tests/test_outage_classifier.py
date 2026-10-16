import json
import ssl

import httpx
import pytest

from app.core import outage
from app.core.rate_limit_queue import active_rate_limit_sources
from app.services import outage_classifier as oc
from app.services.indexers import newznab


@pytest.mark.parametrize("status", [500, 502, 503, 504, 520, 525, 530])
def test_http_outage_statuses(status):
    v = oc.classify_http_status(status)
    assert v == oc.OutageVerdict("outage", f"HTTP {status}", status)


@pytest.mark.parametrize("status", [400, 404, 418, 429])
def test_http_non_outage_statuses(status):
    assert oc.classify_http_status(status) is None


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_statuses(status):
    v = oc.classify_http_status(status)
    assert v is not None and v.kind == "auth"


def _ssl_connect_error():
    exc = httpx.ConnectError("tls")
    exc.__cause__ = ssl.SSLCertVerificationError("bad cert")
    return exc


@pytest.mark.parametrize(
    "exc,label",
    [
        (httpx.ConnectTimeout("t"), "ConnectTimeout"),
        (httpx.ReadTimeout("t"), "ReadTimeout"),
        (httpx.WriteTimeout("t"), "WriteTimeout"),
        (httpx.PoolTimeout("t"), "PoolTimeout"),
        (httpx.ConnectError("c"), "ConnectError"),
        (ConnectionRefusedError(), "ConnectError"),
        (ssl.SSLCertVerificationError("x"), "SSLCertVerificationError"),
        (_ssl_connect_error(), "SSLCertVerificationError"),
        (httpx.TooManyRedirects("r"), "TooManyRedirects"),
        (json.JSONDecodeError("m", "d", 0), "JSONDecodeError"),
    ],
)
def test_exception_outages(exc, label):
    v = oc.classify_exception(exc)
    assert v is not None and v.kind == "outage" and v.label == label


def _status_error(code):
    req = httpx.Request("GET", "http://x")
    return httpx.HTTPStatusError(
        "e", request=req, response=httpx.Response(code, request=req)
    )


def test_exception_http_status_error_and_unknown():
    assert oc.classify_exception(_status_error(503)).label == "HTTP 503"
    assert oc.classify_exception(_status_error(401)).kind == "auth"
    assert oc.classify_exception(_status_error(429)) is None
    assert oc.classify_exception(ValueError("x")) is None


@pytest.mark.parametrize(
    "payload,kind",
    [
        (
            {
                "error": {
                    "@attributes": {
                        "code": "100",
                        "description": "Incorrect user credentials",
                    }
                }
            },
            "auth",
        ),
        ({"error": {"@attributes": {"code": "500"}}}, "outage"),
        ({"@attributes": {"code": "501"}}, "outage"),
        ({"@attributes": {"code": "200"}}, None),
        ({"@attributes": {"offset": "0"}}, None),
        ([], None),
    ],
)
def test_newznab_payloads(payload, kind):
    v = oc.classify_newznab_error(payload)
    assert (v.kind if v else None) == kind


def test_non_json_body():
    v = oc.classify_non_json_body(
        "text/html", "<html><title>Maintenance</title></html>"
    )
    assert v.kind == "outage" and v.label == "Non-JSON response"


def test_backoff_schedule():
    assert [outage.outage_backoff_seconds(a) for a in range(1, 8)] == [
        600,
        1200,
        1800,
        1800,
        1800,
        1800,
        1800,
    ]


@pytest.mark.parametrize(
    "detail,expected",
    [
        ("rate_limited", True),
        (" Rate_Limited ", True),
        ("paused: indexer outage (HTTP 503)", True),
        ("failed: indexer outage (5 attempts failed)", False),
        ("completed", False),
        (None, False),
    ],
)
def test_is_queued_detail(detail, expected):
    assert outage.is_queued_detail(detail) is expected


def test_detail_strings():
    assert (
        outage.paused_detail("indexer", "HTTP 503")
        == "paused: indexer outage (HTTP 503)"
    )
    assert (
        outage.terminal_detail("indexer")
        == "failed: indexer outage (5 attempts failed)"
    )


def test_marker_round_trip():
    m = outage.make_outage_marker("indexer", "NZBgeek", "HTTP 503")
    assert m == "outage|indexer|NZBgeek|HTTP 503"
    assert outage.parse_outage_marker(m) == outage.OutageMarker(
        "indexer", "NZBgeek", "HTTP 503"
    )
    assert outage.parse_outage_marker("rate limit") is None
    assert outage.parse_outage_marker(None) is None


def test_record_outage_sets_cooldowns_and_clears():
    url = "https://geek.example/api"
    assert outage.record_outage("indexer", url, "NZBgeek", "HTTP 503", 1) == 600
    assert newznab.is_indexer_in_cooldown(url)
    # existing longer cooldown is kept
    newznab.set_indexer_cooldown(url, 5000)
    assert outage.record_outage("indexer", url, "NZBgeek", "HTTP 503", 2) >= 4999
    assert outage.has_active_outage()
    assert outage.record_outage("downloader", "torbox", "TorBox", "HTTP 502", 3) == 1800
    kinds = {(o.kind, o.provider_name) for o in outage.active_outages()}
    assert kinds == {("indexer", "NZBgeek"), ("downloader", "TorBox")}
    assert any(
        s.reason == "outage" and s.label == "HTTP 502"
        for s in active_rate_limit_sources()
    )

    from app.core.rate_limit_queue import clear_all_rate_limit_cooldowns

    clear_all_rate_limit_cooldowns()
    assert not outage.has_active_outage()
    assert not newznab.is_indexer_in_cooldown(url)
