"""Provider-neutral downloader labels (m57-11, spec §4.7-4.8, case 12)."""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.transfer_poller import get_active_pushes, get_push_history_ledger
from app.db.models import MediaStatus
from tests.conftest import make_downloader_provider, make_history, make_movie


async def _seed(session: AsyncSession, dtype: str, **hist: object) -> None:
    await make_downloader_provider(session, dtype)
    movie = await make_movie(session, status=MediaStatus.COMPLETED)
    await make_history(
        session,
        item=movie,
        downloader_type=dtype,
        is_dismissed=False,
        completed_at=datetime.now(timezone.utc),
        **hist,
    )
    await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dtype", "name"),
    [
        ("torbox", "TorBox"),
        ("sabnzbd", "SABnzbd"),
        ("premiumize", "Premiumize"),
        ("bogus", "TorBox"),
    ],
)
async def test_rows_expose_downloader_keys(
    db_session: AsyncSession, dtype: str, name: str
) -> None:
    movie = await make_movie(db_session, status=MediaStatus.COMPLETED)
    await make_history(
        db_session,
        item=movie,
        downloader_type=dtype,
        downloader_transfer_id="x1",
        status_detail="completed",
        is_dismissed=False,
        completed_at=datetime.now(timezone.utc),
    )
    await db_session.commit()
    (active,) = await get_active_pushes(db_session)
    (ledger,) = await get_push_history_ledger(db_session)
    for row in (active, ledger):
        assert row["downloader_name"] == name
        assert row["downloader_type"] == ("torbox" if dtype == "bogus" else dtype)
        assert row["downloader_transfer_id"] == "x1"


@pytest.mark.asyncio
async def test_pages_render_active_downloader_name(
    async_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _seed(
        db_session,
        "premiumize",
        status_detail="completed",
        downloader_transfer_id="p1",
    )
    dash = (await async_client.get("/")).text
    assert "Delete from Premiumize" in dash
    settings_html = (await async_client.get("/settings")).text
    assert "Download Timeout" in settings_html
    assert "Stalled Transfer Timeout" not in settings_html


@pytest.mark.asyncio
async def test_push_modal_and_table_use_downloader_name(
    async_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _seed(
        db_session, "sabnzbd", status_detail="completed", downloader_transfer_id="s1"
    )
    table = await async_client.get("/api/pushes/active", headers={"HX-Request": "true"})
    assert "Delete from SABnzbd" in table.text
    assert "TorBox" not in table.text
