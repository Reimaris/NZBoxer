from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.indexers.newznab import (
    DEFAULT_INDEXER_COOLDOWN_SECONDS,
    IndexerRateLimitError,
    _execute_search,
    clear_all_indexer_cooldowns,
    fetch_nzb_bytes,
    get_all_indexer_cooldowns_remaining,
    get_indexer_cooldown_remaining,
    is_indexer_in_cooldown,
    set_indexer_cooldown,
)


@pytest.fixture(autouse=True)
def clean_cooldowns():
    clear_all_indexer_cooldowns()
    yield
    clear_all_indexer_cooldowns()


def test_indexer_cooldown_helpers():
    url = "https://indexer.example.com/api"
    assert is_indexer_in_cooldown(url) is False
    assert get_indexer_cooldown_remaining(url) == 0.0
    assert get_all_indexer_cooldowns_remaining() == 0.0

    set_indexer_cooldown(url, 60)
    assert is_indexer_in_cooldown(url) is True
    assert 58.0 <= get_indexer_cooldown_remaining(url) <= 60.0
    assert 58.0 <= get_all_indexer_cooldowns_remaining() <= 60.0

    clear_all_indexer_cooldowns()
    assert is_indexer_in_cooldown(url) is False
    assert get_indexer_cooldown_remaining(url) == 0.0


def test_indexer_cooldown_normalization():
    url1 = "https://indexer.example.com/api/"
    url2 = "https://indexer.example.com/api"
    set_indexer_cooldown(url1, 100)
    assert is_indexer_in_cooldown(url2) is True
    assert get_indexer_cooldown_remaining(url2) > 0


@pytest.mark.asyncio
async def test_fetch_nzb_bytes_429_retry_after():
    url = "https://indexer.example.com/api?t=get&id=12345"
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.headers = {"Retry-After": "120"}
    req = httpx.Request("GET", url)
    http_err = httpx.HTTPStatusError(
        "Too Many Requests", request=req, response=mock_resp
    )

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = http_err

        with pytest.raises(IndexerRateLimitError) as exc_info:
            await fetch_nzb_bytes(url, api_key="dummy")

        assert exc_info.value.status_code == 429
        assert exc_info.value.retry_after_seconds == 120
        assert is_indexer_in_cooldown(url) is True


@pytest.mark.asyncio
async def test_fetch_nzb_bytes_429_default_fallback():
    url = "https://indexer.example.com/api?t=get&id=12345"
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.headers = {}
    req = httpx.Request("GET", url)
    http_err = httpx.HTTPStatusError(
        "Too Many Requests", request=req, response=mock_resp
    )

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = http_err

        with pytest.raises(IndexerRateLimitError) as exc_info:
            await fetch_nzb_bytes(url, api_key="dummy")

        assert exc_info.value.status_code == 429
        assert exc_info.value.retry_after_seconds == DEFAULT_INDEXER_COOLDOWN_SECONDS
        assert is_indexer_in_cooldown(url) is True


@pytest.mark.asyncio
async def test_execute_search_active_cooldown_prevents_network_call():
    url = "https://indexer.example.com/api"
    set_indexer_cooldown(url, 300)

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        with pytest.raises(IndexerRateLimitError) as exc_info:
            await _execute_search(url, {"q": "ubuntu"})

        mock_get.assert_not_called()
        assert exc_info.value.status_code == 429
        assert exc_info.value.retry_after_seconds > 0


@pytest.mark.asyncio
async def test_execute_search_429_raises_and_sets_cooldown():
    url = "https://indexer.example.com/api"
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.headers = {"Retry-After": "45"}
    req = httpx.Request("GET", url)
    http_err = httpx.HTTPStatusError(
        "Too Many Requests", request=req, response=mock_resp
    )

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = http_err

        with pytest.raises(IndexerRateLimitError) as exc_info:
            await _execute_search(url, {"q": "ubuntu"})

        assert exc_info.value.status_code == 429
        assert exc_info.value.retry_after_seconds == 45
        assert is_indexer_in_cooldown(url) is True


@pytest.mark.asyncio
async def test_candidate_burn_prevention_on_fetch_nzb_bytes_rate_limit(db_session):
    from app.core.push_engine import _dispatch_candidate_list_to_downloader
    from tests.conftest import make_series

    item, seasons, episodes = await make_series(
        db_session, seasons=1, episodes_per_season=3
    )
    season = seasons[0]
    ep = episodes[0]

    cands = [
        {
            "guid": f"guid-{i}",
            "title": f"Test.Show.S01E01.720p-GRP{i}",
            "indexer_url": "https://indexer1.example.com/api",
            "indexer_key": "k1",
            "score": 1000 - i * 10,
        }
        for i in range(1, 6)
    ]

    fetch_mock = AsyncMock(
        side_effect=IndexerRateLimitError(
            "Grab limit reached: HTTP 429",
            status_code=429,
            retry_after_seconds=120,
            indexer_url="https://indexer1.example.com/api",
        )
    )
    errors = []
    with patch("app.services.indexers.newznab.fetch_nzb_bytes", new=fetch_mock):
        result = await _dispatch_candidate_list_to_downloader(
            db_session,
            cands,
            item=item,
            season=season,
            episode=ep,
            push_mode="auto",
            error_out=errors,
        )

    assert result is None
    assert fetch_mock.call_count == 1
    assert len(errors) == 1
    assert "Indexer rate limit exceeded" in errors[0]


@pytest.mark.asyncio
async def test_episodic_push_rate_limit_queueing_and_no_fail_count_penalty(db_session):
    from app.core.push_engine import execute_auto_push
    from app.db.models import EpisodeStatus, MediaStatus, SeasonStatus
    from tests.conftest import make_series

    item, seasons, episodes = await make_series(
        db_session, seasons=1, episodes_per_season=3
    )

    fetch_mock = AsyncMock(
        side_effect=IndexerRateLimitError(
            "Grab limit reached: HTTP 429",
            status_code=429,
            retry_after_seconds=120,
            indexer_url="https://indexer1.example.com/api",
        )
    )
    search_mock = AsyncMock(
        return_value=[
            {
                "title": f"Test.Series.S01E{i:02d}.1080p.WEB-DL-GRP",
                "guid": f"guid-ep-{i}",
                "size_bytes": 1024**3,
                "indexer_url": "https://indexer1.example.com/api",
                "indexer_key": "k1",
            }
            for i in range(1, 4)
        ]
    )

    with (
        patch("app.services.indexers.newznab.search_show", new=search_mock),
        patch("app.services.indexers.newznab.fetch_nzb_bytes", new=fetch_mock),
    ):
        res = await execute_auto_push(
            db_session, item.id, {"allow_season_packs": False}
        )

    assert res["pushed"] is False
    assert fetch_mock.call_count == 1

    await db_session.refresh(item)
    for s in seasons:
        await db_session.refresh(s)
    for ep in episodes:
        await db_session.refresh(ep)

    assert item.status == MediaStatus.RATE_LIMITED
    assert seasons[0].status == SeasonStatus.RATE_LIMITED
    assert all(ep.status == EpisodeStatus.RATE_LIMITED for ep in episodes)

    assert item.fail_count == 0
    assert seasons[0].fail_count == 0
    assert all(ep.fail_count == 0 for ep in episodes)


@pytest.mark.parametrize(
    ("msg", "expected"),
    [
        ("TorBox rate limit exceeded: wait 60s", True),
        ("HTTP 429 Too Many Requests", True),
        ("Grab limit reached: HTTP 429", True),
        ("too many requests for user", True),
        ("NZB download failed: 404 Not Found", False),
        ("Connection timed out", False),
        (None, False),
    ],
)
def test_is_rate_limit_error(msg: str | None, expected: bool):
    from app.core.rate_limit_queue import is_rate_limit_error

    assert is_rate_limit_error(msg) is expected


def test_resume_time_label_and_cooldown_helpers():
    from app.core.rate_limit_queue import (
        clear_all_rate_limit_cooldowns,
        resume_time_label,
    )
    from app.services.downloaders import torbox
    from app.services.indexers import newznab as newznab_client

    clear_all_rate_limit_cooldowns()
    assert resume_time_label() == "resuming shortly"

    # Only indexer cooldown active
    newznab_client.set_indexer_cooldown("https://indexer1.example.com/api", 120)
    label = resume_time_label()
    assert label.startswith("resumes ")

    # TorBox cooldown active and longer
    torbox.set_cooldown(300)
    label2 = resume_time_label()
    assert label2.startswith("resumes ")

    # Clearing all cooldowns
    clear_all_rate_limit_cooldowns()
    assert torbox.get_cooldown_remaining(is_manual=True) == 0.0
    assert newznab_client.get_all_indexer_cooldowns_remaining() == 0.0
    assert resume_time_label() == "resuming shortly"


@pytest.mark.asyncio
async def test_run_rate_limit_resume_tick_respects_indexer_cooldown():
    from app.core.rate_limit_queue import run_rate_limit_resume_tick
    from app.services.indexers import newznab as newznab_client

    newznab_client.set_indexer_cooldown("https://indexer1.example.com/api", 180)

    with patch(
        "app.core.rate_limit_queue.resume_rate_limited_pushes",
        new_callable=AsyncMock,
    ) as mock_resume:
        await run_rate_limit_resume_tick()
        mock_resume.assert_not_called()

        newznab_client.clear_all_indexer_cooldowns()
        await run_rate_limit_resume_tick()
        mock_resume.assert_called_once()


@pytest.mark.asyncio
async def test_resume_rate_limited_api_json(async_client):
    from app.services.downloaders import torbox
    from app.services.indexers import newznab as newznab_client

    torbox.set_cooldown(120)
    newznab_client.set_indexer_cooldown("https://indexer1.example.com/api", 180)

    with patch(
        "app.core.rate_limit_queue.run_rate_limit_resume_tick",
        new_callable=AsyncMock,
    ) as mock_tick:
        resp = await async_client.post("/api/pushes/resume-rate-limited")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "resumed"
    assert data["cooldowns_cleared"] is True
    assert torbox.get_cooldown_remaining(is_manual=True) == 0.0
    assert newznab_client.get_all_indexer_cooldowns_remaining() == 0.0
    mock_tick.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_rate_limited_api_htmx(async_client, db_session):
    from tests.conftest import make_history, make_movie

    movie = await make_movie(db_session, title="HTMX Test Movie")
    await make_history(
        db_session,
        item=movie,
        status_detail="rate_limited",
        is_dismissed=False,
    )
    await db_session.commit()

    with patch(
        "app.core.rate_limit_queue.run_rate_limit_resume_tick",
        new_callable=AsyncMock,
    ):
        resp = await async_client.post(
            "/api/pushes/resume-rate-limited",
            headers={"HX-Request": "true"},
        )

    assert resp.status_code == 200
    assert "refreshDashboard" in resp.headers.get("HX-Trigger", "")
    html = resp.text
    assert "⚡ Resume Queue" in html
    assert "push(es) paused: Provider rate limit active." in html


@pytest.mark.asyncio
async def test_active_pushes_template_indexer_limit_badge(async_client, db_session):
    from tests.conftest import make_history, make_movie

    movie = await make_movie(db_session, title="Indexer Throttle Movie")
    await make_history(
        db_session,
        item=movie,
        status_detail="Indexer rate limit exceeded: HTTP 429",
        is_dismissed=False,
    )
    await db_session.commit()

    resp = await async_client.get(
        "/api/pushes/active",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    html = resp.text
    assert "Paused — indexer limit" in html
    assert "⚡ Resume Queue" in html
