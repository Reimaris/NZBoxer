"""Unit tests for TorBox split-budget rate limiter, circuit breaker, and TTL cache."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.downloaders import torbox
from app.services.downloaders.torbox import DownloaderNetworkError


@pytest.fixture(autouse=True)
def reset_torbox_cooldown():
    """Reset global cooldown, limiter timestamps, and cache state before and after each test."""
    torbox.set_cooldown(0.0)
    torbox._auto_send_limiter.timestamps = []
    torbox._manual_send_limiter.timestamps = []
    torbox._cached_usenet_downloads = []
    torbox._cached_usenet_downloads_at = 0.0
    yield
    torbox.set_cooldown(0.0)
    torbox._auto_send_limiter.timestamps = []
    torbox._manual_send_limiter.timestamps = []
    torbox._cached_usenet_downloads = []
    torbox._cached_usenet_downloads_at = 0.0


def test_cooldown_get_and_set():
    """Verify set_cooldown and get_cooldown_remaining work as expected."""
    assert torbox.get_cooldown_remaining() == 0.0

    torbox.set_cooldown(100.0)
    remaining = torbox.get_cooldown_remaining()
    assert 95.0 <= remaining <= 100.0

    torbox.set_cooldown(0.0)
    assert torbox.get_cooldown_remaining() == 0.0


@pytest.mark.asyncio
async def test_send_nzb_file_fails_fast_when_cooldown_active():
    """Verify that send_nzb_file raises DownloaderNetworkError immediately if cooldown is active."""
    torbox.set_cooldown(60.0)

    with patch("httpx.AsyncClient") as mock_client:
        with pytest.raises(DownloaderNetworkError) as exc_info:
            await torbox.send_nzb_file(
                b"dummy nzb", filename="test.nzb", explicit_key="dummy_key"
            )
        assert "cooldown active" in str(exc_info.value).lower()
        mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_send_nzb_file_handles_429_retry_after():
    """Verify that a 429 response parses Retry-After header and activates cooldown."""
    response_429 = httpx.Response(
        status_code=429,
        headers={"Retry-After": "45"},
        json={"detail": "Rate limit exceeded"},
        request=httpx.Request(
            "POST", "https://api.torbox.app/v1/api/usenet/createusenetdownload"
        ),
    )

    with patch("httpx.AsyncClient") as mock_client_cls:
        client_instance = AsyncMock()
        client_instance.post.return_value = response_429
        client_instance.__aenter__.return_value = client_instance
        mock_client_cls.return_value = client_instance

        with pytest.raises(DownloaderNetworkError) as exc_info:
            await torbox.send_nzb_file(
                b"dummy nzb", filename="test.nzb", explicit_key="dummy_key"
            )

        assert "rate limit exceeded" in str(exc_info.value).lower()
        remaining = torbox.get_cooldown_remaining()
        assert 40.0 <= remaining <= 45.0


@pytest.mark.asyncio
async def test_send_nzb_file_handles_429_missing_retry_after_default():
    """Verify that a 429 without Retry-After defaults to 300s cooldown."""
    response_429 = httpx.Response(
        status_code=429,
        headers={},
        json={"detail": "Rate limit exceeded"},
        request=httpx.Request(
            "POST", "https://api.torbox.app/v1/api/usenet/createusenetdownload"
        ),
    )

    with patch("httpx.AsyncClient") as mock_client_cls:
        client_instance = AsyncMock()
        client_instance.post.return_value = response_429
        client_instance.__aenter__.return_value = client_instance
        mock_client_cls.return_value = client_instance

        with pytest.raises(DownloaderNetworkError):
            await torbox.send_nzb_file(
                b"dummy nzb", filename="test.nzb", explicit_key="dummy_key"
            )

        remaining = torbox.get_cooldown_remaining()
        assert 295.0 <= remaining <= 300.0


@pytest.mark.asyncio
async def test_get_usenet_downloads_ttl_cache():
    """Verify in-memory 20-second TTL cache on get_usenet_downloads."""
    dummy_data = [{"id": 123, "name": "Item 1", "download_state": "completed"}]
    response_200 = httpx.Response(
        status_code=200,
        json={"success": True, "data": dummy_data},
        request=httpx.Request("GET", "https://api.torbox.app/v1/api/usenet/mylist"),
    )

    with (
        patch("httpx.AsyncClient") as mock_client_cls,
        patch.object(torbox._poll_limiter, "wait", new_callable=AsyncMock),
    ):
        client_instance = AsyncMock()
        client_instance.get.return_value = response_200
        client_instance.__aenter__.return_value = client_instance
        mock_client_cls.return_value = client_instance

        # 1. First fetch hits API
        res1 = await torbox.get_usenet_downloads(explicit_key="dummy_key")
        assert res1 == dummy_data
        assert client_instance.get.call_count == 1

        # 2. Second fetch within TTL reuses cache
        res2 = await torbox.get_usenet_downloads(explicit_key="dummy_key")
        assert res2 == dummy_data
        assert client_instance.get.call_count == 1

        # 3. Third fetch with bypass_cache=True forces fresh call
        res3 = await torbox.get_usenet_downloads(
            bypass_cache=True, explicit_key="dummy_key"
        )
        assert res3 == dummy_data
        assert client_instance.get.call_count == 2


@pytest.mark.asyncio
async def test_rolling_window_rate_limiter_raises_fast():
    """Verify that RollingWindowRateLimiter raises RateLimitExceeded without sleeping when max_wait_seconds=0."""
    from app.core.rate_limiter import RateLimitExceeded, RollingWindowRateLimiter

    limiter = RollingWindowRateLimiter(
        limit=2, window_seconds=60.0, max_wait_seconds=0.0
    )

    # First 2 acquire calls succeed
    await limiter.wait()
    await limiter.wait()
    assert limiter.get_wait_time() > 0.0

    # 3rd acquire call fails fast with RateLimitExceeded
    with pytest.raises(RateLimitExceeded) as exc_info:
        await limiter.wait()
    assert exc_info.value.wait_time > 0.0


@pytest.mark.asyncio
async def test_auto_rate_limiter_exhaustion_halts_automation_not_manual():
    """Verify that when 50 automated uploads occur in 1 hour:
    1. get_cooldown_remaining(is_manual=False) > 0.
    2. get_cooldown_remaining(is_manual=True) == 0.0.
    3. Automated uploads raise DownloaderNetworkError immediately.
    4. Manual uploads still succeed.
    """
    import time

    now = time.monotonic()
    # Simulate 50 automated uploads sent over the last 30 minutes
    torbox._auto_send_limiter.timestamps = [now - 1800.0 + i for i in range(50)]

    # Automation cooldown is active
    auto_cd = torbox.get_cooldown_remaining(is_manual=False)
    assert auto_cd > 0.0

    # Manual cooldown is NOT active (reserved headroom available)
    manual_cd = torbox.get_cooldown_remaining(is_manual=True)
    assert manual_cd == 0.0

    # Background automation upload raises DownloaderNetworkError immediately
    with patch("httpx.AsyncClient") as mock_client:
        with pytest.raises(DownloaderNetworkError) as exc_info:
            await torbox.send_nzb_file(
                b"test", filename="auto.nzb", explicit_key="dummy_key", is_manual=False
            )
        assert "cooldown active" in str(exc_info.value).lower()
        mock_client.assert_not_called()

    # Manual upload proceeds to HTTP call without cooldown failure
    response_200 = httpx.Response(
        status_code=200,
        json={"success": True, "data": {"id": 999, "hash": "abc"}},
        request=httpx.Request(
            "POST", "https://api.torbox.app/v1/api/usenet/createusenetdownload"
        ),
    )
    with patch("httpx.AsyncClient") as mock_client_cls:
        client_instance = AsyncMock()
        client_instance.post.return_value = response_200
        client_instance.__aenter__.return_value = client_instance
        mock_client_cls.return_value = client_instance

        res = await torbox.send_nzb_file(
            b"test", filename="manual.nzb", explicit_key="dummy_key", is_manual=True
        )
        assert res.get("id") == 999


@pytest.mark.asyncio
async def test_mylist_429_activates_global_cooldown():
    """Verify that an HTTP 429 on get_usenet_downloads activates global circuit breaker cooldown."""
    response_429 = httpx.Response(
        status_code=429,
        headers={"Retry-After": "120"},
        json={"detail": "Rate limit exceeded"},
        request=httpx.Request("GET", "https://api.torbox.app/v1/api/usenet/mylist"),
    )

    with (
        patch("httpx.AsyncClient") as mock_client_cls,
        patch.object(torbox._poll_limiter, "wait", new_callable=AsyncMock),
    ):
        client_instance = AsyncMock()
        client_instance.get.return_value = response_429
        client_instance.__aenter__.return_value = client_instance
        mock_client_cls.return_value = client_instance

        res = await torbox.get_usenet_downloads(
            bypass_cache=True, explicit_key="dummy_key"
        )
        assert res == []
        remaining = torbox.get_cooldown_remaining()
        assert 115.0 <= remaining <= 120.0


@pytest.mark.asyncio
@pytest.mark.needs_downloader
async def test_auto_push_and_advance_buffer_halt_cleanly_on_torbox_429(
    db_session,
    default_search_preset,
):
    """When TorBox raises DownloaderNetworkError (429 rate limit) mid-season during Auto-Push or
    advance_season_buffer:
    1. execute_auto_push completes cleanly without raising an unhandled exception.
    2. Already-dispatched episodes remain DOWNLOADING and receive their notification.
    3. The season that hit the 429 is queued as a rate_limited DownloadHistory row (not failed).
    4. Subsequent episodes in the season are skipped without querying indexers.
    """
    from sqlalchemy import select

    from app.core.push_engine import execute_auto_push
    from app.db.models import (
        DownloadHistory,
        EpisodeStatus,
        Provider,
        SeasonStatus,
    )
    from tests.conftest import VALID_NZB_BYTES, make_series

    indexer = Provider(
        name="TestIndexer",
        type="newznab",
        category="indexer",
        is_active=True,
        priority=1,
        api_url="https://indexer.example.com",
        api_key="idx-key",
    )
    db_session.add(indexer)
    await db_session.flush()

    item, seasons, episodes = await make_series(
        db_session,
        title="Star Wars Rebels",
        seasons=2,
        episodes_per_season=3,
        preset_id=default_search_preset.id,
        allow_season_packs=False,
        prefer_season_packs=False,
        auto_advance_seasons=True,
    )
    s1, s2 = seasons
    s1.status = SeasonStatus.COMPLETED
    s2.status = SeasonStatus.SEARCHING
    s1_eps = [e for e in episodes if e.season_id == s1.id]
    s2_eps = sorted(
        [e for e in episodes if e.season_id == s2.id], key=lambda x: x.episode_number
    )
    for ep in s1_eps:
        ep.status = EpisodeStatus.COMPLETED
    for ep in s2_eps:
        ep.status = EpisodeStatus.SEARCHING
    await db_session.commit()

    queried_episodes: list[tuple[int | None, int | None]] = []

    async def fake_search_show(*_args, **kwargs):
        s_num = int(kwargs.get("season") or 1)
        ep_raw = kwargs.get("ep") or kwargs.get("episode")
        ep_num = int(ep_raw) if ep_raw is not None else None
        queried_episodes.append((s_num, ep_num))
        return [
            {
                "title": f"Star.Wars.Rebels.S{s_num:02d}E{int(ep_num or 1):02d}.1080p.BluRay.x264-TvR",
                "guid": f"guid-swr-s{s_num}e{ep_num}",
                "size_bytes": 1500 * 1024**2,
            }
        ]

    call_count = 0

    async def fake_send_nzb(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"id": "tb-s2e1", "hash": "hash-s2e1"}
        torbox.set_cooldown(1257.0)
        raise DownloaderNetworkError("TorBox rate limit exceeded (cooldown: 1257s)")

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(VALID_NZB_BYTES, "test.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file", side_effect=fake_send_nzb
        ),
    ):
        res = await execute_auto_push(
            db_session,
            item.id,
            {
                "season_ids": [s2.id],
                "allow_season_packs": False,
                "prefer_season_packs": False,
                "auto_advance_seasons": True,
            },
        )

    assert res["pushed"] is True
    assert res["dispatched_count"] == 1
    # S02E01 succeeded, S02E02 hit 429, and S02E03 was never queried on the indexer!
    assert queried_episodes == [(2, 1), (2, 2)]

    await db_session.refresh(s2_eps[0])
    await db_session.refresh(s2_eps[1])
    await db_session.refresh(s2_eps[2])
    assert s2_eps[0].status == EpisodeStatus.DOWNLOADING
    # Remaining episodes stay SEARCHING; the season is queued as a RATE_LIMITED push (ADR-094)
    assert s2_eps[1].status == EpisodeStatus.SEARCHING
    assert s2_eps[2].status == EpisodeStatus.SEARCHING

    h_rows = (
        (
            await db_session.execute(
                select(DownloadHistory).where(DownloadHistory.media_item_id == item.id)
            )
        )
        .scalars()
        .all()
    )
    assert not [h for h in h_rows if (h.status_detail or "").startswith("failed:")]
    queued = [h for h in h_rows if h.status_detail == "rate_limited"]
    assert len(queued) == 1
    assert queued[0].season_id == s2.id and queued[0].episode_id is None
    assert queued[0].is_dismissed is False


@pytest.mark.asyncio
@pytest.mark.needs_downloader
async def test_manual_grab_queues_rate_limited_on_torbox_rate_limit(
    db_session,
):
    """When TorBox raises DownloaderNetworkError during execute_manual_grab, it queues the release as RATE_LIMITED instead of failing (ADR-094)."""
    from sqlalchemy import select

    from app.core.push_engine import execute_manual_grab
    from app.db.models import DownloadHistory, MediaStatus
    from tests.conftest import VALID_NZB_BYTES, make_movie

    movie = await make_movie(db_session, title="Rogue One", year=2016)
    await db_session.commit()

    async def fake_send_nzb(*_args, **_kwargs):
        torbox.set_cooldown(600.0)
        raise DownloaderNetworkError("TorBox rate limit exceeded (cooldown: 600s)")

    with (
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(VALID_NZB_BYTES, "rogue.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file", side_effect=fake_send_nzb
        ),
    ):
        res = await execute_manual_grab(
            db_session,
            movie.id,
            {
                "guid": "guid-rogue-1",
                "title": "Rogue.One.2016.1080p.BluRay.x264",
                "size_bytes": 8 * 1024**3,
                "score": 3000.0,
            },
        )

    assert res["pushed"] is True
    assert res["queued"] is True
    assert res["status_code"] == 200
    await db_session.refresh(movie)
    assert movie.status == MediaStatus.RATE_LIMITED
    rows = (
        (
            await db_session.execute(
                select(DownloadHistory).where(DownloadHistory.media_item_id == movie.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].status_detail == "rate_limited"
    assert rows[0].push_mode == "manual"
    assert rows[0].nzb_guid == "guid-rogue-1"
