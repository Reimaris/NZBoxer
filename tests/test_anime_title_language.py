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
from app.db.models import MediaType  # noqa: E402
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
