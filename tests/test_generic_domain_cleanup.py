"""m59 generic provider/domain cleanup: migration helpers and gates."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.database import _backup_db_before_m59, _rename_or_merge_column


@pytest.fixture
async def mig_engine(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    yield engine
    await engine.dispose()


async def _run(engine, table, old, new, **kw):
    async with engine.begin() as conn:
        await _rename_or_merge_column(conn, table, old, new, **kw)


async def _info(engine, table):
    async with engine.connect() as conn:
        return {r[1] for r in await conn.execute(text(f"PRAGMA table_info({table})"))}


async def test_rename_only_keeps_data_and_is_idempotent(mig_engine):
    async with mig_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t (id INTEGER, old_c TEXT)"))
        await conn.execute(text("INSERT INTO t VALUES (1, 'x')"))
    await _run(mig_engine, "t", "old_c", "new_c")
    await _run(mig_engine, "t", "old_c", "new_c")
    assert await _info(mig_engine, "t") == {"id", "new_c"}
    async with mig_engine.connect() as conn:
        assert (await conn.execute(text("SELECT new_c FROM t"))).scalar() == "x"


async def test_merge_and_drop_with_index_on_old(mig_engine):
    async with mig_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t (id INTEGER, old_c TEXT, new_c TEXT)"))
        await conn.execute(text("CREATE INDEX ix_old ON t (old_c)"))
        await conn.execute(
            text("INSERT INTO t VALUES (1,'a',NULL),(2,'b','keep'),(3,NULL,NULL)")
        )
    await _run(mig_engine, "t", "old_c", "new_c")
    assert await _info(mig_engine, "t") == {"id", "new_c"}
    async with mig_engine.connect() as conn:
        rows = (await conn.execute(text("SELECT new_c FROM t ORDER BY id"))).all()
    assert [r[0] for r in rows] == ["a", "keep", None]


async def test_merge_sentinel_default_for_not_null(mig_engine):
    async with mig_engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE t (id INTEGER, old_c TEXT, "
                "new_c TEXT NOT NULL DEFAULT 'torbox')"
            )
        )
        await conn.execute(text("INSERT INTO t (id, old_c) VALUES (1, 'sab')"))
    await _run(mig_engine, "t", "old_c", "new_c", sentinel_default="torbox")
    async with mig_engine.connect() as conn:
        assert (await conn.execute(text("SELECT new_c FROM t"))).scalar() == "sab"


async def test_both_absent_is_noop(mig_engine):
    async with mig_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t (id INTEGER)"))
    await _run(mig_engine, "t", "old_c", "new_c")
    assert await _info(mig_engine, "t") == {"id"}


def test_backup_created_once_and_not_overwritten(tmp_path: Path):
    db = tmp_path / "nzboxer.db"
    db.write_bytes(b"v1")
    _backup_db_before_m59(db)
    bak = tmp_path / "nzboxer.db.pre-m59.bak"
    assert bak.read_bytes() == b"v1"
    db.write_bytes(b"v2")
    _backup_db_before_m59(db)
    assert bak.read_bytes() == b"v1"


def test_backup_skips_missing_and_memory(tmp_path: Path):
    _backup_db_before_m59(tmp_path / "missing.db")
    _backup_db_before_m59(Path(":memory:"))
    assert list(tmp_path.iterdir()) == []


# --- m59-02: download_history downloader columns -----------------------------

_LEGACY = {"torbox_id", "torbox_hash", "torbox_sent_at"}


def _make_dh(extra_cols: str) -> str:
    return (
        "CREATE TABLE download_history (id INTEGER PRIMARY KEY, nzb_title TEXT, "
        "torbox_hash VARCHAR(200), torbox_id VARCHAR(255), torbox_sent_at DATETIME"
        f"{extra_cols})"
    )


@pytest.mark.parametrize(
    "extra_cols",
    ["", ", downloader_transfer_id VARCHAR(255), dispatched_at DATETIME"],
    ids=["legacy", "m57-shaped"],
)
async def test_downloader_columns_migrate(mig_engine, extra_cols):
    from app.db.database import _migrate_downloader_columns

    async with mig_engine.begin() as conn:
        await conn.execute(text(_make_dh(extra_cols)))
        await conn.execute(
            text(
                "CREATE INDEX ix_download_history_torbox_hash ON download_history (torbox_hash)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO download_history (id, nzb_title, torbox_hash, torbox_id, "
                "torbox_sent_at) VALUES (1, 'a', 'abc', '123', '2026-01-01 00:00:00')"
            )
        )
    for _ in range(2):  # second run is a no-op
        async with mig_engine.begin() as conn:
            await _migrate_downloader_columns(conn)
    cols = await _info(mig_engine, "download_history")
    assert not cols & _LEGACY
    assert {"downloader_hash", "downloader_transfer_id", "dispatched_at"} <= cols
    async with mig_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT downloader_hash, downloader_transfer_id, dispatched_at "
                    "FROM download_history"
                )
            )
        ).one()
    assert row[0] == "abc" and row[1] == "123" and str(row[2]).startswith("2026-01-01")


async def test_fresh_create_all_has_no_legacy_downloader_columns(db_session):
    rows = (await db_session.execute(text("PRAGMA table_info(download_history)"))).all()
    cols = {r[1] for r in rows}
    assert not cols & _LEGACY
    assert "downloader_hash" in cols


async def test_download_history_has_no_torbox_attributes(db_session):
    from app.db.models import DownloadHistory
    from tests.conftest import make_history

    h = await make_history(db_session, downloader_hash="h1")
    assert h.downloader_hash == "h1"
    bare = DownloadHistory(nzb_title="x", nzb_guid="g")
    db_session.add(bare)
    await db_session.flush()
    assert bare.downloader_type == "torbox"
    for legacy in _LEGACY:
        assert not hasattr(DownloadHistory, legacy)


# --- m59-07: media_items watchlist-neutral columns ---------------------------

_MEDIA_LEGACY = {
    "last_watched_order_simkl",
    "simkl_watched_completed",
    "associated_simkl_ids_json",
    "simkl_synced_at",
}


async def test_media_items_neutral_columns_migrate(mig_engine):
    from app.db.database import _migrate_watchlist_neutral_columns

    async with mig_engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE media_items (id INTEGER PRIMARY KEY, "
                "last_watched_order_simkl INTEGER NOT NULL DEFAULT 0, "
                "simkl_watched_completed BOOLEAN NOT NULL DEFAULT 0, "
                "associated_simkl_ids_json TEXT, simkl_synced_at DATETIME)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO media_items VALUES "
                "(1, 7, 1, '[11,12]', '2026-01-01T00:00:00+00:00'), "
                "(2, 0, 0, NULL, NULL)"
            )
        )
    for _ in range(2):  # second run is a no-op
        async with mig_engine.begin() as conn:
            await _migrate_watchlist_neutral_columns(conn)
    cols = await _info(mig_engine, "media_items")
    assert not cols & _MEDIA_LEGACY
    async with mig_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT last_watched_order, watched_completed, "
                    "associated_franchise_ids_json, watchlist_synced_at "
                    "FROM media_items ORDER BY id"
                )
            )
        ).all()
        info = {
            r[1]: r for r in await conn.execute(text("PRAGMA table_info(media_items)"))
        }
    assert rows[0][:3] == (7, 1, "[11,12]") and str(rows[0][3]).startswith("2026-01-01")
    assert rows[1][2] == "[]" and rows[1][3] is None
    assert info["associated_franchise_ids_json"][3] == 1
    assert info["associated_franchise_ids_json"][4] == "'[]'"


@pytest.mark.parametrize(
    "raw,expected",
    [(None, set()), ("", set()), ("not json", set()), ("[3, 0, null, 5]", {3, 5})],
)
async def test_associated_franchise_ids_tolerant(db_session, raw, expected):
    from tests.conftest import make_movie

    item = await make_movie(db_session, simkl_id=0)
    item.associated_franchise_ids_json = raw
    assert item.associated_franchise_ids == expected


async def test_add_associated_franchise_id_and_fresh_schema(db_session):
    from tests.conftest import make_movie

    item = await make_movie(db_session, simkl_id=9)
    for bad in (None, 0, -1):
        item.add_associated_franchise_id(bad)
    item.add_associated_franchise_id(4)
    assert item.associated_franchise_ids == {4, 9}
    assert item.associated_franchise_ids_json == "[4, 9]"
    rows = (await db_session.execute(text("PRAGMA table_info(media_items)"))).all()
    assert not {r[1] for r in rows} & _MEDIA_LEGACY


# --- m59-12: indexer aliases removed, provider type -> newznab ---------------


def test_legacy_indexer_modules_removed():
    from app.services.indexers import newznab  # noqa: F401

    for old in ("app.services.treasure_maps", "app.services.indexer_catalog"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("treasure_maps", "newznab"),
        (None, "newznab"),
        ("", "newznab"),
        ("nzbgeek", "nzbgeek"),
        ("newznab", "newznab"),
    ],
)
def test_normalize_indexer_type(raw, expected):
    from app.services.indexers import normalize_indexer_type

    assert normalize_indexer_type(raw) == expected


async def test_migrate_indexer_type_idempotent_and_raw_row_still_indexer(db_session):
    from app.db.database import _migrate_indexer_type
    from app.db.models import Provider
    from app.services import provider_service

    db_session.add(
        Provider(
            type="treasure_maps",
            category="indexer",
            name="Old",
            api_url="https://x/api",
            api_key="k",
            priority=1,
            is_active=True,
        )
    )
    await db_session.commit()
    # Raw legacy row is still searched before migration.
    assert [p.name for p in await provider_service.get_active_indexers(db_session)] == [
        "Old"
    ]
    conn = await db_session.connection()
    await _migrate_indexer_type(conn)
    await _migrate_indexer_type(conn)
    types = (
        (await db_session.execute(text("SELECT type FROM providers"))).scalars().all()
    )
    assert list(types) == ["newznab"]


# --- m59-13/14: watchlists package, legacy modules removed ---------------------------


def test_watchlist_registry_and_legacy_modules_removed():
    from app.services import anilist
    from app.services.watchlists import WATCHLIST_DISPLAY_NAMES, WATCHLIST_TYPES

    assert WATCHLIST_DISPLAY_NAMES["mal"] == "MyAnimeList"
    assert set(WATCHLIST_DISPLAY_NAMES) == WATCHLIST_TYPES
    assert "Serves two roles" in (anilist.__doc__ or "")
    for name in ("simkl", "plex", "mal"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"app.services.{name}")


# --- m59-15/16: torbox client lives only in the adapter module ----------------


def test_torbox_legacy_module_removed_and_adapter_holds_client():
    from app.services.downloaders import torbox as adapter

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.services.torbox")
    assert adapter.DEFAULT_API_URL == "https://api.torbox.app/v1/api"
    assert hasattr(adapter, "TorBoxDownloader") and hasattr(adapter, "TorBoxError")


# --- m59-18: delete-transfer routes keep delete-torbox aliases ---------------


@pytest.mark.parametrize("family", ["pushes", "history", "items"])
async def test_delete_transfer_routes_and_legacy_alias_match(async_client, family):
    new = await async_client.post(f"/api/{family}/987654/delete-transfer")
    legacy = await async_client.post(f"/api/{family}/987654/delete-torbox")
    assert new.status_code != 405
    assert (legacy.status_code, legacy.text) == (new.status_code, new.text)


# --- m59-19: row keys and source-named rate-limit badges --------------------


async def test_ledger_row_keys_are_downloader_neutral(db_session):
    from app.core.transfer_poller import get_push_history_ledger
    from tests.conftest import make_history

    hist = await make_history(
        db_session,
        status_detail="completed",
        downloader_type="sabnzbd",
        downloader_transfer_id="abc",
    )
    await db_session.commit()
    row = next(
        r for r in await get_push_history_ledger(db_session) if r["id"] == hist.id
    )
    assert row["downloader_status"] == "READY"
    assert row["downloader_name"] == "SABnzbd"
    assert row["can_delete_transfer"] is True
    assert not {"torbox_status", "can_delete_torbox", "torbox_id"} & row.keys()


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        ([("downloader", "SABnzbd")], "SABnzbd"),
        ([("indexer", "NZBgeek")], "NZBgeek"),
        ([("downloader", "SABnzbd"), ("indexer", "NZBgeek")], "SABnzbd + NZBgeek"),
    ],
)
async def test_active_pushes_and_templates_name_rate_limit_source(
    db_session, monkeypatch, sources, expected
):
    from app.core import rate_limit_queue as rlq
    from app.core.transfer_poller import get_active_pushes
    from app.main import templates
    from tests.conftest import make_history, make_movie

    monkeypatch.setattr(
        rlq,
        "active_rate_limit_sources",
        lambda: [rlq.RateLimitSource(k, n, 60.0) for k, n in sources],
    )
    item = await make_movie(db_session)
    await make_history(
        db_session,
        item=item,
        status_detail="rate_limited",
        is_dismissed=False,
        downloader_type="sabnzbd",
    )
    await db_session.commit()
    rows = await get_active_pushes(db_session)
    assert rows and all(r["rate_limit_source"] == expected for r in rows)
    html = templates.env.from_string(
        "Paused by {{ rate_limit_source() }} rate limit"
    ).render(rate_limit_source=templates.env.globals["rate_limit_source"])
    assert html == f"Paused by {expected} rate limit"


# --- m59-20: static wording ---------------------------------------------------


async def test_settings_and_dashboard_static_wording(async_client):
    settings_html = (await async_client.get("/settings")).text
    for s in (
        "Downloaders (TorBox, SABnzbd, Premiumize)",
        "Download Completed (Ready in Downloader)",
        "Transfers on your downloader are never deleted.",
        "final downloader dispatch",
        "dispatched to the active downloader",
    ):
        assert s in settings_html
    dash_html = (await async_client.get("/")).text
    assert "Downloader Status" in dash_html
    for s in (
        "Ready on TorBox",
        "TorBox transfers",
        "fails on TorBox",
        "TorBox Status",
    ):
        assert s not in settings_html
    # Card 5 empty state ("No active TorBox transfers") is covered by m59-21.
    for s in ("Ready on TorBox", "TorBox Status"):
        assert s not in dash_html


async def test_connected_downloader_card_shows_real_type(async_client, db_session):
    from app.db.models import Provider

    db_session.add(
        Provider(
            name="sab",
            category="downloader",
            type="sabnzbd",
            api_url="http://sab.local:8080",
            is_active=True,
        )
    )
    await db_session.commit()
    html = (await async_client.get("/settings")).text
    assert "http://sab.local:8080" in html
    assert "api.torbox.app/v1/api" not in html


# --- m59-21: push modal & Card 5 wording --------------------------------------


async def test_push_modal_and_card5_wording(async_client, db_session, monkeypatch):
    from app.config import settings
    from tests.conftest import make_movie

    item = await make_movie(db_session)
    await db_session.commit()
    url = f"/api/items/{item.id}/push-modal"
    monkeypatch.setattr(settings, "dry_run", False)
    modal = (await async_client.get(url)).text
    for s in ("Dispatching to Downloader...", "Grabbed &amp; dispatched"):
        assert s in modal or s.replace("&amp;", "&") in modal
    monkeypatch.setattr(settings, "dry_run", True)
    modal += (await async_client.get(url)).text
    assert "Simulate push pipeline without dispatching to downloader" in modal
    for s in ("Pushing to TorBox", "uploading to TorBox", "Grabbed & pushed"):
        assert s not in modal
    card5 = (
        await async_client.get("/api/pushes/active", headers={"HX-Request": "true"})
    ).text
    assert "No active downloader transfers" in card5


async def test_history_row_renders_premiumize_name(async_client, db_session):
    from tests.conftest import make_history, make_movie

    item = await make_movie(db_session)
    await make_history(
        db_session,
        item=item,
        status_detail="completed",
        downloader_type="premiumize",
        downloader_transfer_id="p1",
    )
    await db_session.commit()
    html = (await async_client.get("/")).text
    assert "READY ON PREMIUMIZE" in html
    assert "Delete from Premiumize" in html


def test_dry_run_vocabulary_is_downloader_neutral():
    root = Path(__file__).resolve().parent.parent / "app" / "core"
    poller = (root / "transfer_poller.py").read_text()
    engine = (root / "push_engine.py").read_text()
    assert "Dispatch simulated (Dry-Run Mode)" in poller
    assert "TorBox upload simulated" not in poller
    assert "torbox_skipped" not in engine + poller
    assert "TorBox upload skipped" not in engine


def test_notifications_name_the_downloader():
    from app.services.discord import build_discord_embed, format_telegram_event_message

    assert (
        "Ready on SABnzbd"
        in build_discord_embed("completed", "Test Movie", downloader_name="SABnzbd")[
            "title"
        ]
    )
    assert (
        "Ready on downloader" in build_discord_embed("completed", "Test Movie")["title"]
    )
    legacy = build_discord_embed(
        "ready_on_torbox", "Test Movie", downloader_name="TorBox"
    )
    assert legacy["color"] == 0x10B981 and "Ready on TorBox" in legacy["title"]
    dry = build_discord_embed(
        "completed", "Test Movie", is_dry_run=True, downloader_name="TorBox"
    )
    assert (
        "⚠️ Dispatch simulated in Dry-Run Mode. No transfer was created on TorBox."
        in [f["value"] for f in dry["fields"]]
    )
    assert "no transfer was created on TorBox" in format_telegram_event_message(
        "completed", "Test Movie", is_dry_run=True, downloader_name="TorBox"
    )


async def test_batch_reason_uses_history_downloader(db_session, monkeypatch):
    from app.services import discord
    from tests.conftest import make_history, make_movie

    item = await make_movie(db_session)
    h = await make_history(db_session, item=item, downloader_type="premiumize")
    captured = {}

    async def fake_dispatch(session, **kw):
        captured.update(kw)
        return {}

    monkeypatch.setattr(discord, "dispatch_notification_event", fake_dispatch)
    await discord.dispatch_batch_notification_event(
        db_session, event_type="completed", histories=[h], item=item
    )
    assert captured["status_reason"] == "Ready on Premiumize (Verified Playable Video)"
    assert captured["downloader_name"] == "Premiumize"


@pytest.mark.parametrize(
    ("ptype", "fields", "cfg", "expected", "label"),
    [
        (
            "simkl",
            {"access_token": "a"},
            {"refresh_token": "r"},
            True,
            "Connected · AUTH V2 (Auto-Refresh)",
        ),
        (
            "simkl",
            {"access_token": "a"},
            {},
            False,
            "Reconnect Required (Legacy V1 / Expired)",
        ),
        ("simkl", {}, {}, False, "Not Connected"),
        ("plex", {"access_token": "t"}, {}, True, "Connected"),
        ("anilist", {"username": "u"}, {}, True, "Connected"),
    ],
)
def test_provider_is_authenticated_and_label(ptype, fields, cfg, expected, label):
    import json

    from app.db.models import Provider

    p = Provider(type=ptype, name=ptype, config_json=json.dumps(cfg), **fields)
    assert p.is_authenticated is expected
    assert p.auth_status_label == label


@pytest.mark.parametrize(
    ("ptype", "category"),
    [
        ("sabnzbd", "downloader"),
        ("torbox", "downloader"),
        ("anilist", "watchlist"),
        ("simkl", "watchlist"),
        ("nzbgeek", "indexer"),
        ("newznab", "indexer"),
        ("tmdb", "metadata"),
    ],
)
def test_provider_category_for_type(ptype, category):
    from app.services.provider_service import provider_category_for_type

    assert provider_category_for_type(ptype) == category
    assert provider_category_for_type(ptype.upper()) == category


def test_provider_category_for_unknown_type_raises():
    from app.services.provider_service import provider_category_for_type

    with pytest.raises(ValueError):
        provider_category_for_type("bogus")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data", [{"name": "x"}, {"name": "x", "provider_type": "bogus"}]
)
async def test_save_provider_requires_known_type(async_client, data):
    resp = await async_client.post("/settings/provider", data=data)
    assert resp.status_code == 422


# --- Vocabulary gate (m59 §11.6) ---

_ROOT = Path(__file__).resolve().parent.parent
_LEGACY_WORD = re.compile(r"torbox|simkl|treasure", re.IGNORECASE)
_ADAPTER_FILES = {
    "app/services/downloaders/torbox.py",
    "app/services/watchlists/simkl.py",
    "app/core/air_dates.py",
    "app/services/watchlists/simkl_adapter.py",
    "app/services/indexers/catalog.py",
    "app/services/downloaders/base.py",
    "app/services/watchlists/__init__.py",
    "app/services/provider_service.py",
}
_PROVIDER_UI_FILES = {
    "templates/modals/provider.html",
    "templates/modals/onboarding.html",
    "templates/settings.html",
}
_UI_LINE = re.compile(r"(value|name|id|@click|x-show|href)=|option|selectProvider")
_TYPE_LITERAL = re.compile(r"""['"](torbox|simkl)['"]|simkl_id\b""", re.IGNORECASE)
# An annotation comment allowlists: its own line (trailing), the paragraph below it
# (comment-only line), or the whole function/class below it (when the next
# non-comment line is a decorator, def or class).
_MARKER = re.compile(
    r"# (legacy (schema marker|domain migration) \(allowlisted\)"
    r"|legacy alias \(m59\)"
    r"|provider-specific code \(allowlisted\)"
    r")"
)
_BLOCK_START = re.compile(r"\s*(@|(async\s+)?def\s|class\s)")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _allowlisted_lines(lines: list[str]) -> set[int]:
    """1-based line numbers covered by an annotation marker."""
    covered: set[int] = set()
    for idx, line in enumerate(lines):
        if not _MARKER.search(line):
            continue
        covered.add(idx + 1)
        if line.strip().startswith("#") is False:
            if _BLOCK_START.match(line):  # trailing marker on a def line
                covered |= _block_lines(lines, idx)
            continue
        nxt = idx + 1
        while nxt < len(lines) and lines[nxt].lstrip().startswith("#"):
            nxt += 1
        if nxt >= len(lines):
            continue
        if _BLOCK_START.match(lines[nxt]):
            covered |= _block_lines(lines, nxt)
        else:
            nxt = idx + 1
            while nxt < len(lines) and lines[nxt].strip():
                covered.add(nxt + 1)
                nxt += 1
    return covered


def _block_lines(lines: list[str], start: int) -> set[int]:
    """Lines of the decorator/def/class block starting at ``start`` (0-based)."""
    i = start
    while lines[i].lstrip().startswith("@"):
        i += 1
    base = _indent(lines[i])
    end = i + 1
    while end < len(lines):
        ln = lines[end]
        if ln.strip() and _indent(ln) <= base and not ln.lstrip().startswith(")"):
            break
        end += 1
    return set(range(start + 1, end + 1))


def _in_migrate_fn(lines: list[str], no: int) -> bool:
    for i in range(no - 1, -1, -1):
        m = re.match(r"(?:async\s+)?def\s+(\w+)", lines[i])
        if m:
            return m.group(1).startswith("_migrate_")
    return False


_HTML_MARKER = "provider-specific code (allowlisted)"


def _allowlisted_html_lines(lines: list[str]) -> set[int]:
    """Templates: a marker comment covers its line, or a ``: begin`` .. ``: end`` region."""
    covered: set[int] = set()
    in_region = False
    for no, line in enumerate(lines, 1):
        if f"{_HTML_MARKER}: begin" in line:
            in_region = True
        if in_region or _HTML_MARKER in line:
            covered.add(no)
        if f"{_HTML_MARKER}: end" in line:
            in_region = False
    return covered


def _legacy_hits() -> list[str]:
    files = [
        *(_ROOT / "app").rglob("*.py"),
        *(_ROOT / "templates").rglob("*.html"),
        *(p for p in (_ROOT / "app" / "static").rglob("*") if p.is_file()),
    ]
    hits: list[str] = []
    for path in sorted(files):
        rel = path.relative_to(_ROOT).as_posix()
        if rel in _ADAPTER_FILES:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        covered = (
            _allowlisted_lines(lines)
            if path.suffix == ".py"
            else _allowlisted_html_lines(lines)
        )
        for no, line in enumerate(lines, 1):
            if not _LEGACY_WORD.search(_TYPE_LITERAL.sub("", line)):
                continue
            if rel in _PROVIDER_UI_FILES and _UI_LINE.search(line):
                continue
            if no in covered:
                continue
            if rel == "app/db/database.py" and _in_migrate_fn(lines, no):
                continue
            hits.append(f"{rel}:{no}: {line.strip()}")
    return hits


def test_no_legacy_provider_vocabulary():
    hits = _legacy_hits()
    assert not hits, "legacy provider vocabulary outside allowlist:\n" + "\n".join(hits)


# --- Downloader cooldown interface (m59-31) ---


@pytest.mark.parametrize(
    ("dtype", "limited"), [("torbox", True), ("premiumize", True), ("sabnzbd", False)]
)
def test_downloader_cooldown_via_base_interface(dtype, limited):
    from app.services.downloaders.factory import downloader_class

    cls = downloader_class(dtype)
    try:
        cls.set_cooldown(60)
        assert (cls.get_cooldown_remaining(is_manual=True) > 0) is limited
    finally:
        cls.clear_cooldown()
    assert cls.get_cooldown_remaining(is_manual=True) == 0


def test_rate_limit_queue_aggregates_registered_downloaders():
    from app.core.rate_limit_queue import (
        clear_all_rate_limit_cooldowns,
        downloader_rate_limit_detail,
        get_downloader_cooldown_remaining,
    )
    from app.services.downloaders.factory import downloader_class

    downloader_class("premiumize").set_cooldown(90)
    try:
        assert get_downloader_cooldown_remaining() > 0
        assert downloader_rate_limit_detail().startswith("Premiumize rate limit")
    finally:
        clear_all_rate_limit_cooldowns()
    assert get_downloader_cooldown_remaining() == 0


def test_only_cloud_downloaders_support_magnets():
    from app.services.downloaders.factory import downloader_classes

    assert {t for t, c in downloader_classes().items() if c.supports_magnet} == {
        "torbox"
    }
