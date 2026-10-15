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


# --- m65-02: title resolution helpers -------------------------------------

from app.core.anime_titles import (  # noqa: E402
    anime_season_title_for,
    anime_title_for,
    extract_title_variants,
    find_matched_child,
    normalize_anime_title_language,
    resolve_display_title,
    search_names,
)
from app.db.models import MediaStatus, MediaType  # noqa: E402
from tests.conftest import make_movie, make_series  # noqa: E402

NODE_AOT = {
    "id": 16498,
    "title": {
        "romaji": "Shingeki no Kyojin",
        "english": "Attack on Titan",
        "native": "進撃の巨人",
    },
}
NODE_ENGLISH_MISSING = {
    "id": 1,
    "title": {"romaji": "Foo no Bar", "english": None, "native": "フー"},
}
FULL = {"english": "EN", "romaji": "RO", "native": "NA"}


@pytest.mark.parametrize(
    ("language", "variants", "expected"),
    [
        ("english", FULL, "EN"),
        ("romaji", FULL, "RO"),
        ("native", FULL, "NA"),
        ("english", {**FULL, "english": None}, "RO"),
        ("english", {"english": " ", "romaji": None, "native": "NA"}, "NA"),
        ("romaji", {**FULL, "romaji": None}, "EN"),
        ("romaji", {"english": None, "romaji": "", "native": "NA"}, "NA"),
        ("native", {**FULL, "native": None}, "RO"),
        ("native", {"english": "EN", "romaji": None, "native": None}, "EN"),
        ("english", {"english": None, "romaji": None, "native": None}, "T"),
        ("romaji", {"english": None, "romaji": None, "native": None}, "T"),
        ("native", {"english": None, "romaji": None, "native": None}, "T"),
        ("klingon", {**FULL, "romaji": None}, "EN"),
    ],
)
def test_resolve_display_title(language, variants, expected):
    assert resolve_display_title(language, title="T", **variants) == expected


def test_resolve_display_title_all_blank():
    blank = {"english": None, "romaji": None, "native": None}
    assert resolve_display_title("romaji", title=None, **blank) == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ENGLISH ", "english"),
        ("native", "native"),
        ("klingon", "romaji"),
        (None, "romaji"),
    ],
)
def test_normalize_language(value, expected):
    assert normalize_anime_title_language(value) == expected


def test_extract_title_variants():
    assert extract_title_variants(NODE_ENGLISH_MISSING) == {
        "title_english": None,
        "title_romaji": "Foo no Bar",
        "title_native": "フー",
    }
    assert extract_title_variants({"id": 2})["title_native"] is None


def test_search_names_dedupes_and_lowercases():
    assert search_names("Foo", None, " foo ", "Bar", "") == "foo bar"


def test_find_matched_child():
    children = [
        {"label": "Season 4", "names": "final season", "title": "Final Season"},
        {"label": "Movie 1", "names": "movie", "title": "Attack on Titan"},
    ]
    hit = find_matched_child(children, "attack on titan", "final", "Attack on Titan")
    assert hit == {"label": "Season 4", "title": "Final Season"}
    assert find_matched_child(children, "attack on titan", "attack", "AoT") is None
    assert find_matched_child(children, "x", "movie", "attack on titan") is None
    assert find_matched_child(children, "x", "  ", "t") is None


@pytest.mark.anyio
async def test_model_and_scope_helpers(db_session):
    item, seasons, _ = await make_series(
        db_session,
        seasons=1,
        media_type=MediaType.ANIME,
        title="Attack on Titan",
        title_english="Attack on Titan",
        title_romaji="Shingeki no Kyojin",
        title_native="進撃の巨人",
    )
    assert anime_title_for(item, "native") == "進撃の巨人"
    assert anime_title_for(item, "bogus") == "Shingeki no Kyojin"
    season = seasons[0]
    season.title = None
    assert season.display_title("english") == ""
    assert anime_season_title_for(item, season, "english") == "Season 1"
    season.title_english = "Final"
    assert anime_season_title_for(item, season, "english") == "Final"

    show = await make_movie(db_session, title="Plain", title_romaji="Variant")
    assert anime_title_for(show, "romaji") == "Plain"
    movie = await make_movie(
        db_session, title="M", title_romaji="Variant", is_anime_movie=True
    )
    assert anime_title_for(movie, "romaji") == "Variant"


def test_jinja_globals_registered():
    from app.main import templates

    assert templates.env.globals["anime_title_for"] is anime_title_for
    assert templates.env.globals["anime_season_title_for"] is anime_season_title_for


@pytest.mark.anyio
async def test_push_modal_movie_entry_has_no_episode_accordion(
    db_session, async_client, default_search_preset
):
    item, seasons, _ = await make_series(
        db_session,
        seasons=[
            {"season_number": 1, "entry_type": "season", "title": "Arc"},
            {"season_number": 2, "entry_type": "movie", "title": "Film"},
        ],
        episodes_per_season=1,
        media_type=MediaType.ANIME,
    )
    await db_session.commit()
    season, movie = seasons

    html = (
        await async_client.get(
            f"/api/items/{item.id}/push-modal", headers={"Accept": "text/html"}
        )
    ).text

    assert f"expandedSeasons[{season.id}]" in html
    assert "Episodes (1)" in html
    assert f"expandedSeasons[{movie.id}]" not in html
    assert html.count("Episodes (1)") == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("posted", "stored"),
    [("english", "english"), ("native", "native"), ("bogus", "romaji")],
)
async def test_settings_auto_save_anime_title_language(
    async_client, db_session, posted, stored
):
    from app.db.models import SystemSettings

    resp = await async_client.post(
        "/settings/auto-save", data={"anime_title_language": posted}
    )
    assert resp.status_code < 400
    db_session.expire_all()
    row = await db_session.get(SystemSettings, 1)
    assert row is not None
    assert row.anime_title_language == stored

    html = (await async_client.get("/settings")).text
    assert 'name="anime_title_language"' in html
    assert f'value="{stored}" selected' in html


NODE_AOT_S4 = {
    "id": 110277,
    "title": {
        "romaji": "Shingeki no Kyojin: The Final Season",
        "english": "Attack on Titan: The Final Season",
        "native": "進撃の巨人 The Final Season",
    },
}


@pytest.mark.anyio
async def test_enrich_anime_metadata_persists_title_variants(db_session):
    from unittest.mock import AsyncMock, patch

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.core.automation import enrich_anime_metadata
    from app.db.models import MediaItem

    item, _, _ = await make_series(
        db_session,
        seasons=0,
        media_type=MediaType.ANIME,
        title="Attack on Titan",
        anilist_id=16498,
        title_native="既存",
    )
    await db_session.flush()
    hierarchy = [
        {**NODE_AOT, "format": "TV", "episodes": 1, "entry_type": "season"},
        {**NODE_AOT_S4, "format": "TV", "episodes": 1, "entry_type": "season"},
    ]
    root = {**NODE_AOT, "title": {**NODE_AOT["title"], "native": None}}
    payload = {"root": root, "hierarchy": hierarchy}
    with patch(
        "app.services.anilist.get_anime_root_and_hierarchy",
        new=AsyncMock(return_value=payload),
    ):
        await enrich_anime_metadata(db_session, item)

    loaded = (
        await db_session.execute(
            select(MediaItem)
            .where(MediaItem.id == item.id)
            .options(selectinload(MediaItem.seasons))
        )
    ).scalar_one()
    # Blank AniList value never overwrites an existing variant; title untouched.
    assert (loaded.title_english, loaded.title_romaji) == (
        "Attack on Titan",
        "Shingeki no Kyojin",
    )
    assert loaded.title_native == "既存"
    assert loaded.title == "Attack on Titan"
    s4 = next(s for s in loaded.seasons if s.anilist_id == 110277)
    assert s4.title_romaji == "Shingeki no Kyojin: The Final Season"
    assert s4.title_native == "進撃の巨人 The Final Season"
    assert s4.display_title("english") == "Attack on Titan: The Final Season"


# --- m65-06: startup backfill ----------------------------------------------


@pytest.fixture
def backfill_sleeps(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("app.services.anime_title_backfill.asyncio.sleep", fake_sleep)
    return sleeps


@pytest.mark.anyio
async def test_backfill_populates_item_and_season(
    db_session, session_factory, backfill_sleeps
):
    from sqlalchemy import select

    from app.db.models import MediaItem, Season
    from app.services.anime_title_backfill import run_anime_title_backfill

    item, seasons, _ = await make_series(
        db_session, seasons=1, media_type=MediaType.ANIME, anilist_id=16498
    )
    seasons[0].anilist_id = 110277
    await db_session.commit()

    calls: list[list[int]] = []

    async def fetch(ids):
        calls.append(ids)
        return {16498: NODE_AOT, 110277: NODE_AOT_S4}

    stats = await run_anime_title_backfill(session_factory, fetch=fetch)
    assert (stats.items_updated, stats.seasons_updated) == (1, 1)
    assert stats.ids_missing == 0 and stats.chunks_failed == 0
    assert len(calls) == 1 and backfill_sleeps == []

    async with session_factory() as s:
        got = (await s.execute(select(MediaItem))).scalar_one()
        assert got.title_romaji == "Shingeki no Kyojin"
        season = (await s.execute(select(Season))).scalar_one()
        assert season.title_native == "進撃の巨人 The Final Season"

    # Idempotent: everything populated -> no further fetch.
    stats = await run_anime_title_backfill(session_factory, fetch=fetch)
    assert len(calls) == 1
    assert (stats.items_updated, stats.seasons_updated) == (0, 0)


@pytest.mark.anyio
async def test_backfill_chunks_sleeps_and_isolates_failures(
    db_session, session_factory, backfill_sleeps
):
    from app.services.anime_title_backfill import run_anime_title_backfill

    for aid in range(1, 121):
        await make_series(
            db_session,
            seasons=0,
            media_type=MediaType.ANIME,
            title=f"A{aid}",
            anilist_id=aid,
            simkl_id=50000 + aid,
        )
    await db_session.commit()

    calls: list[list[int]] = []

    async def fetch(ids):
        calls.append(ids)
        if len(calls) == 2:
            raise RuntimeError("boom <secret>")
        return {i: {"id": i, "title": {"romaji": f"R{i}"}} for i in ids}

    stats = await run_anime_title_backfill(session_factory, fetch=fetch)
    assert [len(c) for c in calls] == [50, 50, 20]
    assert backfill_sleeps == [2.0, 2.0]
    assert stats.chunks_failed == 1
    assert stats.items_updated == 70


@pytest.mark.anyio
async def test_backfill_skips_live_action_and_empty(
    db_session, session_factory, backfill_sleeps
):
    from app.services.anime_title_backfill import (
        BackfillStats,
        run_anime_title_backfill,
    )

    await make_movie(db_session, anilist_id=999)
    await db_session.commit()

    async def fetch(ids):  # pragma: no cover - must not be called
        raise AssertionError("no request expected")

    assert await run_anime_title_backfill(session_factory, fetch=fetch) == (
        BackfillStats()
    )


# --- m65-07: display in Card 3 and push modal -------------------------------


async def _seed_titled(db_session, language: str, media_type=MediaType.ANIME):
    from app.db.models import SystemSettings
    from tests.conftest import seed_ready_setup

    await seed_ready_setup(db_session)
    settings_row = await db_session.get(SystemSettings, 1)
    if settings_row is None:
        settings_row = SystemSettings(id=1)
        db_session.add(settings_row)
    settings_row.anime_title_language = language
    item, seasons, _ = await make_series(
        db_session,
        seasons=1,
        media_type=media_type,
        status=MediaStatus.SEARCHING,
        title="Shingeki no Kyojin",
        title_english="Attack on Titan",
        title_romaji="Shingeki no Kyojin",
        title_native="進撃の巨人",
    )
    seasons[0].title = "Season One"
    seasons[0].title_english = "AoT Season One"
    await db_session.commit()
    return item


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("english", "Attack on Titan"),
        ("native", "進撃の巨人"),
        ("romaji", "Shingeki no Kyojin"),
    ],
)
async def test_dashboard_and_modal_use_selected_language(
    async_client, db_session, language, expected
):
    item = await _seed_titled(db_session, language)
    dash = (await async_client.get("/")).text
    assert f"Open Unified Push Modal for {expected}" in dash

    modal = (await async_client.get(f"/api/items/{item.id}/push-modal")).text
    assert f">{expected}</h2>" in modal

    data = (
        await async_client.get(
            f"/api/items/{item.id}/push-modal",
            headers={"accept": "application/json"},
        )
    ).json()
    assert data["item"]["display_title"] == expected
    assert data["item"]["title"] == "Shingeki no Kyojin"
    # Season only has an English variant: every chain falls back to it.
    assert data["entries"][0]["display_title"] == "AoT Season One"


@pytest.mark.anyio
async def test_live_action_show_ignores_variants(async_client, db_session):
    item = await _seed_titled(db_session, "native", media_type=MediaType.SHOW)
    dash = (await async_client.get("/")).text
    assert "Open Unified Push Modal for Shingeki no Kyojin" in dash
    assert "進撃の巨人" not in dash
    data = (
        await async_client.get(
            f"/api/items/{item.id}/push-modal",
            headers={"accept": "application/json"},
        )
    ).json()
    assert data["item"]["display_title"] == "Shingeki no Kyojin"
    assert data["entries"][0]["display_title"] == "Season One"
