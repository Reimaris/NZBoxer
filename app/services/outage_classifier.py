"""
Service Outage Classifier (ADR-158)
===================================
Pure, dependency-free mapping of HTTP statuses, transport exceptions, Newznab
error envelopes and HTML intercept pages to an ``OutageVerdict``. ``outage``
verdicts are transient (queue and retry); ``auth`` verdicts fail immediately.
HTTP 429 is intentionally unclassified and stays on the rate-limit path.
"""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass
from typing import Literal

import httpx

TRANSIENT_HTTP_STATUSES: frozenset[int] = frozenset(
    {500, 502, 503, 504, *range(520, 531)}
)
AUTH_HTTP_STATUSES: frozenset[int] = frozenset({401, 403})
NEWZNAB_TRANSIENT_CODES: frozenset[int] = frozenset({500, 501})
NEWZNAB_AUTH_CODES: frozenset[int] = frozenset({100, 101, 102})


@dataclass(frozen=True)
class OutageVerdict:
    kind: Literal["outage", "auth"]
    label: str
    http_status: int | None = None


def classify_http_status(status: int) -> OutageVerdict | None:
    """Outage for transient 5xx/52x, auth for 401/403, else None (incl. 429)."""
    if status in TRANSIENT_HTTP_STATUSES:
        return OutageVerdict("outage", f"HTTP {status}", status)
    if status in AUTH_HTTP_STATUSES:
        return OutageVerdict("auth", f"HTTP {status}", status)
    return None


def _has_ssl_cause(exc: BaseException | None) -> bool:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


def classify_exception(exc: BaseException) -> OutageVerdict | None:
    """Map a transport/parse exception to an outage verdict, else None."""
    if isinstance(exc, httpx.HTTPStatusError):
        return classify_http_status(exc.response.status_code)
    if _has_ssl_cause(exc):
        return OutageVerdict("outage", "SSLCertVerificationError")
    if isinstance(
        exc,
        (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
        ),
    ):
        return OutageVerdict("outage", type(exc).__name__)
    if isinstance(exc, (httpx.ConnectError, ConnectionRefusedError)):
        return OutageVerdict("outage", "ConnectError")
    if isinstance(exc, httpx.TooManyRedirects):
        return OutageVerdict("outage", "TooManyRedirects")
    if isinstance(exc, json.JSONDecodeError):
        return OutageVerdict("outage", "JSONDecodeError")
    return None


def classify_newznab_error(payload: object) -> OutageVerdict | None:
    """Classify a decoded Newznab JSON error envelope (``error`` or ``@attributes``)."""
    if not isinstance(payload, dict):
        return None
    container = payload.get("error")
    if not isinstance(container, dict):
        container = payload
    attrs = container.get("@attributes")
    if not isinstance(attrs, dict):
        return None
    try:
        code = int(str(attrs.get("code")).strip())
    except ValueError:
        return None
    if code in NEWZNAB_AUTH_CODES:
        return OutageVerdict("auth", f"Newznab {code}")
    if code in NEWZNAB_TRANSIENT_CODES:
        return OutageVerdict("outage", f"Newznab {code}")
    return None


def classify_non_json_body(content_type: str, body_snippet: str) -> OutageVerdict:
    """A non-JSON 200 body (maintenance/parked page) is always a transient outage."""
    return OutageVerdict("outage", "Non-JSON response")
