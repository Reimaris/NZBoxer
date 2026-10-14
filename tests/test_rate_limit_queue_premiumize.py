"""Premiumize fair-use results flow through the rate-limit queue (m57-09)."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.core.push_engine import execute_auto_push
from app.core.rate_limit_queue import (
    clear_all_rate_limit_cooldowns,
    count_rate_limited_pushes,
    is_rate_limit_error,
    resume_rate_limited_pushes,
    resume_time_label,
)
from app.db.models import DownloadHistory, MediaStatus
from app.services.downloaders import premiumize
from tests.conftest import VALID_NZB_BYTES, make_downloader_provider, make_movie

FAIR_USE = {"status": "error", "message": "Fair use limit reached"}
CREATE_OK = {"status": "success", "id": "AbC123", "name": "x.nzb", "type": "nzb"}


@pytest.fixture(autouse=True)
def pmz(monkeypatch):
    premiumize.clear_cooldown()
    state = {"body": FAIR_USE, "calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        return httpx.Response(200, json=state["body"])

    monkeypatch.setattr(
        premiumize,
        "_client_factory",
        lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=timeout
        ),
    )
    yield state
    premiumize.clear_cooldown()


def _search_patches():
    return (
        patch(
            "app.services.indexers.newznab.search_movie",
            new=AsyncMock(
                return_value=[
                    {
                        "title": "Test.Movie.2024.1080p.BluRay.x264-GRP",
                        "guid": "guid-tm",
                        "size_bytes": 8 * 1024**3,
                    }
                ]
            ),
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(VALID_NZB_BYTES, "t.nzb")),
        ),
    )


async def _live_rows(session, item):
    await session.commit()
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


def test_fair_use_text_is_rate_limit_error():
    assert is_rate_limit_error("Fair use limit reached")


def test_label_and_clear_include_premiumize():
    premiumize.set_cooldown(600)
    assert resume_time_label().startswith("resumes")
    clear_all_rate_limit_cooldowns()
    assert premiumize.get_cooldown_remaining() == 0
    assert resume_time_label() == "resuming shortly"


async def test_fair_use_queues_then_resume_succeeds(db_session, pmz):
    await make_downloader_provider(
        db_session, "premiumize", name="Premiumize", api_key="k"
    )
    item = await make_movie(db_session)
    await db_session.commit()

    s1, s2 = _search_patches()
    with s1, s2:
        await execute_auto_push(db_session, item.id, {})

    rows = await _live_rows(db_session, item)
    assert [r.status_detail for r in rows] == ["rate_limited"]
    await db_session.refresh(item)
    assert item.status == MediaStatus.RATE_LIMITED
    assert await count_rate_limited_pushes(db_session) == 1
    assert premiumize.get_cooldown_remaining() > 0

    # Cooldown still active -> nothing resumes, no further HTTP call.
    calls = pmz["calls"]
    assert await resume_rate_limited_pushes() == 0
    assert pmz["calls"] == calls

    premiumize.clear_cooldown()
    pmz["body"] = CREATE_OK
    s1, s2 = _search_patches()
    with s1, s2:
        assert await resume_rate_limited_pushes() == 1
    rows = await _live_rows(db_session, item)
    assert [(r.downloader_type, r.downloader_transfer_id) for r in rows] == [
        ("premiumize", "AbC123")
    ]
    assert await count_rate_limited_pushes(db_session) == 0
