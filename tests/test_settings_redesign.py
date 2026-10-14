import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import reload_settings_from_db, settings
from app.db.models import SystemSettings
from app.main import app

client = TestClient(app)


@pytest.mark.asyncio
async def test_auto_save_boolean_toggle(db_session: AsyncSession):
    """Assert toggling a single boolean updates SQLite directly and returns HX-Trigger header."""
    db_settings = SystemSettings(id=1, discord_enabled=False, notify_on_errors=True)
    db_session.add(db_settings)
    await db_session.commit()

    # Toggle discord_enabled=true
    resp = client.post(
        "/settings/auto-save",
        data={"discord_enabled": "true"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Trigger") == '{"showAutoSaveToast": "Saved"}'
    assert resp.json() == {"status": "ok"}

    await db_session.refresh(db_settings)
    assert db_settings.discord_enabled is True
    # Other field remains untouched
    assert db_settings.notify_on_errors is True

    # Toggle notify_on_errors=false
    resp2 = client.post(
        "/settings/auto-save",
        data={"notify_on_errors": "false"},
    )
    assert resp2.status_code == 200
    assert resp2.headers.get("HX-Trigger") == '{"showAutoSaveToast": "Saved"}'

    await db_session.refresh(db_settings)
    assert db_settings.notify_on_errors is False
    assert db_settings.discord_enabled is True


@pytest.mark.asyncio
async def test_auto_save_integers_and_string(db_session: AsyncSession):
    """Assert updating download_timeout_minutes, sh_max_retries, and discord_webhook_url."""
    db_settings = SystemSettings(id=1, download_timeout_minutes=60, sh_max_retries=3)
    db_session.add(db_settings)
    await db_session.commit()

    resp = client.post(
        "/settings/auto-save",
        data={
            "download_timeout_minutes": "120",
            "sh_max_retries": "5",
            "discord_webhook_url": "https://discord.com/api/webhooks/test/123",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Trigger") == '{"showAutoSaveToast": "Saved"}'

    await db_session.refresh(db_settings)
    assert db_settings.download_timeout_minutes == 120
    assert db_settings.sh_max_retries == 5
    assert (
        db_settings.discord_webhook_url == "https://discord.com/api/webhooks/test/123"
    )

    await reload_settings_from_db(db_session)
    assert settings.download_timeout_minutes == 120


@pytest.mark.asyncio
async def test_auto_save_bounds_checking(db_session: AsyncSession):
    """Verify invalid timeout bounds fall back gracefully to default (60) without crashing."""
    db_settings = SystemSettings(id=1, download_timeout_minutes=120, sh_max_retries=2)
    db_session.add(db_settings)
    await db_session.commit()

    # Out of bounds (> 240 or not in allowed set)
    resp = client.post(
        "/settings/auto-save",
        data={"download_timeout_minutes": "200"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Trigger") == '{"showAutoSaveToast": "Saved"}'

    await db_session.refresh(db_settings)
    assert db_settings.download_timeout_minutes == 60

    # Out of bounds (< 15)
    resp = client.post(
        "/settings/auto-save",
        data={"download_timeout_minutes": "0"},
    )
    assert resp.status_code == 200
    await db_session.refresh(db_settings)
    assert db_settings.download_timeout_minutes == 60

    # Negative sh_max_retries is clamped or falls back gracefully
    resp = client.post(
        "/settings/auto-save",
        data={"sh_max_retries": "-5"},
    )
    assert resp.status_code == 200
    await db_session.refresh(db_settings)
    # val >= 0: if negative, falls back or clamped to 0 / previous / default
    assert db_settings.sh_max_retries >= 0


@pytest.mark.asyncio
async def test_legacy_endpoint_aliases(db_session: AsyncSession):
    """Verify /settings/global and /settings/system aliases also route to the handler."""
    db_settings = SystemSettings(id=1)
    db_session.add(db_settings)
    await db_session.commit()

    resp = client.post(
        "/settings/global",
        data={"download_timeout_minutes": "30"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Trigger") == '{"showAutoSaveToast": "Saved"}'

    await db_session.refresh(db_settings)
    assert db_settings.download_timeout_minutes == 30


@pytest.mark.asyncio
async def test_preset_modal_endpoints(db_session: AsyncSession):
    """Verify GET /settings/preset/new/modal and edit modal return expected HTML."""
    from app.db.models import SearchPreset

    # 1. New modal endpoint
    resp_new = client.get("/settings/preset/new/modal")
    assert resp_new.status_code == 200
    assert "New Search Preset" in resp_new.text
    assert 'name="name"' in resp_new.text
    assert 'name="primary_language"' in resp_new.text

    # 2. Seed a preset
    preset = SearchPreset(
        name="Sci-Fi Special 4K",
        is_default=False,
        primary_language="de",
        fallback_language="en",
        video_quality_mode="custom",
        audio_quality_mode="best",
        allow_season_packs=True,
        prefer_season_packs=True,
        custom_config_json='{"resolutions": ["2160p"], "min_size_gb": 10.5}',
    )
    db_session.add(preset)
    await db_session.commit()
    await db_session.refresh(preset)

    # 3. Edit modal endpoint
    resp_edit = client.get(f"/settings/preset/{preset.id}/edit/modal")
    assert resp_edit.status_code == 200
    assert "Edit Search Preset" in resp_edit.text
    assert "Sci-Fi Special 4K" in resp_edit.text
    assert 'hx-put="/api/presets/' in resp_edit.text

    # 4. Non-existent preset returns 404
    resp_404 = client.get("/settings/preset/999999/edit/modal")
    assert resp_404.status_code == 404


@pytest.mark.asyncio
async def test_preset_htmx_partial_refresh(db_session: AsyncSession):
    """Verify creating, updating, setting default, and deleting presets return presets_list.html on HTMX request."""
    from app.db.models import SearchPreset

    preset = SearchPreset(
        name="Initial Test Preset",
        is_default=True,
        primary_language="en",
    )
    db_session.add(preset)
    await db_session.commit()
    await db_session.refresh(preset)

    # 1. Update with HX-Request: true returns partial HTML and closePresetModal trigger
    resp_update = client.put(
        f"/api/presets/{preset.id}",
        data={"name": "Updated Test Preset", "primary_language": "de"},
        headers={"HX-Request": "true"},
    )
    assert resp_update.status_code == 200
    assert 'id="presets-list"' in resp_update.text
    assert "Updated Test Preset" in resp_update.text
    assert "closePresetModal" in resp_update.headers.get("HX-Trigger", "")

    # 2. Create second preset with HX-Request: true
    resp_create = client.post(
        "/api/presets",
        data={"name": "Second Preset", "primary_language": "fr"},
        headers={"HX-Request": "true"},
    )
    assert resp_create.status_code == 201
    assert 'id="presets-list"' in resp_create.text
    assert "Second Preset" in resp_create.text
    assert "closePresetModal" in resp_create.headers.get("HX-Trigger", "")

    # 3. Set default with HX-Request: true
    created_id = None
    presets = (await db_session.execute(SearchPreset.__table__.select())).fetchall()
    for row in presets:
        if row.name == "Second Preset":
            created_id = row.id
            break
    assert created_id is not None

    resp_default = client.post(
        f"/api/presets/{created_id}/default",
        headers={"HX-Request": "true"},
    )
    assert resp_default.status_code == 200
    assert 'id="presets-list"' in resp_default.text
    assert "Default" in resp_default.text

    # 4. Delete with HX-Request: true
    resp_del = client.delete(
        f"/api/presets/{preset.id}",
        headers={"HX-Request": "true"},
    )
    assert resp_del.status_code == 200
    assert 'id="presets-list"' in resp_del.text
    assert "Updated Test Preset" not in resp_del.text

    # 5. Non-HTMX call still returns JSON
    resp_json = client.get("/api/presets")
    assert resp_json.status_code == 200
    assert isinstance(resp_json.json(), list)


@pytest.mark.asyncio
async def test_unified_provider_flow_grid_sorting_and_markup(db_session: AsyncSession):
    """Verify provider sorting (Watchlist -> Metadata -> Downloader -> Indexers by priority)

    and unified responsive flow grid markup with category color-coded borders and pills.
    """
    from app.db.models import Provider

    # Create providers in arbitrary order
    idx2 = Provider(
        category="indexer",
        type="newznab",
        name="Indexer Beta",
        api_url="https://indexer2.local",
        priority=2,
        is_active=True,
    )
    downloader = Provider(
        category="downloader",
        type="torbox",
        name="TorBox Client",
        api_key="tb_test_key",
        api_url="https://api.torbox.app/v1/api",
        is_active=True,
    )
    meta = Provider(
        category="metadata",
        type="tmdb",
        name="The Movie DB",
        api_key="tmdb_key",
        is_active=True,
    )
    idx1 = Provider(
        category="indexer",
        type="newznab",
        name="Indexer Alpha",
        api_url="https://indexer1.local",
        priority=1,
        is_active=True,
    )
    watchlist = Provider(
        category="watchlist",
        type="simkl",
        name="Simkl Sync",
        username="testuser",
        access_token="tok123",
        config_json='{"sync_interval_minutes": 30, "refresh_token": "ref123"}',
        is_active=True,
    )
    idx_omega = Provider(
        category="indexer",
        type="newznab",
        name="Indexer Omega",
        api_url="https://indexer3.local",
        priority=3,
        is_active=False,
    )

    db_session.add_all([idx2, downloader, meta, idx1, watchlist, idx_omega])
    await db_session.commit()

    resp = client.get("/settings")
    assert resp.status_code == 200
    html = resp.text

    # 1. Responsive flow grid classes
    assert "grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5" in html

    # 2. Add Provider toolbar button
    assert 'hx-get="/settings/provider/new"' in html
    assert "Add Provider" in html

    # 3. Category color-coded borders
    assert "border-[#d40060]/60 hover:border-[#d40060]" in html
    assert "border-amber-500/50 hover:border-amber-400" in html
    assert "border-purple-500/50 hover:border-purple-400" in html
    assert "border-blue-500/50 hover:border-blue-400" in html

    # 4. Internal Category Pills
    assert "bg-[#d40060]/15 text-[#d40060] border border-[#d40060]/30" in html
    assert "bg-amber-500/15 text-amber-400 border border-amber-500/30" in html
    assert "bg-purple-500/15 text-purple-400 border border-purple-500/30" in html
    assert "bg-blue-500/15 text-blue-400 border border-blue-500/30" in html

    # Watchlist Avatar Icon matches category pink theme
    assert "bg-sky-500" not in html

    # 5. Order of cards in rendered HTML:
    # Simkl Sync (Watchlist) -> The Movie DB (Metadata) -> TorBox Client (Downloader) -> Indexer Alpha (Pri 1) -> Indexer Beta (Pri 2) -> Indexer Omega (Pri 3)
    pos_wl = html.find("Simkl Sync")
    pos_meta = html.find("The Movie DB")
    pos_dl = html.find("TorBox Client")
    pos_idx1 = html.find("Indexer Alpha")
    pos_idx2 = html.find("Indexer Beta")
    pos_idx_omega = html.find("Indexer Omega")

    assert (
        pos_wl != -1
        and pos_meta != -1
        and pos_dl != -1
        and pos_idx1 != -1
        and pos_idx2 != -1
        and pos_idx_omega != -1
    )
    assert pos_wl < pos_meta < pos_dl < pos_idx1 < pos_idx2 < pos_idx_omega


@pytest.mark.asyncio
async def test_unified_provider_flow_grid_empty_state(db_session: AsyncSession):
    """Verify empty-state placeholder when no providers are configured."""
    resp = client.get("/settings")
    assert resp.status_code == 200
    html = resp.text

    assert "No providers configured. Click 'Add Provider' above to get started." in html


@pytest.mark.asyncio
async def test_single_list_6_slice_expandable_layout_and_controls(
    db_session: AsyncSession,
):
    """Verify single-list 6-slice layout, initial collapsed states, relocation of

    backup/system check, and absence of legacy tabs and save buttons.
    """
    from app.db.models import (
        BlacklistedRelease,
        NotificationChannel,
        Provider,
        SearchPreset,
    )

    # Seed one of each domain entity for rich summary badge verification
    provider = Provider(
        category="watchlist",
        type="simkl",
        name="Simkl Sync",
        username="testuser",
        is_active=True,
    )
    # Update seeded default preset to have custom name for rich summary badge verification
    from sqlalchemy import select

    existing_preset = (
        (
            await db_session.execute(
                select(SearchPreset).where(SearchPreset.is_default == True)
            )
        )
        .scalars()
        .first()
    )
    if existing_preset:
        existing_preset.name = "Ultra 4K"
    else:
        existing_preset = SearchPreset(
            name="Ultra 4K", is_default=True, primary_language="en"
        )
        db_session.add(existing_preset)
    notif = NotificationChannel(
        type="telegram",
        name="Ops Alerts",
        chat_id="12345",
    )
    bl = BlacklistedRelease(
        nzb_title="Spam.Release.1080p",
        reason="Bad archive",
    )
    db_settings = SystemSettings(
        id=1,
        download_timeout_hours=12,
        sh_max_retries=4,
        discord_enabled=True,
        discord_webhook_url="https://discord.com/api/webhooks/test",
    )
    db_session.add_all([provider, notif, bl, db_settings])
    await db_session.commit()

    resp = client.get("/settings")
    assert resp.status_code == 200
    html = resp.text

    # 1. Verify Alpine state initializes all 6 slices to false (all collapsed)
    assert "providers: false" in html
    assert "presets: false" in html
    assert "notifications: false" in html
    assert "system: false" in html
    assert "blacklist: false" in html
    assert "scoring: false" in html
    assert "toggleSlice(key)" in html

    # 2. Verify all 6 slice headers exist with chevrons
    assert "Connected Providers" in html
    assert "Search Presets" in html
    assert "Notifications" in html
    assert "System &amp; Automation" in html or "System & Automation" in html
    assert "Blacklisted Releases" in html
    assert "Video Scoring Reference" in html
    assert ":class=\"openSlices.providers ? 'rotate-180' : ''\"" in html
    assert ":class=\"openSlices.presets ? 'rotate-180' : ''\"" in html
    assert ":class=\"openSlices.notifications ? 'rotate-180' : ''\"" in html
    assert ":class=\"openSlices.system ? 'rotate-180' : ''\"" in html
    assert ":class=\"openSlices.blacklist ? 'rotate-180' : ''\"" in html
    assert ":class=\"openSlices.scoring ? 'rotate-180' : ''\"" in html

    # 3. Verify rich summary badges in headers
    assert "1 Active (1 Total)" in html
    assert "1 Presets (Default: Ultra 4K)" in html
    assert "Telegram: 1 • Discord: On" in html
    assert "Timeout: 720m • Retries: 4" in html
    assert "1 Blacklisted" in html
    assert "Quality-Density Engine (v3.0)" in html

    # 4. Verify absence of legacy tabs, tab navigation logic, and Save General Settings button
    assert "validTabs" not in html
    assert "activeTab" not in html
    assert "setTab(" not in html
    assert "nzboxer_settings_tab" not in html
    assert "Save General Settings" not in html
    assert 'id="global-settings-form"' not in html

    # 5. Verify Backup Export is retired and System Check is inside Slice 4 (System & Automation)
    pos_slice4 = html.find("System &amp; Automation")
    if pos_slice4 == -1:
        pos_slice4 = html.find("System & Automation")
    pos_slice5 = html.find("Blacklisted Releases")
    pos_syscheck = html.find('hx-get="/system/check/modal"')

    assert pos_slice4 != -1 and pos_slice5 != -1
    assert 'href="/settings/export"' not in html
    assert "Backup Export" not in html
    assert pos_syscheck != -1
    assert pos_slice4 < pos_syscheck < pos_slice5

    # 6. Verify live auto-save wiring on notification and system form controls
    assert 'hx-post="/settings/auto-save"' in html
    assert 'name="discord_enabled"' in html
    assert 'name="discord_webhook_url"' in html
    assert 'name="notify_on_push_initiated"' in html
    assert 'name="notify_on_completed"' in html
    assert 'name="notify_on_failure"' in html
    assert 'name="notify_on_auto_advance"' in html
    assert 'name="notify_on_errors"' in html
    assert 'name="download_timeout_minutes"' in html
    assert 'name="sh_max_retries"' in html

    # 7. Verify transient micro-toast indicator exists with show-auto-save-toast listener
    assert "@show-auto-save-toast.window=" in html
    assert "✓ Saved" in html
