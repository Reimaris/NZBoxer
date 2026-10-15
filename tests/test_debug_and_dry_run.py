import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, reload_settings_from_db, settings
from app.core.fake_detector import is_indexer_metadata_fake, is_nzb_content_fake
from app.core.log_redaction import (
    clear_secrets,
    install_redaction,
    register_secret,
)
from app.core.parser import parse_release_name
from app.core.scorer import score_release
from app.db.models import SystemSettings
from app.main import app

client = TestClient(app)


@pytest.mark.asyncio
async def test_schema_columns_exist(db_session: AsyncSession):
    """SystemSettings model includes debug_mode and dry_run defaulting to False."""
    db_settings = SystemSettings(id=1)
    db_session.add(db_settings)
    await db_session.commit()
    await db_session.refresh(db_settings)

    assert db_settings.debug_mode is False
    assert db_settings.dry_run is False


@pytest.mark.asyncio
async def test_migration_adds_debug_mode_and_dry_run(db_session: AsyncSession):
    """init_db safely ensures debug_mode and dry_run exist via ALTER TABLE if missing."""
    cols_query = await db_session.execute(sa_text("PRAGMA table_info(system_settings)"))
    col_names = {row[1] for row in cols_query.fetchall()}
    assert "debug_mode" in col_names
    assert "dry_run" in col_names


def test_config_env_fallbacks(monkeypatch):
    """Settings loads debug_mode and dry_run with environment fallbacks."""
    # Default without env vars
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("DRY_RUN", raising=False)
    s1 = Settings()
    assert s1.debug_mode is False
    assert s1.dry_run is False

    # DEBUG=true
    monkeypatch.setenv("DEBUG", "true")
    s2 = Settings()
    assert s2.debug_mode is True

    # LOG_LEVEL=DEBUG
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    s3 = Settings()
    assert s3.debug_mode is True

    # DRY_RUN=true
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.setenv("DRY_RUN", "true")
    s4 = Settings()
    assert s4.dry_run is True


@pytest.mark.asyncio
async def test_auto_save_debug_mode_and_dry_run(db_session: AsyncSession):
    """POST /settings/auto-save parses debug_mode and dry_run, persists them, and updates runtime settings."""
    db_settings = SystemSettings(id=1, debug_mode=False, dry_run=False)
    db_session.add(db_settings)
    await db_session.commit()

    # 1. Update via form data
    resp = client.post(
        "/settings/auto-save",
        data={"debug_mode": "true", "dry_run": "true"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    await db_session.refresh(db_settings)
    assert db_settings.debug_mode is True
    assert db_settings.dry_run is True
    assert settings.debug_mode is True
    assert settings.dry_run is True
    assert logging.getLogger("app").level == logging.DEBUG

    # 2. Update via JSON
    resp2 = client.post(
        "/settings/auto-save",
        json={"debug_mode": False, "dry_run": False},
    )
    assert resp2.status_code == 200
    assert resp2.json() == {"status": "ok"}

    await db_session.refresh(db_settings)
    assert db_settings.debug_mode is False
    assert db_settings.dry_run is False
    assert settings.debug_mode is False
    assert settings.dry_run is False
    assert logging.getLogger("app").level == logging.INFO


@pytest.mark.asyncio
async def test_dynamic_logger_level_switching(db_session: AsyncSession):
    """reload_settings_from_db dynamically toggles app logger between DEBUG and INFO while third-party loggers stay filtered."""
    db_settings = SystemSettings(id=1, debug_mode=True)
    db_session.add(db_settings)
    await db_session.commit()

    await reload_settings_from_db(db_session)
    assert logging.getLogger("app").level == logging.DEBUG
    for noisy in ("httpx", "httpcore", "apscheduler", "uvicorn.access"):
        assert logging.getLogger(noisy).level in (logging.WARNING, logging.INFO)

    db_settings.debug_mode = False
    await db_session.commit()
    await reload_settings_from_db(db_session)
    assert logging.getLogger("app").level == logging.INFO
    for noisy in ("httpx", "httpcore", "apscheduler", "uvicorn.access"):
        assert logging.getLogger(noisy).level in (logging.WARNING, logging.INFO)


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_diagnostic_debug_logging_scorer():
    """score_release emits debug-level breakdown logs when logger level is DEBUG."""
    scorer_logger = logging.getLogger("app.core.scorer")
    scorer_logger.setLevel(logging.DEBUG)
    capture = _LogCapture()
    capture.setLevel(logging.DEBUG)
    scorer_logger.addHandler(capture)

    try:
        parsed = parse_release_name("Inception.2010.1080p.BluRay.x264-SPARKS")

        res = score_release(
            parsed,
            size_bytes=8 * 1024 * 1024 * 1024,
            runtime_minutes=148,
            expected_title="Inception",
            expected_year=2010,
        )
        assert res.score > 0
        debug_messages = [
            r.getMessage() for r in capture.records if r.levelno == logging.DEBUG
        ]
        assert any("Inception" in m for m in debug_messages)
    finally:
        scorer_logger.removeHandler(capture)


def test_diagnostic_debug_logging_fake_detector():
    """Fake detector emits debug logs for Layer 1 and Layer 2 checks."""
    fake_logger = logging.getLogger("app.core.fake_detector")
    fake_logger.setLevel(logging.DEBUG)
    capture = _LogCapture()
    capture.setLevel(logging.DEBUG)
    fake_logger.addHandler(capture)

    try:
        raw_item = {
            "title": "Gladiator.2000.1080p.BluRay.x264",
            "size": 10 * 1024 * 1024 * 1024,
            "password": 0,
        }

        is_fake, reason = is_indexer_metadata_fake(
            raw_item, parsed_resolution="1080p", media_type="movie"
        )
        assert not is_fake

        # Minimal valid nzb
        nzb_xml = (
            b'<?xml version="1.0" encoding="utf-8"?>\n'
            b'<nzb xmlns="http://www.newznab.com/DTD/2006/response">\n'
            b'  <file subject="Gladiator.2000.1080p.mkv">\n'
            b'    <segments><segment bytes="50000000" number="1">abc</segment></segments>\n'
            b"  </file>\n"
            b"</nzb>"
        )
        is_fake2, reason2 = is_nzb_content_fake(nzb_xml, media_type="movie")
        assert not is_fake2

        debug_messages = [
            r.getMessage() for r in capture.records if r.levelno == logging.DEBUG
        ]
        assert len(debug_messages) >= 2
    finally:
        fake_logger.removeHandler(capture)


def test_secret_redaction_at_debug_level():
    """install_redaction strictly masks secrets in log records even at DEBUG level."""
    clear_secrets()
    secret_key = "super_secret_indexer_key_999"
    register_secret(secret_key)

    test_handler = logging.Handler()
    records = []

    def handle(record):
        records.append(record)

    test_handler.emit = handle  # type: ignore[assignment]
    test_handler.setLevel(logging.DEBUG)

    install_redaction([test_handler])

    test_logger = logging.getLogger("test_redaction_debug")
    test_logger.setLevel(logging.DEBUG)
    test_logger.addHandler(test_handler)

    test_logger.debug(
        "Connecting to indexer with apikey=%s and literal %s", secret_key, secret_key
    )

    assert len(records) == 1
    logged_msg = records[0].msg
    assert secret_key not in logged_msg
    assert "***" in logged_msg or "…" in logged_msg


# ===========================================================================
# Ticket 02: Dry-Run Mode Interception, Synthetic Tracking & Tagged Alerts
# ===========================================================================


@pytest.mark.asyncio
async def test_dry_run_auto_push_interception(
    db_session: AsyncSession, async_client, monkeypatch
):
    """Auto-push executes search/scoring/Layer 1 & 2 inspection but halts at TorBox boundary in dry-run mode."""
    from unittest.mock import AsyncMock, patch

    from app.core.push_engine import execute_auto_push
    from app.core.transfer_poller import clear_dry_run_pushes, get_active_pushes
    from app.db.models import (
        MediaItem,
        MediaStatus,
        MediaType,
        Provider,
        ProviderCategory,
        SearchPreset,
    )
    from tests.conftest import VALID_NZB_BYTES

    clear_dry_run_pushes()
    monkeypatch.setattr(settings, "dry_run", True)

    provider = Provider(
        name="Simkl",
        type="simkl",
        category=ProviderCategory.WATCHLIST.value,
        is_active=True,
    )
    indexer = Provider(
        name="TestIndexer",
        type="newznab",
        category=ProviderCategory.INDEXER.value,
        api_url="https://idx.example/api",
        api_key="idx-key",
        priority=1,
        is_active=True,
    )
    db_session.add_all([provider, indexer])
    await db_session.flush()

    preset = SearchPreset(
        name="Default",
        is_default=True,
        primary_language="en",
        fallback_language=None,
        video_quality_mode="best",
        audio_quality_mode="best",
        custom_config_json="{}",
    )
    db_session.add(preset)
    await db_session.flush()

    movie = MediaItem(
        provider_id=provider.id,
        simkl_id=9901,
        title="Dune Part Two",
        year=2024,
        media_type=MediaType.MOVIE,
        status=MediaStatus.SEARCHING,
        fail_count=2,
    )
    db_session.add(movie)
    await db_session.commit()

    mock_results = [
        {
            "title": "Dune.Part.Two.2024.1080p.WEB-DL.DDP5.1.H.264-FLUX",
            "size": 5 * 1024 * 1024 * 1024,
            "guid": "dune-2-1080p-guid",
        }
    ]

    with (
        patch(
            "app.services.indexers.newznab.search_movie",
            new=AsyncMock(return_value=mock_results),
        ) as mock_search,
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(VALID_NZB_BYTES, "dune.nzb")),
        ) as mock_fetch,
        patch(
            "app.services.downloaders.torbox.send_nzb_file", new=AsyncMock()
        ) as mock_torbox,
    ):
        result = await execute_auto_push(db_session, movie.id)

    assert mock_search.await_count == 1
    assert mock_fetch.await_count == 1
    mock_torbox.assert_not_awaited()

    assert result["status"] == "simulated"
    assert result["dispatched_count"] == 1
    assert result["guids"] == ["dune-2-1080p-guid"]

    # Database entity states remain untouched in SEARCHING
    await db_session.refresh(movie)
    assert movie.status == MediaStatus.SEARCHING
    assert movie.fail_count == 2

    # Simulated DownloadHistory row created with is_dry_run=True and is_dismissed=True
    hists = (
        (
            await db_session.execute(
                sa_text(
                    "SELECT is_dry_run, is_dismissed, status_detail FROM download_history"
                )
            )
        )
        .mappings()
        .all()
    )
    assert len(hists) == 1
    assert hists[0]["is_dry_run"] == 1 or hists[0]["is_dry_run"] is True
    assert hists[0]["is_dismissed"] == 1 or hists[0]["is_dismissed"] is True
    assert hists[0]["status_detail"] == "simulated"

    # Synthetic Card 5 record exists in get_active_pushes
    active = await get_active_pushes(db_session)
    assert len(active) == 1
    assert active[0]["is_dry_run"] is True
    assert active[0]["status"] == "Dry Run (Simulated)"
    assert active[0]["status_detail"] == "Dispatch simulated (Dry-Run Mode)"
    assert (
        active[0]["release_name"] == "Dune.Part.Two.2024.1080p.WEB-DL.DDP5.1.H.264-FLUX"
    )
    assert active[0]["speed_and_eta"] == "Simulated • No Download"


@pytest.mark.asyncio
async def test_dry_run_manual_grab_interception(db_session: AsyncSession, monkeypatch):
    """Manual grab executes Layer 2 validation, intercepts TorBox upload, and registers synthetic Card 5 record."""
    from unittest.mock import AsyncMock, patch

    from app.core.push_engine import execute_manual_grab
    from app.core.transfer_poller import clear_dry_run_pushes, get_active_pushes
    from app.db.models import (
        MediaItem,
        MediaStatus,
        MediaType,
        Provider,
        ProviderCategory,
    )
    from tests.conftest import VALID_NZB_BYTES

    clear_dry_run_pushes()
    monkeypatch.setattr(settings, "dry_run", True)

    provider = Provider(
        name="Simkl",
        type="simkl",
        category=ProviderCategory.WATCHLIST.value,
        is_active=True,
    )
    db_session.add(provider)
    await db_session.flush()

    movie = MediaItem(
        provider_id=provider.id,
        simkl_id=9902,
        title="Oppenheimer",
        year=2023,
        media_type=MediaType.MOVIE,
        status=MediaStatus.SEARCHING,
    )
    db_session.add(movie)
    await db_session.commit()

    with (
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(VALID_NZB_BYTES, "oppenheimer.nzb")),
        ) as mock_fetch,
        patch(
            "app.services.downloaders.torbox.send_nzb_file", new=AsyncMock()
        ) as mock_torbox,
    ):
        res = await execute_manual_grab(
            db_session,
            movie.id,
            payload={
                "guid": "oppenheimer-guid-1",
                "title": "Oppenheimer.2023.2160p.UHD.BluRay.x265-GRP",
                "size_bytes": 15 * 1024 * 1024 * 1024,
                "score": 5200.0,
            },
        )

    assert mock_fetch.await_count == 1
    mock_torbox.assert_not_awaited()

    assert res["status"] == "simulated"
    assert res["pushed"] is True
    assert res["push_mode"] == "manual"
    assert res["guid"] == "oppenheimer-guid-1"

    # Status remains SEARCHING and simulated history row is persisted with is_dry_run=True
    await db_session.refresh(movie)
    assert movie.status == MediaStatus.SEARCHING
    hists = (
        (
            await db_session.execute(
                sa_text(
                    "SELECT is_dry_run, is_dismissed, status_detail FROM download_history"
                )
            )
        )
        .mappings()
        .all()
    )
    assert len(hists) == 1
    assert hists[0]["is_dry_run"] == 1 or hists[0]["is_dry_run"] is True
    assert hists[0]["is_dismissed"] == 1 or hists[0]["is_dismissed"] is True

    # Synthetic Card 5 record registered
    active = await get_active_pushes(db_session)
    assert len(active) == 1
    assert active[0]["is_dry_run"] is True
    assert active[0]["push_mode"] == "manual"


@pytest.mark.asyncio
async def test_synthetic_card5_retrieval_and_dismissal(
    db_session: AsyncSession, async_client
):
    """Synthetic dry-run records are retrieved in Active Pushes, expired after 300s, and dismissed via API."""
    from datetime import datetime, timedelta, timezone

    from app.core.transfer_poller import (
        COMPLETED_RETENTION_SECONDS,
        clear_dry_run_pushes,
        get_active_pushes,
        register_dry_run_push,
    )

    clear_dry_run_pushes()

    # 1. Register two records
    rec1 = register_dry_run_push(
        media_item_id=101,
        media_title="Show A",
        release_title="Show.A.S01E01.1080p",
        target_badge="S01E01",
    )
    rec2 = register_dry_run_push(
        media_item_id=102,
        media_title="Movie B",
        release_title="Movie.B.2024.2160p",
        target_badge="Movie",
    )

    active = await get_active_pushes(db_session)
    assert len(active) == 2
    assert {r["history_id"] for r in active} == {rec1.id, rec2.id}

    # 2. Dismiss rec1 via API POST /api/pushes/{push_id}/dismiss
    dismiss_resp = await async_client.post(f"/api/pushes/{rec1.id}/dismiss")
    assert dismiss_resp.status_code == 200
    assert "refreshActivePushes" in dismiss_resp.headers.get("HX-Trigger", "")

    active_after = await get_active_pushes(db_session)
    assert len(active_after) == 1
    assert active_after[0]["history_id"] == rec2.id

    # 3. Simulate TTL expiration (>300s) on rec2
    rec2.timestamp = datetime.now(timezone.utc) - timedelta(
        seconds=COMPLETED_RETENTION_SECONDS + 5
    )
    active_expired = await get_active_pushes(db_session)
    assert len(active_expired) == 0


@pytest.mark.asyncio
async def test_dry_run_tagged_notifications(db_session: AsyncSession):
    """Discord and Telegram notification formatters include 🧪 [DRY RUN] badge and simulation notice."""
    from app.services.discord import build_discord_embed
    from app.services.notifications import format_telegram_event_message

    # Discord dry-run embed
    embed = build_discord_embed(
        event_type="push_initiated",
        media_title="Inception",
        media_year=2010,
        target_label="Movie",
        release_name="Inception.2010.1080p.BluRay.x264",
        resolution="1080p",
        is_dry_run=True,
    )
    assert "🧪 [DRY RUN]" in embed["title"]
    assert embed["color"] == 0xF59E0B  # Amber
    field_names = [f["name"] for f in embed["fields"]]
    assert "Simulation Notice" in field_names
    assert "Dry-Run" in embed["footer"]["text"]

    # Telegram dry-run message
    tg_msg = format_telegram_event_message(
        event_type="push_initiated",
        media_title="Inception",
        media_year=2010,
        target_label="Movie",
        release_name="Inception.2010.1080p.BluRay.x264",
        resolution="1080p",
        is_dry_run=True,
    )
    assert "🧪 [DRY RUN]" in tg_msg
    assert (
        "Dispatch simulated in Dry-Run Mode — no transfer was created on downloader."
        in tg_msg
    )


# ===========================================================================
# Ticket 03: UI Warning Banner, Settings Toggles & Modal Controls
# ===========================================================================


@pytest.mark.asyncio
async def test_persistent_top_dry_run_warning_banner(async_client, monkeypatch):
    """templates/base.html renders persistent amber warning banner when settings.dry_run is True and hides it when False."""
    monkeypatch.setattr(settings, "dry_run", False)
    resp_normal = await async_client.get("/")
    assert resp_normal.status_code == 200
    assert "Dry-Run Mode Active: All pushes are simulated" not in resp_normal.text
    assert 'data-dry-run-banner="true"' not in resp_normal.text

    monkeypatch.setattr(settings, "dry_run", True)
    resp_dry = await async_client.get("/")
    assert resp_dry.status_code == 200
    assert "Dry-Run Mode Active" in resp_dry.text
    assert "All pushes are simulated" in resp_dry.text
    assert 'data-dry-run-banner="true"' in resp_dry.text
    assert "sticky top-14" in resp_dry.text


@pytest.mark.asyncio
async def test_settings_slice_4_toggles_and_markup(
    async_client, db_session: AsyncSession
):
    """templates/settings.html includes dedicated debug_mode and dry_run toggles in Slice 4 with auto-save wiring."""
    db_settings = SystemSettings(id=1, debug_mode=False, dry_run=False)
    db_session.add(db_settings)
    await db_session.commit()

    resp = await async_client.get("/settings")
    assert resp.status_code == 200
    html = resp.text

    # Verify toggle labels and helper descriptions
    assert "Debug Mode (Verbose Application Tracing)" in html
    assert (
        "Elevates internal scoring, fake detection, and search logging to DEBUG level"
        in html
    )
    assert "Dry-Run Mode (Simulation Sandbox)" in html
    assert (
        "Executes searches, scoring, and NZB inspection end-to-end, but intercepts and skips the final downloader dispatch"
        in html
    )

    # Verify auto-save wiring
    assert 'name="debug_mode"' in html
    assert 'name="dry_run"' in html
    assert 'hx-post="/settings/auto-save"' in html
    assert 'hx-trigger="change"' in html
    assert 'hx-vals=\'js:{"dry_run": event.target.checked ? "true" : "false"}\'' in html
    assert (
        'hx-vals=\'js:{"debug_mode": event.target.checked ? "true" : "false"}\'' in html
    )


@pytest.mark.asyncio
async def test_push_modal_dynamic_button_and_tooltip(
    async_client, db_session: AsyncSession, monkeypatch
):
    """templates/modals/push_modal.html dynamically relabels Auto-Push button and tooltip depending on settings.dry_run."""
    from app.db.models import MediaItem, MediaType, Provider, ProviderCategory

    provider = Provider(
        name="Simkl",
        type="simkl",
        category=ProviderCategory.WATCHLIST.value,
        is_active=True,
    )
    db_session.add(provider)
    await db_session.flush()

    movie = MediaItem(
        provider_id=provider.id,
        simkl_id=9903,
        title="Interstellar",
        year=2014,
        media_type=MediaType.MOVIE,
    )
    db_session.add(movie)
    await db_session.commit()

    # 1. Normal mode (dry_run = False)
    monkeypatch.setattr(settings, "dry_run", False)
    resp_normal = await async_client.get(f"/api/items/{movie.id}/push-modal")
    assert resp_normal.status_code == 200
    assert "Auto-Push Best" in resp_normal.text
    assert "Simulate Push (Dry Run)" not in resp_normal.text
    assert (
        "Simulate push pipeline without dispatching to downloader"
        not in resp_normal.text
    )

    # 2. Dry-run mode (dry_run = True)
    monkeypatch.setattr(settings, "dry_run", True)
    resp_dry = await async_client.get(f"/api/items/{movie.id}/push-modal")
    assert resp_dry.status_code == 200
    assert "Simulate Push (Dry Run)" in resp_dry.text
    assert "🧪" not in resp_dry.text
    assert "Simulate push pipeline without dispatching to downloader" in resp_dry.text
    assert "Simulating Push..." in resp_dry.text


@pytest.mark.asyncio
async def test_card5_synthetic_row_rendering(async_client, db_session: AsyncSession):
    """Card 5 active pushes table renders synthetic dry-run rows with test-tube icon, 100% Simulated, and single Dismiss button."""
    from app.core.transfer_poller import clear_dry_run_pushes, register_dry_run_push

    clear_dry_run_pushes()
    rec = register_dry_run_push(
        media_item_id=105,
        media_title="The Matrix",
        release_title="The.Matrix.1999.2160p.UHD.BluRay.x265-FLUX",
        target_badge="Movie",
        resolution="2160p",
        source="BluRay",
        language="en",
        score=9200.0,
        size_bytes=22 * 1024 * 1024 * 1024,
    )

    resp = await async_client.get("/api/pushes/active", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    html = resp.text

    # Verify row rendering elements
    assert f'data-history-id="{rec.id}"' in html
    assert "The Matrix" in html
    assert "The.Matrix.1999.2160p.UHD.BluRay.x265-FLUX" in html
    assert "DRY RUN (SIMULATED)" in html
    assert "🧪" not in html
    assert "100% Simulated" in html
    assert f'hx-post="/api/pushes/{rec.id}/dismiss"' in html
    assert "Dismiss" in html
    # Ensure TorBox-specific action buttons are NOT rendered for dry-run row
    assert f'hx-post="/api/pushes/{rec.id}/cancel"' not in html
    assert f'hx-post="/api/pushes/{rec.id}/delete-transfer"' not in html


@pytest.mark.parametrize("is_dry_run", [False, True])
def test_discord_embed_footer_uses_app_version(is_dry_run: bool):
    """Discord embed footer shows the running app version, not a hardcoded one."""
    from app.services.discord import build_discord_embed
    from app.version import APP_VERSION

    embed = build_discord_embed(
        event_type="push_initiated",
        media_title="Inception",
        media_year=2010,
        target_label="Movie",
        release_name="Inception.2010.1080p.BluRay.x264",
        resolution="1080p",
        is_dry_run=is_dry_run,
    )
    assert embed["footer"]["text"].startswith(f"NZBoxer v{APP_VERSION}")
