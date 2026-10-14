import itertools

import pytest

from app.core.prerequisites import get_setup_status
from app.db.models import Provider
from tests.conftest import (
    make_downloader_provider,
    make_indexer_provider,
    make_watchlist_provider,
)


async def _seed(session, *, tmdb=1, indexer=1, downloader=1, watchlist=1):
    if tmdb:
        session.add(
            Provider(
                name="tmdb",
                type="tmdb",
                category="metadata",
                api_key="k",
                is_active=True,
            )
        )
    if indexer:
        await make_indexer_provider(session)
    if downloader:
        await make_downloader_provider(session)
    if watchlist:
        await make_watchlist_provider(
            session, "simkl", client_id="cid", access_token="tok"
        )
    await session.flush()


@pytest.mark.parametrize(
    ("tmdb", "indexer", "downloader", "watchlist"),
    list(itertools.product((0, 1), repeat=4)),
)
async def test_setup_status_truth_table(
    db_session, tmdb, indexer, downloader, watchlist
):
    await _seed(
        db_session,
        tmdb=tmdb,
        indexer=indexer,
        downloader=downloader,
        watchlist=watchlist,
    )
    status = await get_setup_status(db_session)
    expected = tuple(
        n
        for n, ok in (("tmdb", tmdb), ("indexer", indexer), ("downloader", downloader))
        if not ok
    )
    assert status.missing_core == expected
    assert status.has_watchlist is bool(watchlist)
    assert status.core_ready is (not expected)
    assert status.watchlist_ready is (not expected and bool(watchlist))


@pytest.mark.parametrize("category", ["indexer", "downloader"])
async def test_inactive_provider_does_not_count(db_session, category):
    await _seed(
        db_session,
        indexer=int(category != "indexer"),
        downloader=int(category != "downloader"),
    )
    factory = (
        make_indexer_provider if category == "indexer" else make_downloader_provider
    )
    await factory(db_session, is_active=False)
    assert category in (await get_setup_status(db_session)).missing_core


@pytest.mark.parametrize("category", ["indexer", "downloader"])
async def test_credentialless_provider_does_not_count(db_session, category):
    await _seed(
        db_session,
        indexer=int(category != "indexer"),
        downloader=int(category != "downloader"),
    )
    factory = (
        make_indexer_provider if category == "indexer" else make_downloader_provider
    )
    await factory(db_session, api_key="")
    assert category in (await get_setup_status(db_session)).missing_core


async def test_sabnzbd_needs_api_url(db_session):
    await _seed(db_session, downloader=0)
    await make_downloader_provider(db_session, "sabnzbd", api_key=None)
    assert "downloader" in (await get_setup_status(db_session)).missing_core
    await make_downloader_provider(
        db_session, "sabnzbd", api_key=None, api_url="http://sab"
    )
    assert (await get_setup_status(db_session)).core_ready


async def test_missing_core_order_is_fixed(db_session):
    status = await get_setup_status(db_session)
    assert status.missing_core == ("tmdb", "indexer", "downloader")


@pytest.fixture
def guard_on():
    from app.main import app

    app.state.setup_guard_enabled = True
    yield


async def _commit_core(
    session_factory, *, tmdb=True, indexer=True, downloader=True, watchlist=False
):
    async with session_factory() as s:
        await _seed(
            s, tmdb=tmdb, indexer=indexer, downloader=downloader, watchlist=watchlist
        )
        await s.commit()


async def test_guard_blocks_page_with_setup_screen(guard_on, async_client):
    r = await async_client.get("/")
    assert r.status_code == 200
    assert "Setup required" in r.text
    assert "TMDB API key" in r.text


async def test_guard_blocks_api_with_403_json(guard_on, async_client):
    r = await async_client.post("/api/search/manual", data={"query": "x"})
    assert r.status_code == 403
    assert r.json() == {
        "detail": "setup_required",
        "missing": ["tmdb", "indexer", "downloader"],
    }


async def test_guard_htmx_gets_toast_fragment(guard_on, async_client):
    r = await async_client.post("/api/search/manual", headers={"HX-Request": "true"})
    assert r.status_code == 403
    assert "Setup required: TMDB API key, Indexer, Downloader" in r.text
    assert r.headers["X-Status-Span"] == "1"


@pytest.mark.parametrize("path", ["/settings", "/static/does-not-exist.css"])
async def test_guard_allows_open_paths(guard_on, async_client, path):
    r = await async_client.get(path)
    assert "setup_required" not in r.text
    assert r.status_code != 403


async def test_guard_unblocks_when_core_ready(guard_on, async_client, session_factory):
    await _commit_core(session_factory)
    r = await async_client.get("/")
    assert "Setup required" not in r.text


async def test_guard_blocks_simkl_only_install(guard_on, async_client, session_factory):
    await _commit_core(
        session_factory, tmdb=False, indexer=False, downloader=False, watchlist=True
    )
    r = await async_client.get("/")
    assert "Setup required" in r.text


_ANCHORS = {
    "tmdb": "/settings#metadata",
    "indexer": "/settings#indexers",
    "downloader": "/settings#downloaders",
}


@pytest.mark.parametrize(
    "present",
    [(), ("tmdb",), ("indexer", "downloader"), ("tmdb", "indexer")],
)
async def test_setup_page_links_only_missing_items(
    guard_on, async_client, session_factory, present
):
    await _commit_core(
        session_factory,
        tmdb="tmdb" in present,
        indexer="indexer" in present,
        downloader="downloader" in present,
        watchlist=False,
    )
    html_text = (await async_client.get("/")).text
    for key, href in _ANCHORS.items():
        assert (f'href="{href}"' in html_text) is (key not in present)
    assert 'hx-get="/onboarding/modal"' in html_text


async def test_onboarding_modal_reachable_while_blocked(guard_on, async_client):
    assert (await async_client.get("/onboarding/modal")).status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "blocked"),
    [
        ("POST", "/sync", True),
        ("POST", "/settings/provider/1/sync", True),
        ("POST", "/api/search/manual", False),
        ("GET", "/settings", False),
    ],
)
async def test_watchlist_routes_blocked_without_watchlist(
    guard_on, async_client, session_factory, method, path, blocked
):
    await _commit_core(session_factory)
    r = await async_client.request(method, path)
    assert (r.status_code == 403) is blocked
    if blocked:
        assert r.json() == {"detail": "setup_required", "missing": ["watchlist"]}


async def test_dashboard_renders_without_watchlist(
    guard_on, async_client, session_factory
):
    await _commit_core(session_factory)
    r = await async_client.get("/")
    assert "Setup required" not in r.text


async def test_watchlist_provider_reopens_sync(guard_on, async_client, session_factory):
    await _commit_core(session_factory, watchlist=True)
    r = await async_client.post("/sync")
    assert r.status_code != 403


async def test_dashboard_without_watchlist_shows_only_adhoc(
    guard_on, async_client, session_factory
):
    await _commit_core(session_factory)
    text = (await async_client.get("/")).text
    assert "No watchlist connected" in text
    assert "Ad-Hoc Manual Search" in text
    assert 'hx-post="/sync"' not in text
    assert 'id="deck-card-count-movie"' not in text


async def test_dashboard_with_watchlist_shows_sync_and_cards(
    guard_on, async_client, session_factory
):
    await _commit_core(session_factory, watchlist=True)
    text = (await async_client.get("/")).text
    assert "No watchlist connected" not in text
    assert 'hx-post="/sync"' in text
    assert 'id="deck-card-count-movie"' in text


@pytest.fixture
def job_guard_on():
    from app.core import prerequisites

    prerequisites.JOB_GUARD_ENABLED = True
    yield prerequisites


async def test_orchestrator_tick_skipped_when_core_missing(job_guard_on, caplog):
    import logging
    from unittest.mock import AsyncMock, patch

    from app.core import automation

    caplog.set_level(logging.DEBUG)
    with (
        patch.object(
            automation, "rollover_due_future_episodes_and_seasons", new=AsyncMock()
        ) as rollover,
        patch.object(automation, "sync_all_providers", new=AsyncMock()) as sync,
    ):
        await automation.run_orchestrator_tick()
        await automation.run_orchestrator_tick()
    rollover.assert_not_called()
    sync.assert_not_called()
    msg = "[GUARD] Setup incomplete (missing: tmdb, indexer, downloader): orchestrator skipped"
    guard = [r for r in caplog.records if msg in r.getMessage()]
    assert [r.levelno for r in guard] == [logging.INFO, logging.DEBUG]


async def test_orchestrator_without_watchlist_rolls_over_but_skips_sync(
    job_guard_on, session_factory
):
    from unittest.mock import AsyncMock, patch

    from app.core import automation

    await _commit_core(session_factory)
    with (
        patch.object(
            automation,
            "rollover_due_future_episodes_and_seasons",
            new=AsyncMock(
                return_value={"episodes_rolled_over": 0, "seasons_rolled_over": 0}
            ),
        ) as rollover,
        patch.object(automation, "sync_all_providers", new=AsyncMock()) as sync,
        patch.object(
            automation.air_date_engine,
            "enrich_active_shows_episode_air_dates",
            new=AsyncMock(),
        ),
    ):
        await automation.run_orchestrator_tick()
    rollover.assert_called_once()
    sync.assert_not_called()


async def test_periodic_sync_and_air_push_skipped_when_setup_incomplete(job_guard_on):
    from app.core import air_push_engine, automation

    assert await automation.run_periodic_watchlist_sync_if_due() is False
    assert await air_push_engine.run_air_date_push_and_upgrade_tick() is None


def test_job_guard_flag_restored_between_tests():
    from app.core import prerequisites

    assert prerequisites.JOB_GUARD_ENABLED is False


async def test_transfer_poller_and_rate_limit_ticks_skipped_without_providers(
    job_guard_on, caplog
):
    import logging
    from unittest.mock import AsyncMock, patch

    from app.core import rate_limit_queue, transfer_poller

    caplog.set_level(logging.INFO)
    with (
        patch.object(
            rate_limit_queue, "resume_rate_limited_pushes", new=AsyncMock()
        ) as resume,
        patch.object(
            transfer_poller, "count_downloading_entities", new=AsyncMock()
        ) as count,
    ):
        result = await transfer_poller.run_transfer_poller_tick()
        await rate_limit_queue.run_rate_limit_resume_tick()
    assert result["polled"] is False
    count.assert_not_called()
    resume.assert_not_called()
    text = caplog.text
    assert "(missing: tmdb, indexer, downloader): transfer poller skipped" not in text
    assert "(missing: downloader): transfer poller skipped" in text
    assert "(missing: indexer, downloader): rate-limit resume skipped" in text


async def test_transfer_and_rate_limit_ticks_run_with_providers(
    job_guard_on, session_factory
):
    from unittest.mock import AsyncMock, patch

    from app.core import rate_limit_queue, transfer_poller

    await _commit_core(session_factory, tmdb=False)
    with patch.object(
        rate_limit_queue, "resume_rate_limited_pushes", new=AsyncMock()
    ) as resume:
        await rate_limit_queue.run_rate_limit_resume_tick()
        await transfer_poller.run_transfer_poller_tick()
    resume.assert_called_once()


async def test_setup_status_endpoint_reports_readiness(
    guard_on, async_client, session_factory
):
    r = await async_client.get("/setup/status")
    assert r.json() == {"ready": False, "missing": ["tmdb", "indexer", "downloader"]}
    await _commit_core(session_factory)
    assert (await async_client.get("/setup/status")).json()["ready"] is True


async def test_setup_page_polls_status_and_settings_hides_sync_without_watchlist(
    guard_on, async_client, session_factory
):
    assert "/setup/status" in (await async_client.get("/")).text
    # Settings is open but must still hide the Sync button without a watchlist provider.
    await _commit_core(session_factory)
    assert 'hx-post="/sync"' not in (await async_client.get("/settings")).text
    await _commit_core(
        session_factory, tmdb=False, indexer=False, downloader=False, watchlist=True
    )
    assert 'hx-post="/sync"' in (await async_client.get("/settings")).text
