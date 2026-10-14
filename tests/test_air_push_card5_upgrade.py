from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core import transfer_poller
from app.db.models import EpisodeStatus
from tests.conftest import make_history, make_series


@pytest.fixture
def _no_torbox(monkeypatch: pytest.MonkeyPatch):
    from app.services.downloaders import torbox

    async def _dl(*a, **k):
        return [
            {"id": "tb-up", "download_state": "downloading", "progress": 0.2},
            {"id": "tb-norm", "download_state": "downloading", "progress": 0.2},
        ]

    monkeypatch.setattr(torbox, "get_usenet_downloads", _dl)


@pytest.mark.asyncio
async def test_active_pushes_html_shows_upgrade_pill_only_for_upgrades(
    async_client, db_session, _no_torbox
):
    item, _, eps = await make_series(
        db_session,
        episodes_per_season=2,
        episode_statuses=[EpisodeStatus.DOWNLOADING] * 2,
    )
    await make_history(
        db_session,
        item=item,
        episode=eps[0],
        downloader_transfer_id="tb-up",
        nzb_guid="g-up",
        nzb_title="Show.S01E01.UPG",
        status_detail="downloading",
        is_dismissed=False,
        is_upgrade=True,
    )
    await make_history(
        db_session,
        item=item,
        episode=eps[1],
        downloader_transfer_id="tb-norm",
        nzb_guid="g-norm",
        nzb_title="Show.S01E02.NORM",
        status_detail="downloading",
        is_dismissed=False,
    )
    await db_session.commit()

    resp = await async_client.get("/api/pushes/active", headers={"HX-Request": "true"})
    html = resp.text
    assert html.count(">UPGRADE</span>") == 1
    rows = html.split('data-push-row="true"')[1:]
    flagged = [r for r in rows if ">UPGRADE</span>" in r]
    assert len(flagged) == 1
    assert "Show.S01E01.UPG" in flagged[0]


@pytest.mark.asyncio
async def test_two_upgrade_completions_same_show_one_batch_notification(db_session):
    item, _, eps = await make_series(
        db_session,
        episodes_per_season=2,
        episode_statuses=[EpisodeStatus.DOWNLOADED] * 2,
    )
    for i, ep in enumerate(eps):
        await make_history(
            db_session,
            item=item,
            episode=ep,
            downloader_transfer_id=f"tb-{i}",
            nzb_guid=f"g-{i}",
            status_detail="completed",
            is_dismissed=False,
            notification_sent=False,
            is_upgrade=True,
        )
    await db_session.commit()

    with patch.object(
        transfer_poller.discord, "dispatch_batch_notification_event", new=AsyncMock()
    ) as dispatch:
        await transfer_poller._dispatch_settled_show_notifications(
            db_session, {item.id: item}, sh_max_retries=3
        )
    assert dispatch.await_count == 1
    kwargs = dispatch.await_args.kwargs
    assert len(kwargs["histories"]) == 2
    assert kwargs["reason"].startswith("⬆️ Upgraded: ")
