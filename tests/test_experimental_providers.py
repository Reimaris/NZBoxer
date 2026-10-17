import pytest

from app.core.experimental import experimental_types, is_experimental
from app.services.indexers.catalog import INDEXER_PRESETS

TESTED = ["simkl", "tmdb", "torbox", "treasuremaps"]
UNTESTED = ["anilist", "plex", "mal", "sabnzbd", "premiumize", "drunkenslug", "newznab"]


@pytest.mark.parametrize("ptype", TESTED)
def test_tested_providers_not_experimental(ptype):
    assert not is_experimental(ptype)


@pytest.mark.parametrize("ptype", UNTESTED)
def test_untested_providers_experimental(ptype):
    assert is_experimental(ptype)


def test_all_indexers_but_one_experimental():
    assert [p.type for p in INDEXER_PRESETS if not p.experimental] == ["treasuremaps"]
    assert "treasuremaps" not in experimental_types()


@pytest.mark.asyncio
async def test_add_modal_shows_pills(async_client):
    html = (await async_client.get("/settings/provider/new")).text
    assert html.count(">Experimental<") >= 14


@pytest.mark.asyncio
async def test_status_lists_active_experimental_providers(db_session):
    from app.core.prerequisites import get_setup_status
    from tests.conftest import make_downloader_provider, make_watchlist_provider

    await make_watchlist_provider(db_session, "simkl")
    await make_watchlist_provider(db_session, "plex", name="My Plex")
    await make_downloader_provider(db_session, "sabnzbd", name="Sab", is_active=False)
    await db_session.commit()
    status = await get_setup_status(db_session)
    assert status.experimental_active == ("My Plex",)


@pytest.mark.asyncio
async def test_banner_shown_only_with_experimental_provider(
    session_factory, async_client
):
    from app.main import app
    from tests.conftest import make_watchlist_provider

    app.state.setup_guard_enabled = True
    try:
        assert (
            "data-experimental-banner" not in (await async_client.get("/settings")).text
        )
        async with session_factory() as s:
            await make_watchlist_provider(s, "anilist", name="AL")
            await s.commit()
        html = (await async_client.get("/settings")).text
        assert "data-experimental-banner" in html
        assert "template=bug_report.yml" in html
        # Both top banners share one sticky wrapper so they stay pinned together.
        wrapper = html[html.index('id="top-banners"') :]
        assert (
            "sticky top-14"
            in html[html.rindex("<div", 0, html.index('id="top-banners"')) :][:200]
        )
        assert wrapper.index("data-experimental-banner") < wrapper.index(
            "<!-- Global Alerts"
        )
    finally:
        app.state.setup_guard_enabled = False
