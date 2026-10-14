"""Split poller cadence (m57-08, spec case 9)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.core import transfer_poller as tp
from app.db.models import MediaStatus
from app.services.downloaders.base import DownloaderTransfer, TransferState
from app.services.downloaders.premiumize import PremiumizeDownloader
from app.services.downloaders.sabnzbd import SabnzbdDownloader
from tests.conftest import make_downloader_provider, make_history, make_movie


@pytest.fixture
def scheduler():
    sch = MagicMock()
    sch.get_job.return_value = MagicMock(next_run_time=datetime.now(timezone.utc))
    old = (tp.transfer_poller._scheduler, tp.transfer_poller.interval_seconds)
    tp.transfer_poller.set_scheduler(sch)
    tp.transfer_poller.interval_seconds = 20
    yield sch
    tp.transfer_poller._scheduler, tp.transfer_poller.interval_seconds = old


async def _seed(session, *types):
    for i, t in enumerate(types):
        await make_downloader_provider(session, t, name=f"{t}{i}", api_url="http://x")
        item = await make_movie(
            session, status=MediaStatus.DOWNLOADING, simkl_id=500 + i, title=f"M{i}"
        )
        await make_history(
            session,
            item=item,
            nzb_guid=f"g{i}",
            downloader_type=t,
            downloader_transfer_id=f"id{i}",
            status_detail="downloading",
            is_dismissed=False,
            dispatched_at=datetime.now(timezone.utc),
        )
    await session.commit()


@pytest.fixture(autouse=True)
def _no_transfers(monkeypatch):
    async def none(self):
        return [DownloaderTransfer("id0", "x", TransferState.DOWNLOADING, 1.0)]

    monkeypatch.setattr(SabnzbdDownloader, "get_transfers", none)
    monkeypatch.setattr(PremiumizeDownloader, "get_transfers", none)


@pytest.mark.parametrize(
    ("types", "expected_interval", "reschedules"),
    [
        (("sabnzbd",), 5, 1),
        (("sabnzbd", "premiumize"), 20, 0),
        (("premiumize",), 20, 0),
    ],
)
async def test_interval_by_active_types(
    db_session, scheduler, types, expected_interval, reschedules
):
    await _seed(db_session, *types)
    await tp.run_transfer_poller_tick(db_session)
    assert tp.transfer_poller.interval_seconds == expected_interval
    assert scheduler.reschedule_job.call_count == reschedules
    if reschedules:
        trig = scheduler.reschedule_job.call_args.kwargs["trigger"]
        assert trig.interval.total_seconds() == expected_interval


async def test_no_reschedule_when_unchanged_and_back_to_cloud(db_session, scheduler):
    await _seed(db_session, "sabnzbd")
    await tp.run_transfer_poller_tick(db_session)
    await tp.run_transfer_poller_tick(db_session)
    assert scheduler.reschedule_job.call_count == 1
    tp.transfer_poller.wake({"torbox"})
    assert tp.transfer_poller.interval_seconds == 20
    assert scheduler.reschedule_job.call_count == 2


async def test_zero_active_pauses_job(db_session, scheduler):
    await tp.run_transfer_poller_tick(db_session)
    scheduler.pause_job.assert_called_with("transfer_poller_job")
