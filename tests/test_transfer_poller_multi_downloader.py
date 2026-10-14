"""Poller, Layer 3 and deletion across downloaders (m57-07, spec cases 5 & 11)."""

from datetime import datetime, timezone

import pytest

from app.core.transfer_poller import (
    manual_delete_transfer,
    run_transfer_poller_tick,
)
from app.db.models import MediaStatus, SystemSettings
from app.services.downloaders.base import (
    DownloaderFile,
    DownloaderNetworkError,
    DownloaderTransfer,
    TransferState,
)
from app.services.downloaders.premiumize import PremiumizeDownloader
from app.services.downloaders.sabnzbd import SabnzbdDownloader
from tests.conftest import make_downloader_provider, make_history, make_movie

SAB_FAIL = "Aborted, cannot be completed - https://sabnzbd.org/not-complete"


def _transfer(state, **kw):
    return DownloaderTransfer(
        transfer_id="nzo1",
        name="Test.Release.2024.1080p.WEB-DL.H264-GRP",
        state=state,
        progress_pct=kw.pop("progress_pct", 50.0),
        **kw,
    )


async def _seed(session, dtype, **hist):
    session.add(SystemSettings(id=1, download_timeout_minutes=60, sh_max_retries=3))
    await make_downloader_provider(session, dtype, api_url="http://dl/api")
    item = await make_movie(session, status=MediaStatus.DOWNLOADING)
    h = await make_history(
        session,
        item=item,
        downloader_type=dtype,
        downloader_transfer_id="nzo1",
        status_detail="downloading",
        is_dismissed=False,
        dispatched_at=datetime.now(timezone.utc),
        **hist,
    )
    await session.commit()
    return item, h


def _patch_sab(monkeypatch, transfers=None, exc=None, files=()):
    async def get_transfers(self):
        if exc:
            raise exc
        return transfers

    async def get_files(self, tid):
        return list(files)

    monkeypatch.setattr(SabnzbdDownloader, "get_transfers", get_transfers)
    monkeypatch.setattr(SabnzbdDownloader, "get_transfer_files", get_files)


async def test_sab_completed_passes_layer3_with_empty_file_list(
    db_session, monkeypatch
):
    item, h = await _seed(db_session, "sabnzbd")
    _patch_sab(monkeypatch, [_transfer(TransferState.COMPLETED)])
    await run_transfer_poller_tick(db_session)
    await db_session.refresh(h)
    await db_session.refresh(item)
    assert h.status_detail == "completed"
    assert item.status == MediaStatus.COMPLETED


async def test_sab_failed_propagates_fail_message_and_deletes(db_session, monkeypatch):
    item, h = await _seed(db_session, "sabnzbd")
    _patch_sab(monkeypatch, [_transfer(TransferState.FAILED, fail_message=SAB_FAIL)])
    deleted = []

    async def delete(self, tid, delete_files=True):
        deleted.append(tid)
        return True

    monkeypatch.setattr(SabnzbdDownloader, "delete_transfer", delete)
    await run_transfer_poller_tick(db_session)
    from sqlalchemy import select

    from app.db.models import BlacklistedRelease

    bl = (await db_session.execute(select(BlacklistedRelease))).scalars().one()
    assert bl.reason == SAB_FAIL
    assert deleted == ["nzo1"]


async def test_network_error_skips_type_and_keeps_rows(db_session, monkeypatch):
    item, h = await _seed(db_session, "sabnzbd")
    _patch_sab(monkeypatch, exc=DownloaderNetworkError("down"))
    await run_transfer_poller_tick(db_session)
    await db_session.refresh(h)
    assert h.status_detail == "downloading"


async def test_removed_provider_marks_downloader_removed(db_session, monkeypatch):
    item, h = await _seed(db_session, "sabnzbd")
    from sqlalchemy import delete

    from app.db.models import Provider

    await db_session.execute(delete(Provider).where(Provider.type == "sabnzbd"))
    await db_session.commit()
    await run_transfer_poller_tick(db_session)
    await db_session.refresh(h)
    assert h.status_detail == "downloader_removed"
    await run_transfer_poller_tick(db_session)  # never loops
    await db_session.refresh(h)
    assert h.status_detail == "downloader_removed"


@pytest.mark.parametrize(
    ("age_minutes", "files", "expected"),
    [
        (0, [], "unpacking"),  # grace: still pending
        (11, [], "failed"),  # past 10 min -> fake path
        (0, [DownloaderFile("Movie.mkv", 8 * 1024**3)], "completed"),
    ],
)
async def test_premiumize_empty_listing_grace(
    db_session, monkeypatch, age_minutes, files, expected
):
    from datetime import timedelta

    item, h = await _seed(db_session, "premiumize")
    if age_minutes:
        h.unpack_started_at = datetime.now(timezone.utc) - timedelta(
            minutes=age_minutes
        )
        await db_session.commit()

    async def get_transfers(self):
        return [_transfer(TransferState.COMPLETED, progress_pct=100.0)]

    async def get_files(self, tid):
        return files

    async def delete(self, tid, delete_files=True):
        return True

    monkeypatch.setattr(PremiumizeDownloader, "get_transfers", get_transfers)
    monkeypatch.setattr(PremiumizeDownloader, "get_transfer_files", get_files)
    monkeypatch.setattr(PremiumizeDownloader, "delete_transfer", delete)
    await run_transfer_poller_tick(db_session)
    await db_session.refresh(h)
    if expected == "failed":
        assert h.status_detail != "downloading"
        assert h.status_detail not in ("completed", "unpacking")
    else:
        assert h.status_detail == expected


async def test_delete_uses_row_downloader_type_not_active(db_session, monkeypatch):
    item, h = await _seed(db_session, "sabnzbd")
    h.status_detail = "completed"
    await make_downloader_provider(
        db_session, "torbox", name="tb", priority=0, is_active=True
    )
    await db_session.commit()
    sab_deleted, tb_deleted = [], []

    async def sab_delete(self, tid, delete_files=True):
        sab_deleted.append((tid, delete_files))
        return True

    async def tb_delete(*a, **k):
        tb_deleted.append(a)
        return {"success": True}

    monkeypatch.setattr(SabnzbdDownloader, "delete_transfer", sab_delete)
    monkeypatch.setattr(
        "app.services.downloaders.torbox.delete_usenet_download", tb_delete
    )
    await manual_delete_transfer(db_session, history_id=h.id)
    assert sab_deleted == [("nzo1", True)]
    assert tb_deleted == []
