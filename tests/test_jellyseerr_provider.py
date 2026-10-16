"""m68-01: Jellyseerr/Overseerr provider model, entry contract, redaction."""

import json

import pytest
from sqlalchemy import text

from app.core.experimental import is_experimental
from app.core.log_redaction import install_model_listeners, redact_text
from app.db.database import _migrate_availability_notified
from app.db.models import Provider
from app.services.watchlist_providers import WatchlistEntry

KEY = "test-key-0123456789abcdef"


def _provider(ptype: str, **cfg: object) -> Provider:
    return Provider(
        name=ptype, type=ptype, category="watchlist", config_json=json.dumps(cfg)
    )


def test_watchlist_config_defaults() -> None:
    cfg = _provider("jellyseerr").watchlist_config
    assert cfg["server_url"] == ""
    assert cfg["api_key"] == ""
    assert cfg["sync_admin_watchlist"] is False
    assert cfg["notify_available"] is True


@pytest.mark.parametrize("ptype", ["jellyseerr", "overseerr"])
@pytest.mark.parametrize(
    ("cfg", "connected"),
    [
        ({"server_url": "http://seerr.local:5055", "api_key": KEY}, True),
        ({"server_url": "http://seerr.local:5055", "api_key": "  "}, False),
        ({"server_url": " ", "api_key": KEY}, False),
        ({}, False),
    ],
)
def test_connection_status(ptype: str, cfg: dict, connected: bool) -> None:
    p = _provider(ptype, **cfg)
    assert p.is_authenticated is connected
    assert p.watchlist_connection_status == (
        "connected" if connected else "not_connected"
    )


def test_entry_requested_seasons_default_and_value() -> None:
    base = {"remote_id": "1", "media_kind": "series", "title": "X"}
    assert WatchlistEntry(**base).requested_seasons is None  # type: ignore[arg-type]
    e = WatchlistEntry(**base, requested_seasons=frozenset({1, 2}))  # type: ignore[arg-type]
    assert e.requested_seasons == frozenset({1, 2})


@pytest.mark.parametrize("ptype", ["jellyseerr", "overseerr"])
def test_experimental(ptype: str) -> None:
    assert is_experimental(ptype)


async def test_api_key_in_config_json_redacted_after_save(db_session) -> None:
    install_model_listeners()
    db_session.add(_provider("jellyseerr", server_url="http://x", api_key=KEY))
    await db_session.commit()
    assert KEY not in redact_text(f"key {KEY}")


async def test_availability_notified_migration_idempotent(db_session) -> None:
    conn = await db_session.connection()
    await conn.execute(
        text("ALTER TABLE watchlist_sources DROP COLUMN availability_notified_at")
    )
    for _ in range(2):
        await _migrate_availability_notified(conn)
    cols = {
        r[1] for r in await conn.execute(text('PRAGMA table_info("watchlist_sources")'))
    }
    assert "availability_notified_at" in cols
