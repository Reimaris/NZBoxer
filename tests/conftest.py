"""
Shared Test Fixtures
====================
Provides an in-memory SQLite database and mocked HTTPX clients.
"""
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.database import close_db, get_engine, get_session_factory, init_db
from app.db.models import Base

# Override DB URL for tests
settings.database_url = "sqlite+aiosqlite:///file:testdb?mode=memory&cache=shared"

@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"

@pytest.fixture(autouse=True)
async def setup_db():
    await init_db(settings.database_url)
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await close_db()


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        yield session
