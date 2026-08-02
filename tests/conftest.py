"""
Shared Test Fixtures
====================
Provides an in-memory SQLite database and mocked HTTPX clients.
"""
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models import Base

# Override DB URL for tests
settings.database_url = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(settings.database_url, echo=False)
TestingSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)

@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"

@pytest.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

from collections.abc import AsyncGenerator

@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with TestingSessionLocal() as session:
        yield session
