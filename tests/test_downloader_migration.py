"""DownloadHistory downloader-column migration, synonyms and Provider config (m57-02)."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.database import close_db, init_db
from app.db.models import DownloadHistory, Provider
from tests.conftest import make_downloader_provider, make_history

_LEGACY_DDL = """
CREATE TABLE download_history (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    media_item_id INTEGER REFERENCES media_items(id) ON DELETE CASCADE,
    nzb_title VARCHAR(1000) NOT NULL,
    torbox_hash VARCHAR(200),
    torbox_id VARCHAR(200),
    torbox_sent_at DATETIME,
    is_ad_hoc BOOLEAN NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


async def _make_legacy_db(tmp_path, rebuild: bool) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
    await init_db(url)
    await close_db()
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        if rebuild:  # CASCADE FK forces the table-rebuild path
            await conn.execute(text("DROP TABLE download_history"))
            await conn.execute(text(_LEGACY_DDL))
        else:  # current schema, rolled back to the pre-m57 column names
            await conn.execute(
                text("DROP INDEX ix_download_history_downloader_transfer_id")
            )
            await conn.execute(
                text("ALTER TABLE download_history DROP COLUMN downloader_type")
            )
            for new, old in (
                ("downloader_transfer_id", "torbox_id"),
                ("dispatched_at", "torbox_sent_at"),
            ):
                await conn.execute(
                    text(f"ALTER TABLE download_history RENAME COLUMN {new} TO {old}")
                )
        await conn.execute(
            text(
                "INSERT INTO download_history (nzb_title, torbox_id, torbox_sent_at, is_ad_hoc) "
                "VALUES ('Legacy.Release', '123', '2024-05-01 10:00:00', 1)"
            )
        )
    await engine.dispose()
    return url


async def _read_rows(url: str):
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        rows = (
            (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.downloader_transfer_id == "123"
                    )
                )
            )
            .scalars()
            .all()
        )
        cols = {
            r[1]
            for r in (
                await session.execute(text("PRAGMA table_info(download_history)"))
            ).fetchall()
        }
        idx = {
            r[1]
            for r in (
                await session.execute(text("PRAGMA index_list(download_history)"))
            ).fetchall()
        }
    await engine.dispose()
    return rows, cols, idx


@pytest.mark.parametrize("rebuild", [False, True], ids=["alter", "rebuild"])
async def test_legacy_rows_backfilled_and_idempotent(tmp_path, rebuild):
    url = await _make_legacy_db(tmp_path, rebuild)
    for _ in range(2):  # second run must be a no-op
        await init_db(url)
        await close_db()
        rows, cols, idx = await _read_rows(url)
        assert len(rows) == 1
        row = rows[0]
        assert row.downloader_transfer_id == "123"
        assert row.downloader_type == "torbox"
        assert row.dispatched_at is not None
        assert row.is_ad_hoc is True
        assert {"downloader_type", "downloader_transfer_id", "dispatched_at"} <= cols
        assert "ix_download_history_downloader_transfer_id" in idx


async def test_downloader_columns_roundtrip(db_session):
    hist = await make_history(db_session, downloader_transfer_id="tb-1")
    assert hist.downloader_transfer_id == "tb-1"
    assert hist.downloader_type == "torbox"
    hist.downloader_transfer_id = "tb-2"
    await db_session.flush()
    found = (
        await db_session.execute(
            select(DownloadHistory).where(
                DownloadHistory.downloader_transfer_id == "tb-2"
            )
        )
    ).scalar_one()
    assert found.id == hist.id
    other = await make_history(
        db_session, downloader_type="sabnzbd", downloader_transfer_id="nzo_1"
    )
    assert other.downloader_transfer_id == "nzo_1"


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, ("movies", "tv", "anime")),
        (
            {"category_movie": "m", "category_series": "s", "category_anime": "a"},
            ("m", "s", "a"),
        ),
        ({"category_movie": "", "category_series": 7}, ("", "tv", "anime")),
        ("not json", ("movies", "tv", "anime")),
        ("[1]", ("movies", "tv", "anime")),
    ],
)
async def test_sabnzbd_config(db_session, config, expected):
    p: Provider = await make_downloader_provider(
        db_session, "sabnzbd", config_json=config
    )
    cfg = p.sabnzbd_config
    assert (
        cfg["category_movie"],
        cfg["category_series"],
        cfg["category_anime"],
    ) == expected
