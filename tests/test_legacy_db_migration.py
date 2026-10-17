"""Tests for the database file rename from NZBoxer to Smewlet."""

import logging
import sqlite3
from pathlib import Path

import pytest

from app.config import Settings
from app.db.database import migrate_legacy_database


def test_default_database_url_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import DEFAULT_DATABASE_URL

    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert DEFAULT_DATABASE_URL == "sqlite+aiosqlite:///./config/smewlet.db"
    assert Settings().database_url == DEFAULT_DATABASE_URL


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    return d


def _make_db(path: Path, note: str, wal: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE probe (id INTEGER, note TEXT)")
    conn.execute("INSERT INTO probe VALUES (1, ?)", (note,))
    conn.commit()
    return conn


def _note(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("SELECT note FROM probe").fetchone()[0])
    finally:
        conn.close()


def test_copies_legacy_db_including_wal_rows(cfg: Path) -> None:
    legacy_conn = _make_db(cfg / "nzboxer.db", "legacy-row", wal=True)
    try:
        assert migrate_legacy_database(cfg / "smewlet.db") is True
        assert _note(cfg / "smewlet.db") == "legacy-row"
        assert (cfg / "nzboxer.db").exists()
        assert not (cfg / "smewlet.db.migrating").exists()
    finally:
        legacy_conn.close()


def test_existing_target_is_not_overwritten(cfg: Path) -> None:
    _make_db(cfg / "nzboxer.db", "legacy-row").close()
    _make_db(cfg / "smewlet.db", "target-row").close()
    assert migrate_legacy_database(cfg / "smewlet.db") is False
    assert _note(cfg / "smewlet.db") == "target-row"


def test_fresh_install_does_nothing(cfg: Path) -> None:
    assert migrate_legacy_database(cfg / "smewlet.db") is False
    assert not (cfg / "smewlet.db").exists()


def test_non_default_name_is_ignored(cfg: Path) -> None:
    _make_db(cfg / "nzboxer.db", "legacy-row").close()
    assert migrate_legacy_database(cfg / "custom.db") is False
    assert not (cfg / "custom.db").exists()


def test_warning_logged(cfg: Path, caplog: pytest.LogCaptureFixture) -> None:
    _make_db(cfg / "nzboxer.db", "legacy-row").close()
    with caplog.at_level(logging.WARNING):
        migrate_legacy_database(cfg / "smewlet.db")
    record = next(r for r in caplog.records if "Migrated legacy database" in r.message)
    assert record.levelno == logging.WARNING
    assert (
        "Migrated legacy database config/nzboxer.db to config/smewlet.db"
        in record.message
    )


def test_failure_raises_and_leaves_no_temp_file(
    cfg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_db(cfg / "nzboxer.db", "legacy-row").close()

    def boom(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sqlite3, "connect", boom)
    with pytest.raises(RuntimeError, match="disk I/O error"):
        migrate_legacy_database(cfg / "smewlet.db")
    assert not (cfg / "smewlet.db").exists()
    assert not (cfg / "smewlet.db.migrating").exists()
