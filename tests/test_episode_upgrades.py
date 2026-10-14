"""Integration tests for the time-bounded episode upgrade pipeline."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.core.air_push_engine import run_episode_upgrades
from app.core.push_engine import _dispatch_candidate_list_to_downloader
from app.db.models import DownloadHistory, EpisodeStatus
from app.services.downloaders import torbox
from app.services.indexers import newznab as newznab_client
from tests.conftest import VALID_NZB_BYTES, make_history, make_series

AIR = datetime(2026, 1, 10, 20, 0, tzinfo=timezone.utc)
NOW = AIR + timedelta(hours=5)
OLD_TITLE = "Show.S01E01.720p.HDTV.x264-GRP"
NEW_TITLE = "Show.S01E01.1080p.WEB-DL.DDP5.1.H.264-GRP"


@pytest.fixture(autouse=True)
def upgrade_settings(monkeypatch):
    monkeypatch.setattr(settings, "upgrade_window_hours", 48)
    monkeypatch.setattr(settings, "max_upgrade_checks", 3)
    monkeypatch.setattr(settings, "dry_run", False)


async def _setup(db_session, *, air=AIR, checks=0, first_pushed_at=False, **item_kw):
    item, season, eps = await make_series(
        db_session,
        episodes_per_season=1,
        episode_statuses=[EpisodeStatus.COMPLETED],
        auto_push_episodes=True,
        **item_kw,
    )
    eps[0].air_date = air
    eps[0].first_pushed_at = first_pushed_at if first_pushed_at is not False else air
    eps[0].upgrades_applied = checks
    await db_session.commit()
    old = await make_history(
        db_session,
        item=item,
        season=season[0] if isinstance(season, list) else season,
        episode=eps[0],
        nzb_title=OLD_TITLE,
        score=1000.0,
        downloader_transfer_id="111",
        is_dismissed=False,
    )
    await db_session.commit()
    return item, eps[0], old


def _cand(title=NEW_TITLE, score=2200.0):
    return {"guid": "g-new", "title": title, "score": score, "is_fallback": False}


class _Mocks:
    def __init__(self, candidates, dispatch_result="ok"):
        self.candidates = candidates
        self.dispatch_result = dispatch_result

    def __enter__(self):
        async def _dispatch(session, cands, item, season, episode, **kw):
            if self.dispatch_result is None:
                return None
            hist = DownloadHistory(
                media_item_id=item.id,
                season_id=season.id,
                episode_id=episode.id,
                nzb_title=cands[0]["title"],
                nzb_guid=cands[0]["guid"],
                downloader_transfer_id="222",
                push_mode=kw["push_mode"],
                is_upgrade=kw["is_upgrade"],
                replaces_history_id=kw.get("replaces_history_id"),
                status_detail="downloading",
            )
            session.add(hist)
            episode.status = EpisodeStatus.DOWNLOADING
            await session.commit()
            return hist

        self.dispatch = AsyncMock(side_effect=_dispatch)
        self.purge = AsyncMock()
        self._patches = [
            patch(
                "app.core.air_push_engine._query_show_across_indexers",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.core.air_push_engine._score_and_partition_candidates",
                new=AsyncMock(
                    return_value={"all_valid": self.candidates, "had_candidates": True}
                ),
            ),
            patch(
                "app.core.air_push_engine._dispatch_candidate_list_to_downloader",
                new=self.dispatch,
            ),
            patch(
                "app.core.transfer_poller._delete_and_purge_transfer",
                new=self.purge,
            ),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


async def _reload(db_session, ep, old):
    await db_session.refresh(ep)
    await db_session.refresh(old)


async def test_upgrade_dispatches_without_purging_old(db_session):
    _, ep, old = await _setup(db_session)
    with _Mocks([_cand()]) as m:
        assert await run_episode_upgrades(db_session, NOW) == 1
    await _reload(db_session, ep, old)
    kw = m.dispatch.await_args.kwargs
    assert kw["push_mode"] == "auto" and kw["is_upgrade"] is True
    assert kw["reset_fail_count"] is False and kw["replaces_history_id"] == old.id
    m.purge.assert_not_awaited()
    assert old.status_detail == "completed" and old.is_dismissed is False
    new = (
        (
            await db_session.execute(
                select(DownloadHistory).where(DownloadHistory.is_upgrade.is_(True))
            )
        )
        .scalars()
        .one()
    )
    assert new.downloader_transfer_id == "222" and new.replaces_history_id == old.id
    assert ep.upgrades_applied == 1 and ep.last_upgrade_checked_at is not None
    assert ep.status == EpisodeStatus.DOWNLOADING


async def test_no_better_candidate_searches_uncapped(db_session):
    _, ep, old = await _setup(db_session)
    with _Mocks([_cand(score=900.0)]) as m:
        for hours in range(5):  # more passes than max_upgrade_checks
            now = NOW + timedelta(hours=hours)
            assert await run_episode_upgrades(db_session, now) == 0
            await _reload(db_session, ep, old)
            assert ep.upgrades_applied == 0
            assert ep.last_upgrade_checked_at.replace(tzinfo=timezone.utc) == now
    m.dispatch.assert_not_awaited()
    m.purge.assert_not_awaited()
    assert old.status_detail == "completed" and old.is_dismissed is False


async def test_failed_dispatch_does_not_count(db_session):
    _, ep, old = await _setup(db_session)
    with _Mocks([_cand()], dispatch_result=None) as m:
        assert await run_episode_upgrades(db_session, NOW) == 0
    await _reload(db_session, ep, old)
    m.purge.assert_not_awaited()
    assert old.status_detail == "completed" and old.is_dismissed is False
    assert ep.status == EpisodeStatus.COMPLETED
    assert ep.upgrades_applied == 0


async def test_in_flight_upgrade_row_is_not_baseline(db_session):
    item, ep, old = await _setup(db_session)
    await make_history(
        db_session,
        item=item,
        episode=ep,
        nzb_title=NEW_TITLE,
        score=2200.0,
        downloader_transfer_id="222",
        is_dismissed=False,
        status_detail="downloading",
        is_upgrade=True,
    )
    await db_session.commit()
    with _Mocks([_cand(score=2100.0)]) as m:
        assert await run_episode_upgrades(db_session, NOW) == 1
    assert m.dispatch.await_args.kwargs["replaces_history_id"] == old.id


@pytest.mark.parametrize(
    ("setup_kw", "now"),
    [
        ({"checks": 3}, NOW),  # cap reached
        ({"first_pushed_at": None}, NOW),  # never pushed
        ({}, AIR + timedelta(hours=49)),  # outside window
        ({}, AIR - timedelta(hours=1)),  # before air
    ],
)
async def test_ineligible_episode_skipped(db_session, setup_kw, now):
    _, ep, old = await _setup(db_session, **setup_kw)
    checks = ep.upgrades_applied
    with _Mocks([_cand()]) as m:
        assert await run_episode_upgrades(db_session, now) == 0
    await db_session.refresh(ep)
    m.dispatch.assert_not_awaited()
    assert ep.upgrades_applied == checks


async def test_downloading_episode_skipped(db_session):
    _, ep, _ = await _setup(db_session)
    ep.status = EpisodeStatus.DOWNLOADING
    await db_session.commit()
    with _Mocks([_cand()]) as m:
        assert await run_episode_upgrades(db_session, NOW) == 0
    m.dispatch.assert_not_awaited()


@pytest.mark.parametrize(
    "item_kw", [{"watched_completed": True}, {"last_watched_order": 1}]
)
async def test_watched_episode_skipped(db_session, item_kw):
    _, ep, _ = await _setup(db_session, **item_kw)
    with _Mocks([_cand()]) as m:
        assert await run_episode_upgrades(db_session, NOW) == 0
    await db_session.refresh(ep)
    m.dispatch.assert_not_awaited()
    assert ep.upgrades_applied == 0


async def test_opted_out_and_dry_run_skipped(db_session, monkeypatch):
    item, ep, _ = await _setup(db_session)
    with _Mocks([_cand()]) as m:
        monkeypatch.setattr(settings, "dry_run", True)
        assert await run_episode_upgrades(db_session, NOW) == 0
        monkeypatch.setattr(settings, "dry_run", False)
        item.auto_push_episodes = False
        await db_session.commit()
        assert await run_episode_upgrades(db_session, NOW) == 0
    m.dispatch.assert_not_awaited()


async def test_rate_limited_does_not_stamp_evaluation(db_session):
    _, ep, _ = await _setup(db_session)
    with (
        _Mocks([_cand()]) as m,
        patch.object(torbox, "get_cooldown_remaining", return_value=60),
    ):
        assert await run_episode_upgrades(db_session, NOW) == 0
    await db_session.refresh(ep)
    m.dispatch.assert_not_awaited()
    assert ep.upgrades_applied == 0 and ep.last_upgrade_checked_at is None


async def test_dispatch_marks_history_is_upgrade(db_session):
    item, ep, _ = await _setup(db_session)
    season = (await db_session.get(type(ep), ep.id)).season_id
    from app.db.models import Season

    season_obj = await db_session.get(Season, season)
    with (
        patch.object(
            newznab_client,
            "fetch_nzb_bytes",
            new=AsyncMock(return_value=(VALID_NZB_BYTES, "x.nzb")),
        ),
        patch.object(
            torbox,
            "send_nzb_file",
            new=AsyncMock(return_value={"id": 333, "hash": "h"}),
        ),
        patch("app.core.transfer_poller.wake_transfer_poller"),
    ):
        hist = await _dispatch_candidate_list_to_downloader(
            db_session,
            [_cand()],
            item=item,
            season=season_obj,
            episode=ep,
            reset_fail_count=False,
            is_upgrade=True,
        )
    assert isinstance(hist, DownloadHistory) and hist.is_upgrade is True
