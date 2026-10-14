import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SystemSettings
from app.main import app

client = TestClient(app)


@pytest.mark.asyncio
async def test_get_settings_page(db_session: AsyncSession):
    """Test that the settings page renders successfully with categorized provider sections."""
    from app.db.models import Provider, ProviderCategory

    # Create dummy settings and providers
    db_settings = SystemSettings(
        id=1, scoring_settings={"scoring": {"resolution": {"1080p": 50}}}
    )
    p_simkl = Provider(
        name="My Simkl",
        type="simkl",
        category=ProviderCategory.WATCHLIST.value,
        is_active=True,
    )
    p_tb = Provider(
        name="TorBox Client",
        type="torbox",
        category=ProviderCategory.DOWNLOADER.value,
        is_active=True,
    )
    p_tm = Provider(
        name="TM Indexer",
        type="newznab",
        category=ProviderCategory.INDEXER.value,
        priority=1,
        is_active=True,
    )
    db_session.add_all([db_settings, p_simkl, p_tb, p_tm])
    await db_session.commit()

    response = client.get("/settings")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Settings" in response.text
    # Verify Slice 1 Connected Providers and unified grid render
    assert "Connected Providers" in response.text
    assert "Search Presets" in response.text
    assert "Print Media Providers" not in response.text
    assert "My Simkl" in response.text
    assert "TorBox Client" in response.text
    assert "TM Indexer" in response.text


@pytest.mark.asyncio
async def test_post_global_settings_valid(db_session: AsyncSession):
    """Test saving valid v3 global settings."""
    db_settings = SystemSettings(id=1)
    db_session.add(db_settings)
    await db_session.commit()

    response = client.post(
        "/settings/global",
        data={
            "sh_max_retries": "5",
            "download_timeout_minutes": "120",
        },
    )

    assert response.status_code == 200
    assert "Settings saved successfully" in response.text

    # Verify DB was updated
    await db_session.refresh(db_settings)
    assert db_settings.sh_max_retries == 5
    assert db_settings.download_timeout_minutes == 120


@pytest.mark.asyncio
async def test_system_check_run(db_session: AsyncSession, monkeypatch):
    """Test system check endpoint returns provider connectivity HTML."""
    from app.db.models import Provider, ProviderCategory

    p_tmdb = Provider(
        name="TMDB Provider",
        type="tmdb",
        category="metadata",
        api_key="dummy_tmdb_key",
        is_active=True,
    )
    p_tm = Provider(
        name="TreasureMaps",
        type="newznab",
        category=ProviderCategory.INDEXER.value,
        api_key="dummy_tm_key",
        api_url="https://treasuremaps.net/api",
        is_active=True,
    )
    p_tb = Provider(
        name="TorBox",
        type="torbox",
        category=ProviderCategory.DOWNLOADER.value,
        api_key="dummy_tb_key",
        is_active=True,
    )
    db_session.add_all([p_tmdb, p_tm, p_tb])
    await db_session.commit()

    import httpx

    orig_send = httpx.AsyncClient.send

    async def mock_send(self, request: httpx.Request, *args, **kwargs):
        url_str = str(request.url)
        if url_str.startswith("http://testserver"):
            return await orig_send(self, request, *args, **kwargs)
        if "themoviedb.org" in url_str:
            return httpx.Response(200, json={"images": {}}, request=request)
        if "treasuremaps.net" in url_str:
            return httpx.Response(
                200,
                text="<caps><server/></caps>",
                headers={"content-type": "application/xml"},
                request=request,
            )
        if "torbox" in url_str:
            return httpx.Response(
                200,
                json={"success": True, "data": {"email": "tb@test.com"}},
                request=request,
            )
        if "graphql.anilist.co" in url_str:
            return httpx.Response(
                200, json={"data": {"Page": {"media": [{"id": 1}]}}}, request=request
            )
        return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", mock_send)

    response = client.get("/system/check/run")
    assert response.status_code == 200
    assert "TMDB Provider" in response.text
    assert "TreasureMaps" in response.text
    assert "TorBox" in response.text
    assert "ms" in response.text


@pytest.mark.asyncio
async def test_download_timeout_and_notification_setting_persistence(
    db_session: AsyncSession,
):
    """Test saving and retrieving download_timeout_minutes and Discord/notification settings."""
    from app.config import reload_settings_from_db, settings

    db_settings = SystemSettings(id=1, download_timeout_minutes=60)
    db_session.add(db_settings)
    await db_session.commit()

    response = client.post(
        "/settings/global",
        data={
            "download_timeout_minutes": "30",
            "discord_webhook_url": "https://discord.com/api/webhooks/123/abc",
            "discord_enabled": "true",
            "notify_on_push_initiated": "true",
        },
    )
    assert response.status_code == 200

    await db_session.refresh(db_settings)
    assert db_settings.download_timeout_minutes == 30
    assert db_settings.discord_enabled is True
    assert db_settings.notify_on_push_initiated is True

    await reload_settings_from_db(db_session)
    assert settings.download_timeout_minutes == 30


@pytest.mark.asyncio
async def test_download_timeout_minutes_auto_save_and_clamping(
    db_session: AsyncSession,
):
    """Test auto-save endpoint with download_timeout_minutes values and fallbacks."""

    db_settings = SystemSettings(id=1, download_timeout_minutes=60)
    db_session.add(db_settings)
    await db_session.commit()

    # Valid values: 15, 30, 60, 120, 240
    for valid_val in (15, 30, 60, 120, 240):
        resp = client.post(
            "/settings/auto-save", json={"download_timeout_minutes": valid_val}
        )
        assert resp.status_code == 200
        await db_session.refresh(db_settings)
        assert db_settings.download_timeout_minutes == valid_val

    # Invalid / unallowed values fallback to 60
    for invalid_val in (0, 45, 100, 300, 999, "invalid"):
        resp = client.post(
            "/settings/auto-save", json={"download_timeout_minutes": invalid_val}
        )
        assert resp.status_code == 200
        await db_session.refresh(db_settings)
        assert db_settings.download_timeout_minutes == 60


def test_settings_env_var_timeout_fallbacks(monkeypatch):
    """Test Settings env var parsing for minutes and fallback to hours * 60."""
    from app.config import Settings

    # Default without env vars
    monkeypatch.delenv("DOWNLOAD_TIMEOUT_MINUTES", raising=False)
    monkeypatch.delenv("DOWNLOAD_TIMEOUT_HOURS", raising=False)
    s = Settings()
    assert s.download_timeout_minutes == 60

    # Explicit minutes
    monkeypatch.setenv("DOWNLOAD_TIMEOUT_MINUTES", "120")
    monkeypatch.delenv("DOWNLOAD_TIMEOUT_HOURS", raising=False)
    s = Settings()
    assert s.download_timeout_minutes == 120

    # Fallback to hours * 60
    monkeypatch.delenv("DOWNLOAD_TIMEOUT_MINUTES", raising=False)
    monkeypatch.setenv("DOWNLOAD_TIMEOUT_HOURS", "3")
    s = Settings()
    assert s.download_timeout_minutes == 180


@pytest.mark.asyncio
async def test_database_migration_from_legacy_hours(tmp_path):
    """Test SQLite migration converting legacy download_timeout_hours to minutes."""
    import aiosqlite

    from app.db.database import close_db, init_db

    db_file = tmp_path / "legacy.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    # Setup legacy schema with download_timeout_hours
    async with aiosqlite.connect(str(db_file)) as conn:
        await conn.execute(
            "CREATE TABLE system_settings (id INTEGER PRIMARY KEY, download_timeout_hours INTEGER);"
        )
        # Legacy > 4h (e.g. 24h) should migrate to 60m
        await conn.execute(
            "INSERT INTO system_settings (id, download_timeout_hours) VALUES (1, 24);"
        )
        await conn.commit()

    # Run init_db on the legacy database
    await init_db(db_url)
    try:
        async with aiosqlite.connect(str(db_file)) as conn:
            cursor = await conn.execute(
                "SELECT download_timeout_minutes FROM system_settings WHERE id = 1;"
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 60
    finally:
        await close_db()

    # Test small legacy value <= 4h (e.g. 2h -> 120m)
    db_file_small = tmp_path / "legacy_small.db"
    db_url_small = f"sqlite+aiosqlite:///{db_file_small}"
    async with aiosqlite.connect(str(db_file_small)) as conn:
        await conn.execute(
            "CREATE TABLE system_settings (id INTEGER PRIMARY KEY, download_timeout_hours INTEGER);"
        )
        await conn.execute(
            "INSERT INTO system_settings (id, download_timeout_hours) VALUES (1, 2);"
        )
        await conn.commit()

    await init_db(db_url_small)
    try:
        async with aiosqlite.connect(str(db_file_small)) as conn:
            cursor = await conn.execute(
                "SELECT download_timeout_minutes FROM system_settings WHERE id = 1;"
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == 120
    finally:
        await close_db()


@pytest.mark.asyncio
async def test_telegram_log_demoted(monkeypatch):
    """Test that telegram notification success is logged at DEBUG, not INFO."""
    import logging

    import httpx

    from app.services import telegram

    class MockResponse:
        def __init__(self):
            self.status_code = 200

        def raise_for_status(self):
            pass

    async def mock_post(*args, **kwargs):
        return MockResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    records = []

    class MemoryHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = MemoryHandler()
    tel_logger = logging.getLogger("app.services.telegram")
    prev_level = tel_logger.level
    tel_logger.setLevel(logging.DEBUG)
    tel_logger.addHandler(handler)
    try:
        success = await telegram.send_notification(
            "Test msg", "dummy_token", "dummy_chat_id"
        )
        assert success is True
        info_records = [
            r
            for r in records
            if r.levelno == logging.INFO
            and "Telegram notification sent successfully" in r.getMessage()
        ]
        debug_records = [
            r
            for r in records
            if r.levelno == logging.DEBUG
            and "Telegram notification sent successfully" in r.getMessage()
        ]
        assert len(info_records) == 0
        assert len(debug_records) == 1
    finally:
        tel_logger.removeHandler(handler)
        tel_logger.setLevel(prev_level)


@pytest.mark.parametrize(
    ("ptype", "badge"),
    [("dognzb", "DN"), ("treasuremaps", "TM"), ("mystery_idx", "NZB")],
)
async def test_connected_providers_indexer_badge(
    async_client, db_session, ptype, badge
):
    from app.db.models import Provider

    db_session.add(
        Provider(type=ptype, category="indexer", name="Idx", priority=2, is_active=True)
    )
    await db_session.commit()
    html = (await async_client.get("/settings")).text
    assert f'data-badge="{badge}"' in html
