"""Outage-aware push engine: queue, backoff, terminal failure, auth fail-fast (ADR-158..160)."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.core.outage import clear_outages
from app.core.push_engine import execute_auto_push
from app.core.rate_limit_queue import resume_rate_limited_pushes
from app.db.models import BlacklistedRelease, DownloadHistory, FailureLog, MediaStatus
from app.services.downloaders import torbox
from app.services.downloaders.base import DownloaderUploadResult, outage_upload_result
from app.services.indexers import newznab
from app.services.outage_classifier import OutageVerdict
from tests.conftest import VALID_NZB_BYTES, make_movie

INDEXER_URL = "https://indexer.example.com/api"
RESULT = [
    {
        "title": "Test.Movie.2024.1080p.BluRay.x264-GRP",
        "guid": "guid-tm",
        "size_bytes": 8 * 1024**3,
        "indexer_url": INDEXER_URL,
    }
]


def _client():
    return type("C", (), {"display_name": "TorBox", "provider_type": "torbox"})()


def _fetch_raising(kind: str, label: str):
    cls = newznab.IndexerAuthError if kind == "auth" else newznab.IndexerOutageError
    return AsyncMock(
        side_effect=cls("boom", INDEXER_URL, OutageVerdict(kind, label))  # type: ignore[arg-type]
    )


def _patches(fetch=None, upload=None):
    return (
        patch(
            "app.services.indexers.newznab.search_movie",
            new=AsyncMock(return_value=[dict(r) for r in RESULT]),
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=fetch or AsyncMock(return_value=(VALID_NZB_BYTES, "t.nzb")),
        ),
        patch(
            "app.core.push_engine.dispatch_nzb_to_active_downloader",
            new=upload
            or AsyncMock(
                return_value=(
                    _client(),
                    DownloaderUploadResult(
                        success=True, transfer_id="tb1", raw={"hash": "h"}
                    ),
                )
            ),
        ),
    )


async def _push(session, item, **kw):
    p1, p2, p3 = _patches(**kw)
    with p1, p2, p3:
        await execute_auto_push(session, item.id, {})
    await session.commit()


async def _live(session, item):
    return (
        (
            await session.execute(
                select(DownloadHistory)
                .where(
                    DownloadHistory.media_item_id == item.id,
                    DownloadHistory.is_dismissed.is_(False),
                )
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def test_indexer_outage_queues_then_backs_off_then_fails_terminally(db_session):
    item = await make_movie(db_session)
    expected = [600, 1200, 1800, 1800, 1800]
    for attempt, seconds in enumerate(expected, start=1):
        await _push(db_session, item, fetch=_fetch_raising("outage", "HTTP 503"))
        (row,) = await _live(db_session, item)
        assert row.status_detail == "paused: indexer outage (HTTP 503)"
        assert row.outage_attempts == attempt
        assert item.fail_count == 0
        assert newznab.get_indexer_cooldown_remaining(INDEXER_URL) == pytest.approx(
            seconds, abs=5
        )
        newznab.set_indexer_cooldown(INDEXER_URL, 0)
        clear_outages()
    await db_session.refresh(item)
    assert item.status == MediaStatus.RATE_LIMITED

    await _push(db_session, item, fetch=_fetch_raising("outage", "HTTP 503"))
    (row,) = await _live(db_session, item)
    assert row.status_detail == "failed: indexer outage (5 attempts failed)"
    assert row.failed_at is not None
    await db_session.refresh(item)
    assert item.status == MediaStatus.FAILED


async def test_downloader_outage_keeps_candidate_and_does_not_blacklist(db_session):
    item = await make_movie(db_session)
    client = _client()
    upload = AsyncMock(
        return_value=(
            client,
            outage_upload_result(OutageVerdict("outage", "HTTP 502"), "x"),
        )
    )
    await _push(db_session, item, upload=upload)
    (row,) = await _live(db_session, item)
    assert row.status_detail == "paused: downloader outage (HTTP 502)"
    assert item.fail_count == 0
    assert not (await db_session.execute(select(BlacklistedRelease))).scalars().all()


async def test_indexer_auth_error_fails_immediately_without_cooldown(db_session):
    item = await make_movie(db_session)
    await _push(db_session, item, fetch=_fetch_raising("auth", "HTTP 401"))
    (row,) = await _live(db_session, item)
    assert row.status_detail == "failed: indexer authentication error (HTTP 401)"
    assert row.outage_attempts == 0
    assert newznab.get_indexer_cooldown_remaining(INDEXER_URL) == 0
    logs = (await db_session.execute(select(FailureLog))).scalars().all()
    assert logs


async def test_resume_redispatches_after_outage_cleared(db_session):
    item = await make_movie(db_session)
    await _push(db_session, item, fetch=_fetch_raising("outage", "HTTP 503"))
    assert await resume_rate_limited_pushes() == 0  # still cooling down
    newznab.set_indexer_cooldown(INDEXER_URL, 0)
    clear_outages()
    p1, p2, p3 = _patches()
    with p1, p2, p3:
        assert await resume_rate_limited_pushes() == 1
    (row,) = await _live(db_session, item)
    assert row.downloader_transfer_id == "tb1"


async def test_429_flow_unchanged(db_session):
    item = await make_movie(db_session)
    torbox.set_cooldown(600.0)
    p1, p2, p3 = _patches()
    with p1, p2, p3:
        await execute_auto_push(db_session, item.id, {})
    await db_session.commit()
    (row,) = await _live(db_session, item)
    assert row.status_detail == "rate_limited"
    assert row.outage_attempts == 0
