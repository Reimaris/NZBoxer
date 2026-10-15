"""m65: anime title language columns and idempotent migration."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.database import _migrate_anime_title_columns

TITLE_COLS = {"title_english", "title_romaji", "title_native"}


async def _columns(conn, table: str) -> dict[str, tuple]:
    rows = await conn.execute(text(f'PRAGMA table_info("{table}")'))
    return {r[1]: r for r in rows}


@pytest.mark.anyio
async def test_migration_adds_columns_idempotently():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE system_settings (id INTEGER PRIMARY KEY)")
        )
        await conn.execute(
            text("CREATE TABLE media_items (id INTEGER PRIMARY KEY, title TEXT)")
        )
        await conn.execute(text("CREATE TABLE seasons (id INTEGER PRIMARY KEY)"))
        await conn.execute(text("INSERT INTO system_settings (id) VALUES (1)"))

        await _migrate_anime_title_columns(conn)
        await _migrate_anime_title_columns(conn)

        assert TITLE_COLS <= set(await _columns(conn, "media_items"))
        assert TITLE_COLS <= set(await _columns(conn, "seasons"))
        assert "anime_title_language" in await _columns(conn, "system_settings")
        value = (
            await conn.execute(text("SELECT anime_title_language FROM system_settings"))
        ).scalar_one()
        assert value == "romaji"
    await engine.dispose()
