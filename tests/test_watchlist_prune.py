"""Source-aware pruning and dedupe source re-pointing tests (m56-06)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.automation import deduplicate_media_items
from app.db.models import (
    DownloadHistory,
    EpisodeStatus,
    MediaItem,
    MediaItemWatchlistSource,
    MediaStatus,
)
from app.services.watchlist_ingest import prune_inactive_media_items
from tests.conftest import (
    make_history,
    make_movie,
    make_series,
    make_source,
    make_watchlist_provider,
)


async def _ids(session) -> set[int]:
    return set((await session.execute(select(MediaItem.id))).scalars().all())


async def test_kept_while_any_source_active_pruned_when_all_inactive(db_session):
    simkl = await make_watchlist_provider(db_session, "simkl")
    al = await make_watchlist_provider(db_session, "anilist")
    kept = await make_movie(db_session, provider_id=simkl.id, tmdb_id=1, simkl_id=1)
    gone = await make_movie(db_session, provider_id=simkl.id, tmdb_id=2, simkl_id=2)
    orphan = await make_movie(db_session, provider_id=simkl.id, tmdb_id=3, simkl_id=3)
    await make_source(db_session, kept, simkl, "completed", "k1")
    await make_source(db_session, kept, al, "watching", "k2")
    await make_source(db_session, gone, simkl, "completed", "g1")
    await make_source(db_session, gone, al, "dropped", "g2")
    await db_session.commit()

    assert await prune_inactive_media_items(db_session) == 1
    await db_session.commit()
    assert await _ids(db_session) == {kept.id, orphan.id}  # zero-source item kept


@pytest.mark.parametrize(
    "ep_status", [EpisodeStatus.DOWNLOADING, EpisodeStatus.RATE_LIMITED]
)
async def test_prune_deferred_for_active_transfer(db_session, ep_status, caplog):
    simkl = await make_watchlist_provider(db_session, "simkl")
    item, _, _ = await make_series(
        db_session,
        provider_id=simkl.id,
        episodes_per_season=1,
        episode_statuses=[ep_status],
    )
    await make_source(db_session, item, simkl, "completed", "s1")
    await db_session.commit()

    with caplog.at_level("INFO"):
        assert await prune_inactive_media_items(db_session) == 0
    assert item.id in await _ids(db_session)
    assert "Deferring prune" in caplog.text


async def test_prune_preserves_download_history(db_session):
    simkl = await make_watchlist_provider(db_session, "simkl")
    item = await make_movie(
        db_session, provider_id=simkl.id, status=MediaStatus.COMPLETED
    )
    await make_source(db_session, item, simkl, "completed", "s1")
    hist = await make_history(db_session, item=item)
    await db_session.commit()
    hist_id = hist.id

    assert await prune_inactive_media_items(db_session) == 1
    await db_session.commit()
    db_session.expire_all()

    row = await db_session.get(DownloadHistory, hist_id)
    assert row is not None and row.media_item_id is None


async def test_dedupe_repoints_sources_and_drops_collisions(db_session):
    simkl = await make_watchlist_provider(db_session, "simkl")
    plex = await make_watchlist_provider(db_session, "plex")
    a = await make_movie(db_session, provider_id=simkl.id, tmdb_id=9, simkl_id=0)
    b = await make_movie(db_session, provider_id=plex.id, tmdb_id=9, simkl_id=0)
    await make_source(db_session, a, simkl, "watching", "s-a")
    await make_source(db_session, b, simkl, "watching", "s-b")
    await make_source(db_session, b, plex, "plantowatch", "p-b")
    await db_session.commit()
    a_id, b_id = a.id, b.id
    simkl_id, plex_id = simkl.id, plex.id

    assert await deduplicate_media_items(db_session) == 1
    await db_session.commit()
    db_session.expire_all()

    assert await _ids(db_session) == {min(a_id, b_id)}
    rows = (
        await db_session.execute(
            select(
                MediaItemWatchlistSource.media_item_id,
                MediaItemWatchlistSource.provider_id,
            )
        )
    ).all()
    assert {tuple(r) for r in rows} == {
        (min(a_id, b_id), simkl_id),
        (min(a_id, b_id), plex_id),
    }
