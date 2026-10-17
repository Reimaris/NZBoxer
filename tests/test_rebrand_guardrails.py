"""Guardrails for the Smewlet rebrand."""

from app.config import DEFAULT_USER_AGENT
from app.version import APP_NAME, APP_VERSION


def test_brand_constants() -> None:
    assert APP_NAME == "Smewlet"
    assert DEFAULT_USER_AGENT == f"Smewlet/{APP_VERSION} (Linux; x64)"
