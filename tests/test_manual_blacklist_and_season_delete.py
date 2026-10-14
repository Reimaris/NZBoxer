"""Tests for manual release blacklisting (Ticket 03) and season-level delete (Ticket 04)."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.push_engine import (
    _load_blacklisted_sets,
    _score_and_partition_candidates,
)
from app.db.models import (
    BlacklistedRelease,
    EpisodeStatus,
    MediaStatus,
    SeasonStatus,
)
from tests.conftest import make_history, make_movie, make_series


@pytest.mark.asyncio
async def test_sqlite_migration_blacklisted_releases_nullable_media_item_id(tmp_path):
    """Verify init_db() migrates legacy NOT NULL media_item_id on blacklisted_releases to NULL."""
    db_file = tmp_path / "legacy_blacklist.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    seed_engine = create_async_engine(db_url, echo=False)

    async with seed_engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE blacklisted_releases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
                    nzb_guid VARCHAR(512) NOT NULL,
                    nzb_title VARCHAR(512) NOT NULL,
                    reason VARCHAR(255) NOT NULL DEFAULT 'Stalled / Dead NZB',
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        await conn.execute(
            text(
                "INSERT INTO blacklisted_releases (media_item_id, nzb_guid, nzb_title, reason) "
                "VALUES (1, 'legacy-guid-1', 'Legacy.Movie.2024.1080p', 'Failed')"
            )
        )
    await seed_engine.dispose()

    import app.db.database as db_mod

    orig_engine = db_mod._engine
    orig_factory = db_mod._session_factory
    try:
        await db_mod.init_db(db_url)
        migrated_engine = db_mod.get_engine()
        async with migrated_engine.begin() as conn:
            res = await conn.execute(text("PRAGMA table_info(blacklisted_releases)"))
            cols = {row[1]: row for row in res.fetchall()}
            assert "media_item_id" in cols
            assert int(cols["media_item_id"][3] or 0) == 0  # notnull == 0

            # Existing row preserved
            rows = (
                await conn.execute(
                    text(
                        "SELECT media_item_id, nzb_guid, nzb_title FROM blacklisted_releases"
                    )
                )
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == 1
            assert rows[0][1] == "legacy-guid-1"

            # Can insert NULL media_item_id
            await conn.execute(
                text(
                    "INSERT INTO blacklisted_releases (media_item_id, nzb_guid, nzb_title, reason) "
                    "VALUES (NULL, 'global-guid-1', 'Global.Bad.Release.1080p', 'Manual')"
                )
            )
        await migrated_engine.dispose()
    finally:
        db_mod._engine = orig_engine
        db_mod._session_factory = orig_factory


@pytest.mark.asyncio
async def test_load_blacklisted_sets_scoped_and_global(db_session):
    """Verify _load_blacklisted_sets loads item-scoped + global rows for item, or global-only when None."""
    movie1 = await make_movie(db_session, title="Movie One", simkl_id=1001)
    movie2 = await make_movie(db_session, title="Movie Two", simkl_id=1002)

    db_session.add_all(
        [
            BlacklistedRelease(
                media_item_id=movie1.id,
                nzb_guid="guid-m1",
                nzb_title="Movie.One.Bad.1080p",
                reason="Manual",
            ),
            BlacklistedRelease(
                media_item_id=movie2.id,
                nzb_guid="guid-m2",
                nzb_title="Movie.Two.Bad.1080p",
                reason="Manual",
            ),
            BlacklistedRelease(
                media_item_id=None,
                nzb_guid="guid-global",
                nzb_title="Global.Bad.Release.2160p",
                reason="Manual",
            ),
        ]
    )
    await db_session.commit()

    guids_m1, titles_m1 = await _load_blacklisted_sets(db_session, movie1.id)
    assert "guid-m1" in guids_m1
    assert "guid-global" in guids_m1
    assert "guid-m2" not in guids_m1
    assert "Movie.One.Bad.1080p" in titles_m1
    assert "movie.one.bad.1080p" in titles_m1
    assert "Global.Bad.Release.2160p" in titles_m1

    guids_global, titles_global = await _load_blacklisted_sets(db_session, None)
    assert "guid-global" in guids_global
    assert "guid-m1" not in guids_global
    assert "Global.Bad.Release.2160p" in titles_global
    assert "global.bad.release.2160p" in titles_global


@pytest.mark.asyncio
async def test_manual_picker_and_adhoc_search_exclude_blacklisted_releases(
    async_client, db_session
):
    """Verify blacklisted releases are excluded from both Push Modal picker and Card 6 Ad-Hoc search."""
    movie = await make_movie(db_session, title="Dune Part Two", year=2024)
    db_session.add_all(
        [
            BlacklistedRelease(
                media_item_id=movie.id,
                nzb_guid="https://idx.example.com/get/bad-item",
                nzb_title="Dune.Part.Two.2024.BAD.1080p.WEB-DL",
                reason="Manually blacklisted from Manual Picker",
            ),
            BlacklistedRelease(
                media_item_id=None,
                nzb_guid="https://idx.example.com/get/bad-global",
                nzb_title="Dune.Part.Two.2024.GLOBALBAD.1080p.WEB-DL",
                reason="Manually blacklisted from Ad-Hoc Search",
            ),
        ]
    )
    await db_session.commit()

    guids, titles = await _load_blacklisted_sets(db_session, movie.id)
    eff = {
        "primary_language": "en",
        "fallback_language": None,
        "video_quality_mode": "best",
        "audio_quality_mode": "best",
        "custom_config": {},
    }
    raw_items = [
        {
            "title": "Dune.Part.Two.2024.BAD.1080p.WEB-DL",
            "guid": "https://idx.example.com/get/bad-item",
            "size": 5_000_000_000,
        },
        {
            "title": "dune.part.two.2024.globalbad.1080p.web-dl",
            "guid": "https://idx.example.com/get/other-guid-same-title",
            "size": 5_000_000_000,
        },
        {
            "title": "Dune.Part.Two.2024.GOOD.1080p.WEB-DL",
            "guid": "https://idx.example.com/get/good-item",
            "size": 6_000_000_000,
        },
    ]
    partitioned = await _score_and_partition_candidates(
        raw_results=raw_items,
        blacklisted_guids=guids,
        blacklisted_titles=titles,
        expected_title="Dune Part Two",
        expected_year=2024,
        expected_alt_title=None,
        expected_season=None,
        expected_episode=None,
        expected_season_title=None,
        runtime_minutes=150,
        effective_cfg=eff,
        media_type=movie.media_type.value,
    )
    all_titles = [c["title"] for c in partitioned["all_valid"]]
    assert "Dune.Part.Two.2024.GOOD.1080p.WEB-DL" in all_titles
    assert "Dune.Part.Two.2024.BAD.1080p.WEB-DL" not in all_titles
    assert "dune.part.two.2024.globalbad.1080p.web-dl" not in all_titles

    # Also test Card 6 POST /api/search/manual/releases filtering against global blacklist
    with patch(
        "app.services.indexers.newznab.search_raw",
        new_callable=AsyncMock,
        return_value=[
            {
                "title": "Dune.Part.Two.2024.GLOBALBAD.1080p.WEB-DL",
                "guid": "https://idx.example.com/get/bad-global",
                "link": "https://idx.example.com/get/bad-global",
                "size": 5_000_000_000,
            },
            {
                "title": "Dune.Part.Two.2024.GOOD.1080p.WEB-DL",
                "guid": "https://idx.example.com/get/good-item",
                "link": "https://idx.example.com/get/good-item",
                "size": 6_000_000_000,
            },
        ],
    ):
        resp = await async_client.post(
            "/api/search/manual/releases",
            data={
                "scope": "item",
                "kind": "any",
                "title": "Dune Part Two",
                "drawer_id": "drawer-tmdb-1",
            },
        )
        assert resp.status_code == 200
        assert "Dune.Part.Two.2024.GOOD.1080p.WEB-DL" in resp.text
        assert "Dune.Part.Two.2024.GLOBALBAD.1080p.WEB-DL" not in resp.text


@pytest.mark.asyncio
async def test_post_api_blacklist_item_scoped_global_and_idempotent(
    async_client, db_session
):
    """Verify POST /api/blacklist creates item-scoped or global entries and deduplicates."""
    movie = await make_movie(db_session, title="Interstellar")
    await db_session.commit()

    # 1. Item-scoped blacklist
    resp1 = await async_client.post(
        "/api/blacklist",
        json={
            "media_item_id": movie.id,
            "nzb_guid": "guid-inter-1",
            "nzb_title": "Interstellar.2014.1080p.BAD",
            "reason": "Manually blacklisted from Manual Picker",
        },
    )
    assert resp1.status_code in (200, 201)

    # 2. Duplicate call is idempotent
    resp2 = await async_client.post(
        "/api/blacklist",
        json={
            "media_item_id": movie.id,
            "nzb_guid": "guid-inter-1",
            "nzb_title": "Interstellar.2014.1080p.BAD",
            "reason": "Manually blacklisted from Manual Picker",
        },
    )
    assert resp2.status_code == 200

    rows = (
        (
            await db_session.execute(
                select(BlacklistedRelease).where(
                    BlacklistedRelease.media_item_id == movie.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].nzb_guid == "guid-inter-1"

    # 3. Global blacklist (media_item_id=None)
    resp3 = await async_client.post(
        "/api/blacklist",
        json={
            "media_item_id": None,
            "nzb_guid": "guid-global-adhoc",
            "nzb_title": "AdHoc.Release.2024.720p",
            "reason": "Manually blacklisted from Ad-Hoc Search",
        },
    )
    assert resp3.status_code in (200, 201)

    global_rows = (
        (
            await db_session.execute(
                select(BlacklistedRelease).where(
                    BlacklistedRelease.media_item_id.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(global_rows) == 1
    assert global_rows[0].nzb_title == "AdHoc.Release.2024.720p"

    # 4. Settings blacklist view renders "Global" for NULL media_item_id
    get_resp = await async_client.get("/api/blacklist")
    assert get_resp.status_code == 200
    assert "Global" in get_resp.text
    assert "AdHoc.Release.2024.720p" in get_resp.text


@pytest.mark.asyncio
async def test_post_history_blacklist_deletes_torbox_marks_deleted_and_resets_target(
    async_client, db_session
):
    """Verify POST /api/history/{id}/blacklist records BlacklistedRelease, purges TorBox, and resets target."""
    movie = await make_movie(
        db_session, title="The Matrix", status=MediaStatus.COMPLETED
    )
    hist = await make_history(
        db_session,
        item=movie,
        nzb_title="The.Matrix.1999.1080p.BluRay",
        nzb_guid="guid-matrix-1",
        status_detail="completed",
        downloader_transfer_id="tb-matrix-99",
    )
    await db_session.commit()

    with patch(
        "app.core.transfer_poller._delete_and_purge_transfer",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_del:
        resp = await async_client.post(f"/api/history/{hist.id}/blacklist")
        assert resp.status_code == 200
        mock_del.assert_awaited_once()
        assert mock_del.call_args[0][1] == "tb-matrix-99"

    await db_session.refresh(hist)
    await db_session.refresh(movie)
    assert hist.status_detail == "deleted"
    assert hist.is_dismissed is True
    assert movie.status == MediaStatus.SEARCHING

    bl = (
        await db_session.execute(
            select(BlacklistedRelease).where(
                BlacklistedRelease.nzb_guid == "guid-matrix-1"
            )
        )
    ).scalar_one_or_none()
    assert bl is not None
    assert bl.media_item_id == movie.id
    assert bl.reason == "Manually blacklisted from Push History"


@pytest.mark.asyncio
async def test_post_pushes_blacklist_active_and_failed_rows(async_client, db_session):
    """Verify POST /api/pushes/{id}/blacklist works for both active and failed/rate-limited pushes."""
    series, seasons, episodes = await make_series(
        db_session,
        title="Severance",
        seasons=1,
        episodes_per_season=2,
        status=MediaStatus.DOWNLOADING,
    )
    s1 = seasons[0]
    ep1, ep2 = episodes[0], episodes[1]
    ep1.status = EpisodeStatus.DOWNLOADING
    ep2.status = EpisodeStatus.FAILED
    s1.status = SeasonStatus.DOWNLOADING

    h_active = await make_history(
        db_session,
        item=series,
        season=s1,
        episode=ep1,
        nzb_title="Severance.S01E01.1080p.WEB-DL",
        nzb_guid="guid-sev-e01",
        status_detail="downloading",
        downloader_transfer_id="tb-sev-1",
        is_dismissed=False,
    )
    h_failed = await make_history(
        db_session,
        item=series,
        season=s1,
        episode=ep2,
        nzb_title="Severance.S01E02.1080p.WEB-DL",
        nzb_guid="guid-sev-e02",
        status_detail="failed",
        downloader_transfer_id=None,
        is_dismissed=False,
    )
    await db_session.commit()

    with patch(
        "app.core.transfer_poller._delete_and_purge_transfer",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_del:
        resp1 = await async_client.post(f"/api/pushes/{h_active.id}/blacklist")
        assert resp1.status_code == 200
        mock_del.assert_awaited_once()
        assert mock_del.call_args[0][1] == "tb-sev-1"

        resp2 = await async_client.post(f"/api/pushes/{h_failed.id}/blacklist")
        assert resp2.status_code == 200

    await db_session.refresh(h_active)
    await db_session.refresh(h_failed)
    await db_session.refresh(ep1)
    await db_session.refresh(ep2)
    await db_session.refresh(s1)
    await db_session.refresh(series)

    assert h_active.status_detail == "canceled"
    assert h_failed.is_dismissed is True
    assert ep1.status == EpisodeStatus.SEARCHING
    assert ep2.status == EpisodeStatus.SEARCHING
    assert s1.status == SeasonStatus.SEARCHING
    assert series.status == MediaStatus.SEARCHING

    bl_rows = (
        (
            await db_session.execute(
                select(BlacklistedRelease).where(
                    BlacklistedRelease.media_item_id == series.id
                )
            )
        )
        .scalars()
        .all()
    )
    guids = {r.nzb_guid for r in bl_rows}
    assert "guid-sev-e01" in guids
    assert "guid-sev-e02" in guids
    assert all(r.reason == "Manually blacklisted from Active Pushes" for r in bl_rows)


@pytest.mark.asyncio
async def test_season_level_delete_all_episodes_mixed_state(async_client, db_session):
    """Verify Season.has_deletable_transfers and season-level delete across mixed episode states."""
    series, seasons, episodes = await make_series(
        db_session,
        title="The Bear",
        seasons=1,
        episodes_per_season=5,
        status=MediaStatus.DOWNLOADING,
    )
    s1 = seasons[0]
    ep1, ep2, ep3, ep4, ep5 = episodes
    s1.status = SeasonStatus.SEARCHING
    ep1.status = EpisodeStatus.COMPLETED
    ep2.status = EpisodeStatus.DOWNLOADING
    ep3.status = EpisodeStatus.RATE_LIMITED
    ep4.status = EpisodeStatus.SEARCHING
    ep5.status = EpisodeStatus.FUTURE

    h_ep1 = await make_history(
        db_session,
        item=series,
        season=s1,
        episode=ep1,
        nzb_title="The.Bear.S01E01.1080p.WEB-DL",
        nzb_guid="guid-bear-e01",
        status_detail="completed",
        downloader_transfer_id="tb-bear-e01",
        is_dismissed=True,
    )
    h_ep2 = await make_history(
        db_session,
        item=series,
        season=s1,
        episode=ep2,
        nzb_title="The.Bear.S01E02.1080p.WEB-DL",
        nzb_guid="guid-bear-e02",
        status_detail="downloading",
        downloader_transfer_id="tb-bear-e02",
        is_dismissed=False,
    )
    h_ep3 = await make_history(
        db_session,
        item=series,
        season=s1,
        episode=ep3,
        nzb_title="The.Bear.S01E03.1080p.WEB-DL",
        nzb_guid="guid-bear-e03",
        status_detail="rate_limited",
        downloader_transfer_id=None,
        is_dismissed=False,
    )
    ep4.status = EpisodeStatus.FAILED
    ep4.fail_count = 3
    ep4.last_error = "Unpack failed"
    h_ep4_failed = await make_history(
        db_session,
        item=series,
        season=s1,
        episode=ep4,
        nzb_title="The.Bear.S01E04.1080p.WEB-DL",
        nzb_guid="guid-bear-e04",
        status_detail="failed: Unpack failed",
        downloader_transfer_id=None,
        is_dismissed=False,
    )
    h_ep1_replaced = await make_history(
        db_session,
        item=series,
        season=s1,
        episode=ep1,
        nzb_title="The.Bear.S01E01.OLD.1080p.WEB-DL",
        nzb_guid="guid-bear-e01-old",
        status_detail="replaced",
        downloader_transfer_id="tb-bear-e01-old",
        is_dismissed=True,
    )
    await db_session.commit()
    await db_session.refresh(s1)

    assert s1.has_deletable_transfers is True

    # Verify push modal renders the season-level Delete from TorBox button even when season.status == SEARCHING
    modal_resp = await async_client.get(f"/api/items/{series.id}/push-modal")
    assert modal_resp.status_code == 200
    assert (
        f"/api/items/{series.id}/delete-transfer?season_id={s1.id}" in modal_resp.text
    )

    with patch(
        "app.core.transfer_poller._delete_and_purge_transfer",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_del:
        del_resp = await async_client.post(
            f"/api/items/{series.id}/delete-transfer?season_id={s1.id}"
        )
        assert del_resp.status_code == 200
        deleted_ids = [call.args[1] for call in mock_del.await_args_list]
        assert set(deleted_ids) == {"tb-bear-e01", "tb-bear-e02"}

    for obj in (
        s1,
        ep1,
        ep2,
        ep3,
        ep4,
        ep5,
        h_ep1,
        h_ep2,
        h_ep3,
        h_ep4_failed,
        h_ep1_replaced,
        series,
    ):
        await db_session.refresh(obj)

    assert s1.status == SeasonStatus.SEARCHING
    assert ep1.status == EpisodeStatus.SEARCHING
    assert ep2.status == EpisodeStatus.SEARCHING
    assert ep3.status == EpisodeStatus.SEARCHING
    assert ep4.status == EpisodeStatus.SEARCHING
    assert ep4.fail_count == 0
    assert ep4.last_error is None
    assert ep5.status == EpisodeStatus.FUTURE
    assert series.status == MediaStatus.SEARCHING

    assert h_ep1.status_detail == "deleted"
    assert h_ep1.is_dismissed is True
    assert h_ep2.status_detail == "deleted"
    assert h_ep2.is_dismissed is True
    assert h_ep3.status_detail == "deleted"
    assert h_ep3.is_dismissed is True
    assert h_ep4_failed.status_detail == "deleted"
    assert h_ep4_failed.is_dismissed is True
    assert h_ep1_replaced.status_detail == "replaced"
    assert s1.has_deletable_transfers is False
