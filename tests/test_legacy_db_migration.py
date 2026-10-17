"""Tests for the database file rename from NZBoxer to Smewlet."""

import pytest

from app.config import Settings


def test_default_database_url_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import DEFAULT_DATABASE_URL

    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert DEFAULT_DATABASE_URL == "sqlite+aiosqlite:///./config/smewlet.db"
    assert Settings().database_url == DEFAULT_DATABASE_URL
