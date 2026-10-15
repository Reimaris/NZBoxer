import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_retire_backup_export_endpoint_returns_404(async_client: AsyncClient):
    """GET /settings/export must be retired and return 404 Not Found."""
    resp = await async_client.get("/settings/export")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_settings_template_no_backup_export_references(async_client: AsyncClient):
    """templates/settings.html and /settings page must not contain Backup Export references."""
    resp = await async_client.get("/settings")
    assert resp.status_code == 200
    html = resp.text
    assert "Backup Export" not in html
    assert "/settings/export" not in html

    # Also check the raw template file directly
    with open("templates/settings.html", "r", encoding="utf-8") as f:
        template_content = f.read()
    assert "Backup Export" not in template_content
    assert "/settings/export" not in template_content


@pytest.mark.asyncio
async def test_system_check_modal_endpoint(async_client: AsyncClient):
    """GET /system/check/modal renders the modernized modal HTML with heartbeat icon, results container, and re-run button."""
    resp = await async_client.get("/system/check/modal")
    assert resp.status_code == 200
    html = resp.text
    assert "System Diagnostics" in html
    assert "Health" in html
    assert "system-check-results" in html
    assert 'hx-get="/system/check/run"' in html
    assert 'hx-trigger="load"' in html
    assert "Re-run Diagnostics" in html
    assert 'hx-target="#system-check-results"' in html


@pytest.mark.asyncio
async def test_settings_slice_4_system_diagnostics_card_and_button(
    async_client: AsyncClient,
):
    """Slice 4 in templates/settings.html presents 'System Diagnostics & Health' with 'Run Health Check' button."""
    resp = await async_client.get("/settings")
    assert resp.status_code == 200
    html = resp.text
    assert "System Diagnostics & Health" in html
    assert "Run Health Check" in html
    assert 'hx-get="/system/check/modal"' in html
    assert 'hx-target="#modal-container"' in html
    assert "Administrative Actions" not in html


@pytest.mark.asyncio
async def test_system_check_run_configured_only_filtering_and_vital_role_warnings(
    async_client: AsyncClient,
    db_session,
    monkeypatch,
):
    """System check runs core probes, omits unconfigured services, and warns if no indexers or downloaders are active."""
    import httpx

    orig_send = httpx.AsyncClient.send

    async def mock_send(self, request: httpx.Request, *args, **kwargs):
        url_str = str(request.url)
        if url_str.startswith("http://test"):
            return await orig_send(self, request, *args, **kwargs)
        if "graphql.anilist.co" in url_str:
            return httpx.Response(
                200, json={"data": {"Page": {"media": [{"id": 1}]}}}, request=request
            )
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", mock_send)

    resp = await async_client.get("/system/check/run")
    assert resp.status_code == 200
    html = resp.text

    # Core probes unconditional
    assert "SQLite Database" in html or "Database" in html
    assert "Persistent Storage" in html or "Storage" in html
    assert "AniList API" in html or "AniList" in html
    assert "Core Infrastructure" in html
    assert "Metadata Provider" not in html

    # Unconfigured integrations must NOT appear
    assert "TheMovieDB" not in html
    assert "Telegram" not in html
    assert "Discord" not in html

    # Vital role warning banners should be present since 0 indexers and 0 downloaders configured
    assert "No Usenet Downloader" in html
    assert "No Usenet Indexer" in html
    # Ensure no red failure crosses for unconfigured services
    assert "API key missing" not in html
    assert "No configured provider" not in html
    assert "No configured bot" not in html


@pytest.mark.asyncio
async def test_system_check_run_with_active_providers_and_indexers(
    async_client: AsyncClient,
    db_session,
    monkeypatch,
):
    """System check probes configured active indexers and downloaders, tracking ms latency and removing warnings."""
    import httpx

    from app.db.models import Provider

    # Seed TorBox downloader
    tb = Provider(
        category="downloader",
        type="torbox",
        name="TorBox Cloud",
        api_key="tb_test_token",
        api_url="https://api.torbox.app/v1/api",
        is_active=True,
    )
    # Seed 2 active indexers
    idx1 = Provider(
        category="indexer",
        type="newznab",
        name="Treasure Maps Indexer",
        api_key="tm_key",
        api_url="https://treasure-maps.com/api",
        is_active=True,
    )
    idx2 = Provider(
        category="indexer",
        type="newznab",
        name="Geek Indexer",
        api_key="geek_key",
        api_url="https://api.nzbgeek.info/api",
        is_active=True,
    )
    # Seed TMDB
    tmdb = Provider(
        category="metadata",
        type="tmdb",
        name="TheMovieDB",
        api_key="tmdb_test_key",
        is_active=True,
    )
    db_session.add_all([tb, idx1, idx2, tmdb])
    await db_session.commit()

    orig_send = httpx.AsyncClient.send

    async def mock_send(self, request: httpx.Request, *args, **kwargs):
        url_str = str(request.url)
        if url_str.startswith("http://test"):
            return await orig_send(self, request, *args, **kwargs)
        if "torbox" in url_str:
            return httpx.Response(
                200,
                json={"success": True, "data": {"email": "user@test.com"}},
                request=request,
            )
        if "treasure-maps" in url_str or "nzbgeek" in url_str:
            return httpx.Response(
                200,
                text="<caps><server version='1.0'/></caps>",
                headers={"content-type": "application/xml"},
                request=request,
            )
        if "themoviedb.org" in url_str:
            return httpx.Response(200, json={"images": {}}, request=request)
        if "graphql.anilist.co" in url_str:
            return httpx.Response(
                200, json={"data": {"Page": {"media": [{"id": 1}]}}}, request=request
            )
        return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", mock_send)

    resp = await async_client.get("/system/check/run")
    assert resp.status_code == 200
    html = resp.text

    # Both indexers must be reported
    assert "Treasure Maps Indexer" in html
    assert "Geek Indexer" in html
    assert "TorBox Cloud" in html
    assert "TheMovieDB" in html

    # Latency badge present with color classes (emerald, amber, or rose)
    assert "ms" in html
    assert any(
        cls in html for cls in ("text-emerald-300", "text-amber-300", "text-rose-300")
    )

    # Vital role warnings must NOT be present
    assert "No Usenet Downloader" not in html
    assert "No Usenet Indexer" not in html


@pytest.mark.asyncio
async def test_system_check_notifications_discord_and_telegram(
    async_client: AsyncClient,
    db_session,
    monkeypatch,
):
    """System check probes enabled Discord webhook (GET) and Telegram bot (getMe)."""
    import httpx

    from app.db.models import NotificationChannel, SystemSettings

    # Set up system settings with Discord enabled
    stmt = await db_session.get(SystemSettings, 1)
    if not stmt:
        stmt = SystemSettings(
            id=1,
            discord_enabled=True,
            discord_webhook_url="https://discord.com/api/webhooks/123/abc",
        )
        db_session.add(stmt)
    else:
        stmt.discord_enabled = True
        stmt.discord_webhook_url = "https://discord.com/api/webhooks/123/abc"

    # Add Telegram channel
    tg = NotificationChannel(
        type="telegram",
        name="My Telegram Bot",
        bot_token="123456:ABC-DEF",
        chat_id="99999",
    )
    db_session.add(tg)
    await db_session.commit()

    orig_send = httpx.AsyncClient.send

    async def mock_send(self, request: httpx.Request, *args, **kwargs):
        url_str = str(request.url)
        if url_str.startswith("http://test"):
            return await orig_send(self, request, *args, **kwargs)
        if "discord.com" in url_str:
            return httpx.Response(
                200, json={"name": "Alerts Hook", "guild_id": "111"}, request=request
            )
        if "api.telegram.org" in url_str:
            return httpx.Response(
                200,
                json={"ok": True, "result": {"username": "NZBoxerBot"}},
                request=request,
            )
        if "graphql.anilist.co" in url_str:
            return httpx.Response(
                200, json={"data": {"Page": {"media": [{"id": 1}]}}}, request=request
            )
        return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", mock_send)

    resp = await async_client.get("/system/check/run")
    assert resp.status_code == 200
    html = resp.text

    assert "Discord Webhook" in html or "Discord" in html
    assert "Telegram Bot" in html or "Telegram" in html


@pytest.mark.asyncio
async def test_system_check_probes_connected_watchlist_providers(
    async_client: AsyncClient, db_session, monkeypatch
):
    """Each connected non-Simkl watchlist type renders '<name> (Watchlist)'; not_connected is skipped."""
    from app.db.models import Provider
    from app.services import anilist
    from app.services.watchlists import mal, plex

    async def al(username, access_token=None):
        return True, f"User: {username}"

    async def px(auth_token, client_identifier):
        return False, "Reconnect required"

    async def ml(access_token):
        return True, "User: mal-bob"

    monkeypatch.setattr(anilist, "check_user", al)
    monkeypatch.setattr(plex, "check_account", px)
    monkeypatch.setattr(mal, "check_account", ml)

    db_session.add_all(
        [
            Provider(
                category="watchlist",
                type="anilist",
                name="AniList",
                username="alice",
                is_active=True,
            ),
            Provider(
                category="watchlist",
                type="plex",
                name="Plex",
                access_token="ptok",
                is_active=True,
            ),
            Provider(
                category="watchlist",
                type="mal",
                name="MyAnimeList",
                client_id="c",
                access_token="mtok",
                is_active=True,
            ),
        ]
    )
    await db_session.commit()
    html = (await async_client.get("/system/check/run")).text
    assert "AniList (Watchlist)" in html and "User: alice" in html
    assert "Plex (Watchlist)" in html and "Reconnect required" in html
    assert "MyAnimeList (Watchlist)" in html and "User: mal-bob" in html


@pytest.mark.asyncio
async def test_system_check_skips_not_connected_watchlist(
    async_client: AsyncClient, db_session, monkeypatch
):
    from app.db.models import Provider
    from app.services.watchlists import plex

    called = []

    async def px(auth_token, client_identifier):
        called.append(1)
        return True, "x"

    monkeypatch.setattr(plex, "check_account", px)
    db_session.add(
        Provider(category="watchlist", type="plex", name="Plex", is_active=True)
    )
    await db_session.commit()
    html = (await async_client.get("/system/check/run")).text
    assert "Plex (Watchlist)" not in html and not called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ptype", "name", "fields", "msg"),
    [
        (
            "sabnzbd",
            "SABnzbd",
            {"api_url": "http://sab:8080/sabnzbd/api", "api_key": "k"},
            "SABnzbd 4.3.1",
        ),
        ("premiumize", "Premiumize", {"api_key": "k"}, "Premiumize account OK"),
    ],
)
async def test_system_check_probes_active_non_torbox_downloader(
    async_client: AsyncClient, db_session, monkeypatch, ptype, name, fields, msg
):
    """SAB/Premiumize are tested via test_connection; TorBox is never called."""
    import httpx

    from app.db.models import Provider
    from app.services.downloaders import premiumize, sabnzbd

    async def ok(self):
        return True, msg

    monkeypatch.setattr(sabnzbd.SabnzbdDownloader, "test_connection", ok)
    monkeypatch.setattr(premiumize.PremiumizeDownloader, "test_connection", ok)

    real_send = httpx.AsyncClient.send

    async def no_http(self, request, **kwargs):
        if request.url.host != "test":  # only the ASGI test client may send
            raise AssertionError(f"unexpected HTTP call: {request.url}")
        return await real_send(self, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    db_session.add(
        Provider(category="downloader", type=ptype, name=name, is_active=True, **fields)
    )
    await db_session.commit()
    html = (await async_client.get("/system/check/run")).text
    assert f"{name} (Downloader)" in html and msg in html
    assert "No Usenet Downloader" not in html


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ptype", "fields", "msg"),
    [
        ("sabnzbd", {"api_key": "k"}, "Host missing"),
        ("sabnzbd", {"api_url": "http://sab:8080/sabnzbd/api"}, "API Key missing"),
        ("premiumize", {}, "API Key missing"),
    ],
)
async def test_system_check_downloader_missing_credentials_no_network(
    async_client: AsyncClient, db_session, monkeypatch, ptype, fields, msg
):
    import httpx

    from app.db.models import Provider

    real_send = httpx.AsyncClient.send

    async def no_http(self, request, **kwargs):
        if request.url.host != "test":  # only the ASGI test client may send
            raise AssertionError(f"unexpected HTTP call: {request.url}")
        return await real_send(self, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    db_session.add(
        Provider(category="downloader", type=ptype, name="DL", is_active=True, **fields)
    )
    await db_session.commit()
    html = (await async_client.get("/system/check/run")).text
    assert "DL (Downloader)" in html and msg in html


async def test_system_check_after_onboarding_nzbgeek(
    async_client: AsyncClient, monkeypatch
):
    """Onboarding with indexer_type=nzbgeek yields an NZBGeek probe and no indexer banner."""
    import httpx

    resp = await async_client.post(
        "/api/onboarding/step1",
        data={"indexer_type": "nzbgeek", "indexer_api_key": "geek-key"},
    )
    assert resp.status_code == 200

    orig_send = httpx.AsyncClient.send

    async def mock_send(self, request: httpx.Request, *args, **kwargs):
        if str(request.url).startswith("http://test"):
            return await orig_send(self, request, *args, **kwargs)
        return httpx.Response(
            200,
            text="<caps><server version='1.0'/></caps>",
            headers={"content-type": "application/xml"},
            request=request,
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", mock_send)
    html = (await async_client.get("/system/check/run")).text
    assert "NZBgeek" in html or "NZBGeek" in html
    assert "Usenet Indexer" in html
    assert "No Usenet Indexer" not in html


@pytest.mark.parametrize(
    ("ptype", "label"),
    [
        ("simkl", "Simkl"),
        ("torbox", "TorBox"),
        ("sabnzbd", "SABnzbd"),
        ("newznab", "Newznab"),
    ],
)
def test_provider_type_labels_registry_driven(ptype, label):
    from app.core.diagnostics import PROVIDER_TYPE_LABELS

    assert PROVIDER_TYPE_LABELS[ptype] == label
