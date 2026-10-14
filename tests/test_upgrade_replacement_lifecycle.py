"""Tests for deferred old-release replacement when an upgrade download completes."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.core.transfer_poller import run_transfer_poller_tick
from app.db.models import BlacklistedRelease, EpisodeStatus
from tests.conftest import make_history, make_series

GB = 1024**3


async def _seed(db_session, *, upgrade=True, link_old=True):
    item, seasons, eps = await make_series(
        db_session,
        episodes_per_season=1,
        episode_statuses=[EpisodeStatus.DOWNLOADING],
    )
    season, ep = seasons[0], eps[0]
    old = await make_history(
        db_session,
        item=item,
        season=season,
        episode=ep,
        nzb_title="Show.S01E01.720p.HDTV.x264-GRP",
        score=1000.0,
        downloader_transfer_id="tb-old",
        status_detail="completed",
    )
    new = await make_history(
        db_session,
        item=item,
        season=season,
        episode=ep,
        nzb_title="Show.S01E01.1080p.WEB-DL.DDP5.1.H.264-GRP",
        nzb_guid="guid-new",
        score=2200.0,
        downloader_transfer_id="tb-new",
        status_detail="downloading",
        is_upgrade=upgrade,
        replaces_history_id=old.id if link_old else None,
        dispatched_at=datetime.now(timezone.utc),
        is_dismissed=False,
    )
    await db_session.commit()
    return old, new


def _snapshot(include_old=False):
    snap = [
        {
            "id": "tb-new",
            "name": "Show.S01E01.1080p.WEB-DL.DDP5.1.H.264-GRP",
            "download_state": "completed",
            "download_finished": True,
            "progress": 1.0,
            "download_speed": 0,
            "eta": 0,
            "files": [{"name": "Show.S01E01.1080p.mkv", "size": 3 * GB}],
        }
    ]
    if include_old:
        snap.append(
            {**snap[0], "id": "tb-old", "name": "Show.S01E01.720p.HDTV.x264-GRP"}
        )
    return snap


async def _tick(db_session, include_old=False):
    with patch(
        "app.core.transfer_poller._delete_and_purge_transfer",
        new_callable=AsyncMock,
    ) as purge:
        await run_transfer_poller_tick(
            db_session, pre_fetched_downloads=_snapshot(include_old)
        )
    await db_session.commit()
    return purge


@pytest.mark.asyncio
async def test_upgrade_completion_purges_and_replaces_old(db_session):
    old, new = await _seed(db_session)
    purge = await _tick(db_session)
    await db_session.refresh(old)
    await db_session.refresh(new)
    assert new.status_detail == "completed"
    purge.assert_awaited_once()
    assert purge.await_args.args[1] == "tb-old"
    assert old.status_detail == "replaced"
    assert old.is_dismissed is True
    assert old.notification_sent is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "upgrade,link_old", [(False, True), (True, False)], ids=["non-upgrade", "no-link"]
)
async def test_completion_without_replacement_leaves_others_untouched(
    db_session, upgrade, link_old
):
    old, new = await _seed(db_session, upgrade=upgrade, link_old=link_old)
    purge = await _tick(db_session, include_old=True)
    await db_session.refresh(old)
    await db_session.refresh(new)
    assert new.status_detail == "completed"
    purge.assert_not_awaited()
    assert old.status_detail == "completed"


@pytest.mark.asyncio
async def test_upgrade_completion_with_deleted_old_history_is_noop(db_session):
    old, new = await _seed(db_session)
    await db_session.delete(old)
    await db_session.commit()
    purge = await _tick(db_session)
    await db_session.refresh(new)
    assert new.status_detail == "completed"
    purge.assert_not_awaited()


FAKE_REASON = "Layer 3 Fake Detection: Downloaded video payload is unrealistically small (< threshold)"


async def _fail(db_session, new, ep_item):
    from app.core.transfer_poller import _handle_transfer_failure

    item, season, ep = ep_item
    with (
        patch(
            "app.core.transfer_poller._delete_and_purge_transfer",
            new_callable=AsyncMock,
        ),
        patch(
            "app.core.transfer_poller._auto_replace_failed_push",
            new_callable=AsyncMock,
            return_value=None,
        ) as auto,
    ):
        await _handle_transfer_failure(
            session=db_session,
            target=ep,
            history=new,
            item=item,
            season=season,
            episode=ep,
            tb_item=None,
            reason=FAKE_REASON,
            error_type="fake",
            log_msg=FAKE_REASON,
        )
    return auto


async def _ctx(db_session, new):
    from app.db.models import Episode, MediaItem, Season

    ep = await db_session.get(Episode, new.episode_id)
    season = await db_session.get(Season, new.season_id)
    item = await db_session.get(MediaItem, new.media_item_id)
    return item, season, ep


@pytest.mark.asyncio
async def test_failed_upgrade_reverts_to_old_release(db_session):
    old, new = await _seed(db_session)
    ctx = await _ctx(db_session, new)
    auto = await _fail(db_session, new, ctx)
    await db_session.refresh(old)
    await db_session.refresh(new)
    ep = ctx[2]
    assert ep.status == EpisodeStatus.COMPLETED
    assert ep.fail_count == 0
    assert old.status_detail == "completed"
    assert old.is_dismissed is True  # factory default, untouched
    assert new.status_detail == f"failed: {FAKE_REASON}"
    assert new.is_dismissed is True
    auto.assert_not_awaited()
    bl = (await db_session.execute(select(BlacklistedRelease))).scalars().all()
    assert [b.nzb_guid for b in bl] == ["guid-new"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["deleted", "replaced", "non-upgrade"])
async def test_failure_falls_through_to_normal_path(db_session, mode):
    old, new = await _seed(db_session, upgrade=mode != "non-upgrade")
    if mode == "deleted":
        await db_session.delete(old)
    elif mode == "replaced":
        old.status_detail = "replaced"
    await db_session.commit()
    ctx = await _ctx(db_session, new)
    auto = await _fail(db_session, new, ctx)
    auto.assert_awaited_once()
    assert ctx[2].fail_count == 1


@pytest.mark.asyncio
async def test_single_cancel_of_upgrade_reverts_to_completed(db_session):
    from app.core.transfer_poller import cancel_active_push

    old, new = await _seed(db_session)
    with patch(
        "app.core.transfer_poller._delete_and_purge_transfer",
        new_callable=AsyncMock,
    ):
        assert await cancel_active_push(db_session, new.id) is True
    await db_session.refresh(old)
    await db_session.refresh(new)
    _, _, ep = await _ctx(db_session, new)
    assert ep.status == EpisodeStatus.COMPLETED
    assert old.status_detail == "completed"
    assert new.status_detail == "canceled" and new.is_dismissed is True


@pytest.mark.asyncio
async def test_single_cancel_of_normal_transfer_reverts_to_searching(db_session):
    from app.core.transfer_poller import cancel_active_push

    _, new = await _seed(db_session, upgrade=False)
    with patch(
        "app.core.transfer_poller._delete_and_purge_transfer",
        new_callable=AsyncMock,
    ):
        await cancel_active_push(db_session, new.id)
    _, _, ep = await _ctx(db_session, new)
    assert ep.status == EpisodeStatus.SEARCHING


@pytest.mark.asyncio
async def test_global_abort_upgrade_completed_normal_searching(db_session):
    from app.core.transfer_poller import abort_all_active_transfers

    old, up = await _seed(db_session)
    _, normal = await _seed(db_session, upgrade=False)
    with patch(
        "app.core.transfer_poller._delete_and_purge_transfer",
        new_callable=AsyncMock,
    ):
        await abort_all_active_transfers(db_session)
    await db_session.commit()
    _, _, up_ep = await _ctx(db_session, up)
    _, _, normal_ep = await _ctx(db_session, normal)
    await db_session.refresh(old)
    assert up_ep.status == EpisodeStatus.COMPLETED
    assert normal_ep.status == EpisodeStatus.SEARCHING
    assert old.status_detail == "completed"


async def _evaluate_upgrade(db_session, *, candidate_score=2200.0):
    """Seed a COMPLETED in-window episode with an old release, run one hourly pass."""
    from datetime import timedelta

    from app.config import settings
    from app.core.air_push_engine import run_episode_upgrades
    from app.db.models import DownloadHistory

    air = datetime(2026, 1, 10, 20, 0, tzinfo=timezone.utc)
    item, seasons, eps = await make_series(
        db_session,
        episodes_per_season=1,
        episode_statuses=[EpisodeStatus.COMPLETED],
        auto_push_episodes=True,
    )
    ep = eps[0]
    ep.air_date = ep.first_pushed_at = air
    old = await make_history(
        db_session,
        item=item,
        season=seasons[0],
        episode=ep,
        nzb_title="Show.S01E01.720p.HDTV.x264-GRP",
        score=1000.0,
        downloader_transfer_id="tb-old",
        status_detail="completed",
        dispatched_at=air,
        is_dismissed=False,
    )
    await db_session.commit()

    async def _dispatch(session, cands, item, season, episode, **kw):
        hist = DownloadHistory(
            media_item_id=item.id,
            season_id=season.id,
            episode_id=episode.id,
            nzb_title=cands[0]["title"],
            nzb_guid="guid-new",
            downloader_transfer_id="tb-new",
            push_mode="auto",
            is_upgrade=kw["is_upgrade"],
            replaces_history_id=kw["replaces_history_id"],
            status_detail="downloading",
            dispatched_at=datetime.now(timezone.utc),
            is_dismissed=False,
        )
        session.add(hist)
        episode.status = EpisodeStatus.DOWNLOADING
        await session.commit()
        return hist

    cand = {
        "guid": "guid-new",
        "title": "Show.S01E01.1080p.WEB-DL.DDP5.1.H.264-GRP",
        "score": candidate_score,
        "is_fallback": False,
    }
    with (
        patch.object(settings, "dry_run", False),
        patch(
            "app.core.air_push_engine._query_show_across_indexers",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.core.air_push_engine._score_and_partition_candidates",
            new=AsyncMock(return_value={"all_valid": [cand], "had_candidates": True}),
        ),
        patch(
            "app.core.air_push_engine._dispatch_candidate_list_to_downloader",
            new=AsyncMock(side_effect=_dispatch),
        ),
    ):
        now = air + timedelta(hours=5)
        assert await run_episode_upgrades(db_session, now) == 1
    new = (
        await db_session.execute(
            select(DownloadHistory).where(DownloadHistory.is_upgrade.is_(True))
        )
    ).scalar_one()
    db_session.expire(ep, ["download_history"])  # poller uses its own session in prod
    return old, new, ep, now


@pytest.mark.asyncio
async def test_evaluation_then_completion_purges_old(db_session):
    old, new, ep, _ = await _evaluate_upgrade(db_session)
    assert new.replaces_history_id == old.id and old.status_detail == "completed"
    purge = await _tick(db_session, include_old=True)
    await db_session.refresh(old)
    await db_session.refresh(new)
    assert purge.await_args.args[1] == "tb-old"
    assert old.status_detail == "replaced" and new.status_detail == "completed"
    assert ep.upgrades_applied == 1


@pytest.mark.asyncio
async def test_evaluation_then_failure_reverts_and_old_is_next_baseline(db_session):
    from datetime import timedelta

    from app.core.air_push_engine import _evaluate_episode_upgrade

    old, new, ep, now = await _evaluate_upgrade(db_session)
    ctx = await _ctx(db_session, new)
    await _fail(db_session, new, ctx)
    await db_session.refresh(old)
    assert ep.status == EpisodeStatus.COMPLETED and old.status_detail == "completed"

    captured: dict = {}

    async def _partition(**kw):
        return {
            "all_valid": [
                {"title": "Show.S01E01.1080p.WEB-DL.x264-GRP", "score": 1500.0}
            ],
            "had_candidates": True,
        }

    with (
        patch(
            "app.core.air_push_engine._query_show_across_indexers",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.core.air_push_engine._score_and_partition_candidates",
            new=AsyncMock(side_effect=_partition),
        ),
        patch(
            "app.core.air_push_engine.is_candidate_valid_upgrade",
            side_effect=lambda *a: captured.setdefault("args", a) and (False, "x"),
            create=False,
        ),
    ):
        # Baseline is the old release (score 1000), not the failed upgrade row.
        await _evaluate_episode_upgrade(
            db_session, ctx[0].id, ep.id, now + timedelta(hours=1)
        )
    assert captured["args"][3] == 1000.0  # current_score: old release, not failed row
    assert ep.upgrades_applied == 1
