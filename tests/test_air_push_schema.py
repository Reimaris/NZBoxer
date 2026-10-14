"""Schema, model defaults and legacy migration for m45 (air-date auto-push / upgrades)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import close_db, get_session_factory, init_db
from app.db.models import SystemSettings
from tests.conftest import make_history, make_series

NEW_COLUMNS = {
    "media_items": {"auto_push_episodes"},
    "episodes": {"upgrades_applied", "first_pushed_at", "last_upgrade_checked_at"},
    "download_history": {"is_upgrade", "replaces_history_id"},
    "system_settings": {
        "air_delay_hours",
        "upgrade_window_hours",
        "max_upgrade_checks",
    },
}


async def _columns(session: AsyncSession, table: str) -> dict[str, tuple]:
    res = await session.execute(text(f"PRAGMA table_info({table});"))
    return {row[1]: tuple(row) for row in res.fetchall()}


@pytest.mark.anyio
async def test_fresh_row_defaults(db_session: AsyncSession) -> None:
    item, _, episodes = await make_series(db_session, episodes_per_season=1)
    hist = await make_history(db_session, item=item)
    settings = SystemSettings(id=1)
    db_session.add(settings)
    await db_session.flush()
    await db_session.refresh(item)
    await db_session.refresh(episodes[0])
    await db_session.refresh(hist)
    await db_session.refresh(settings)

    assert item.auto_push_episodes is False
    assert episodes[0].upgrades_applied == 0
    assert episodes[0].first_pushed_at is None
    assert hist.replaces_history_id is None
    assert episodes[0].last_upgrade_checked_at is None
    assert hist.is_upgrade is False
    assert settings.air_delay_hours == 2
    assert settings.upgrade_window_hours == 48
    assert settings.max_upgrade_checks == 3


@pytest.mark.anyio
async def test_legacy_schema_migration_adds_columns_idempotently(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "legacy_m45.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE media_items (id INTEGER PRIMARY KEY, provider_id INTEGER, simkl_id INTEGER, title VARCHAR(500));
        CREATE TABLE episodes (id INTEGER PRIMARY KEY, season_id INTEGER, episode_number INTEGER);
        CREATE TABLE download_history (id INTEGER PRIMARY KEY, nzb_title VARCHAR(1000) NOT NULL);
        CREATE TABLE system_settings (id INTEGER PRIMARY KEY, sh_max_retries INTEGER);
        INSERT INTO media_items (id) VALUES (1);
        INSERT INTO episodes (id) VALUES (1);
        INSERT INTO download_history (id, nzb_title) VALUES (1, 'x');
        INSERT INTO system_settings (id) VALUES (1);
        """
    )
    conn.commit()
    conn.close()
    db_url = f"sqlite+aiosqlite:///{db_file}"

    async def check() -> None:
        async with get_session_factory()() as session:
            for table, names in NEW_COLUMNS.items():
                cols = await _columns(session, table)
                assert names <= cols.keys(), table
            expected = {
                ("media_items", "auto_push_episodes"): 0,
                ("episodes", "upgrades_applied"): 0,
                ("download_history", "is_upgrade"): 0,
                ("system_settings", "air_delay_hours"): 2,
                ("system_settings", "upgrade_window_hours"): 48,
                ("system_settings", "max_upgrade_checks"): 3,
            }
            for (table, col), val in expected.items():
                row = (
                    await session.execute(text(f"SELECT {col} FROM {table};"))
                ).first()
                assert row is not None and row[0] == val, (table, col)
            row = (
                await session.execute(
                    text("SELECT last_upgrade_checked_at FROM episodes;")
                )
            ).first()
            assert row is not None and row[0] is None

    await close_db()
    await init_db(db_url)
    await check()
    await close_db()
    await init_db(db_url)  # idempotent second run
    await check()
    await close_db()


@pytest.mark.anyio
async def test_legacy_upgrade_checks_renamed_and_first_pushed_backfilled(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "legacy_m45_checks.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE media_items (id INTEGER PRIMARY KEY, provider_id INTEGER, simkl_id INTEGER, title VARCHAR(500));
        CREATE TABLE episodes (id INTEGER PRIMARY KEY, season_id INTEGER, episode_number INTEGER,
            upgrade_checks INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE download_history (id INTEGER PRIMARY KEY, nzb_title VARCHAR(1000) NOT NULL,
            episode_id INTEGER, torbox_sent_at DATETIME, is_upgrade BOOLEAN NOT NULL DEFAULT 0);
        CREATE TABLE system_settings (id INTEGER PRIMARY KEY, sh_max_retries INTEGER);
        INSERT INTO episodes (id, upgrade_checks) VALUES (1, 2), (2, 1);
        INSERT INTO download_history (id, nzb_title, episode_id, torbox_sent_at, is_upgrade) VALUES
            (1, 'late', 1, '2026-10-13 00:00:00', 0),
            (2, 'early', 1, '2026-10-12 00:00:00', 0),
            (3, 'upgrade', 1, '2026-10-11 00:00:00', 1);
        INSERT INTO system_settings (id) VALUES (1);
        """
    )
    conn.commit()
    conn.close()
    db_url = f"sqlite+aiosqlite:///{db_file}"

    async def check() -> None:
        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, upgrades_applied, first_pushed_at FROM episodes ORDER BY id;"
                    )
                )
            ).fetchall()
            assert [r[1] for r in rows] == [0, 0]
            assert str(rows[0][2]).startswith("2026-10-12 00:00:00")
            assert rows[1][2] is None
            cols = await _columns(session, "episodes")
            assert "upgrade_checks" not in cols

    await close_db()
    await init_db(db_url)
    await check()
    await close_db()
    await init_db(db_url)  # idempotent: no re-backfill / reset
    await check()
    await close_db()
