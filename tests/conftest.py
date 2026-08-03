"""
Shared Test Fixtures
====================
Provides an in-memory SQLite database and mocked HTTPX clients.
"""
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models import Base

from sqlalchemy.pool import StaticPool

from sqlalchemy.pool import StaticPool
from app.db.database import init_db, get_engine, get_session_factory, close_db

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

from collections.abc import AsyncGenerator

@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        yield session
