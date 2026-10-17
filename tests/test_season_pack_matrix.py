"""
Unit and Integration Tests for Milestone m21:
Two-Checkbox Season Pack Acquisition Matrix (`allow_season_packs` & `prefer_season_packs`)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.push_engine import execute_auto_push, execute_manual_search
from app.core.transfer_poller import run_transfer_poller_tick
from app.db.models import (
    DownloadHistory,
    Episode,
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    MediaType,
    Provider,
    ProviderCategory,
    SearchPreset,
    Season,
    SeasonStatus,
    SystemSettings,
)
from app.services.preset_service import (
    create_preset,
    normalize_season_pack_flags,
    save_inline_preset,
    update_item_sticky_search_config,
    update_preset,
)

FAKE_VALID_NZB = b"""<?xml version="1.0" encoding="UTF-8"?>
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
  <file poster="test" date="1700000000" subject="Test.Release.1080p.WEB-DL.mkv (1/1)">
    <groups><group>alt.binaries.test</group></groups>
    <segments><segment bytes="500000000" number="1">seg1@test</segment></segments>
  </file>
</nzb>
"""

GB = 1024 * 1024 * 1024


async def _seed_show_with_3_episodes(
    session: AsyncSession,
    allow_season_packs: bool = False,
    prefer_season_packs: bool = False,
) -> tuple[MediaItem, Season, list[Episode]]:
    """Create a watchlist provider, indexer, show, Season 1, and 3 episodes (E01, E02, E03)."""
    provider = Provider(
        name="Simkl",
        type="simkl",
        category=ProviderCategory.WATCHLIST.value,
        is_active=True,
    )
    indexer = Provider(
        name="PrimaryIndexer",
        type="newznab",
        category=ProviderCategory.INDEXER.value,
        api_url="https://idx.example/api",
        api_key="idx-key",
        priority=1,
        is_active=True,
    )
    session.add_all([provider, indexer])
    await session.flush()

    show = MediaItem(
        provider_id=provider.id,
        simkl_id=88001,
        tvdb_id=88001,
        title="Andor",
        year=2022,
        media_type=MediaType.SHOW,
        status=MediaStatus.SEARCHING,
        allow_season_packs=allow_season_packs,
        prefer_season_packs=prefer_season_packs,
    )
    session.add(show)
    await session.flush()

    s1 = Season(
        media_item_id=show.id,
        season_number=1,
        watch_order=1,
        type_number=1,
        entry_type="season",
        title="Season 1",
        monitored=True,
        episode_count=3,
        air_date=datetime(2022, 9, 21, tzinfo=timezone.utc),
        status=SeasonStatus.SEARCHING,
    )
    session.add(s1)
    await session.flush()

    eps: list[Episode] = []
    for ep_num in (1, 2, 3):
        ep = Episode(
            season_id=s1.id,
            episode_number=ep_num,
            monitored=True,
            air_date=datetime(2022, 9, 21, tzinfo=timezone.utc),
            status=EpisodeStatus.SEARCHING,
        )
        session.add(ep)
        eps.append(ep)

    await session.commit()
    return show, s1, eps


@pytest.mark.asyncio
async def test_defaults_and_dependent_checkbox_invariant(
    db_session: AsyncSession,
) -> None:
    """
    Verify:
    1. SearchPreset and MediaItem default both `allow_season_packs` and `prefer_season_packs` to False.
    2. `normalize_season_pack_flags`, `create_preset`, `update_preset`, `save_inline_preset`,
       and `update_item_sticky_search_config` enforce `not allow_season_packs => not prefer_season_packs`.
    """
    assert normalize_season_pack_flags(None, None) == (False, False)
    assert normalize_season_pack_flags(False, True) == (False, False)
    assert normalize_season_pack_flags(True, False) == (True, False)
    assert normalize_season_pack_flags(True, True) == (True, True)

    provider = Provider(name="Simkl", type="simkl", category="watchlist")
    db_session.add(provider)
    await db_session.flush()

    item = MediaItem(
        provider_id=provider.id,
        simkl_id=101,
        title="Default Show",
        media_type=MediaType.SHOW,
        status=MediaStatus.SEARCHING,
    )
    preset = SearchPreset(name="Test Default Off")
    db_session.add_all([item, preset])
    await db_session.commit()

    assert item.allow_season_packs is False
    assert item.prefer_season_packs is False
    assert preset.allow_season_packs is False
    assert preset.prefer_season_packs is False
    assert preset.to_dict()["allow_season_packs"] is False
    assert preset.to_dict()["prefer_season_packs"] is False

    # Attempt to create a preset with allow=False, prefer=True -> prefer must be forced to False
    p_invalid = await create_preset(
        db_session,
        {
            "name": "Invalid Combo",
            "allow_season_packs": False,
            "prefer_season_packs": True,
        },
    )
    assert p_invalid.allow_season_packs is False
    assert p_invalid.prefer_season_packs is False

    # Update preset to allow=True, prefer=True -> both True
    p_valid = await update_preset(
        db_session,
        p_invalid.id,
        {"allow_season_packs": True, "prefer_season_packs": True},
    )
    assert p_valid is not None
    assert p_valid.allow_season_packs is True
    assert p_valid.prefer_season_packs is True

    # Turning allow_season_packs off via update_preset automatically unchecks prefer_season_packs
    p_off = await update_preset(
        db_session,
        p_invalid.id,
        {"allow_season_packs": False},
    )
    assert p_off is not None
    assert p_off.allow_season_packs is False
    assert p_off.prefer_season_packs is False

    # Save inline preset with allow=False, prefer=True -> item and preset both normalize to False
    p_inline = await save_inline_preset(
        db_session,
        {
            "name": "Inline Combo",
            "media_item_id": item.id,
            "allow_season_packs": False,
            "prefer_season_packs": True,
        },
    )
    await db_session.refresh(item)
    assert p_inline.allow_season_packs is False
    assert p_inline.prefer_season_packs is False
    assert item.allow_season_packs is False
    assert item.prefer_season_packs is False

    # Sticky item config with allow=True, prefer=False -> Score Decides
    await update_item_sticky_search_config(
        db_session,
        item.id,
        {"allow_season_packs": True, "prefer_season_packs": False},
    )
    await db_session.refresh(item)
    assert item.allow_season_packs is True
    assert item.prefer_season_packs is False


@pytest.mark.asyncio
async def test_state_1_episodes_only_skips_packs_in_auto_push_and_manual_search(
    db_session: AsyncSession,
) -> None:
    """
    When `allow_season_packs=False` (default):
    1. `execute_auto_push` never queries season packs (`ep=None`) and pushes only individual episodes.
    2. `execute_manual_search` skips season pack queries for TV seasons (`pack_or_movie_releases` empty)
       while still returning `episode_accordions`.
    """
    show, s1, eps = await _seed_show_with_3_episodes(
        db_session, allow_season_packs=False, prefer_season_packs=False
    )

    queried_eps: list[str | None] = []

    async def fake_search_show(
        season=None, ep=None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        queried_eps.append(ep)
        if ep is None:
            return [
                {
                    "title": "Andor.S01.2160p.UHD.BluRay.REMUX.HEVC.TrueHD.7.1.Atmos-GRP",
                    "size": 60 * GB,
                    "guid": "guid-pack-should-not-be-queried",
                }
            ]
        ep_num = int(str(ep))
        return [
            {
                "title": f"Andor.S01E{ep_num:02d}.1080p.WEB-DL.DDP5.1.H.264-GRP",
                "size": int(1.5 * GB),
                "guid": f"guid-ep-{ep_num}",
            }
        ]

    # 1. Manual search with allow_season_packs=False
    with patch(
        "app.services.indexers.newznab.search_show", side_effect=fake_search_show
    ):
        manual_res = await execute_manual_search(
            db_session,
            show.id,
            {
                "season_ids": [s1.id],
                "allow_season_packs": False,
                "prefer_season_packs": False,
            },
        )

    assert None not in queried_eps, (
        "Season pack query (ep=None) must be skipped when allow_season_packs=False"
    )
    assert manual_res["pack_or_movie_releases"]["releases"] == []
    assert len(manual_res["episode_accordions"]) == 3

    # 2. Auto-Push with allow_season_packs=False
    queried_eps.clear()
    tb_calls = 0

    async def fake_send_nzb(*args: Any, **kwargs: Any) -> dict[str, str]:
        nonlocal tb_calls
        tb_calls += 1
        return {"id": f"tb-ep-{tb_calls}", "hash": f"hash-ep-{tb_calls}"}

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(FAKE_VALID_NZB, "ep.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file", side_effect=fake_send_nzb
        ),
    ):
        auto_res = await execute_auto_push(
            db_session,
            show.id,
            {
                "season_ids": [s1.id],
                "allow_season_packs": False,
                "prefer_season_packs": False,
            },
        )

    assert None not in queried_eps
    assert auto_res["pushed"] is True
    assert auto_res["dispatched_count"] == 3
    assert auto_res["guids"] == ["guid-ep-1", "guid-ep-2", "guid-ep-3"]


@pytest.mark.asyncio
async def test_state_2_score_decides_pack_wins_when_pack_score_higher_or_equal(
    db_session: AsyncSession,
) -> None:
    """
    In Score Decides mode (`allow_season_packs=True, prefer_season_packs=False`):
    When `best_pack` (2160p Remux) scores >= `E01` (1080p WEB-DL), Smewlet probes only `E01`
    (does not query E02 or E03) and dispatches ONLY the Season Pack to TorBox.
    """
    show, s1, _ = await _seed_show_with_3_episodes(
        db_session, allow_season_packs=True, prefer_season_packs=False
    )

    queried_eps: list[str | None] = []

    async def fake_search_show(
        season=None, ep=None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        queried_eps.append(ep)
        if ep is None:
            return [
                {
                    "title": "Andor.S01.2160p.UHD.BluRay.REMUX.HEVC.TrueHD.7.1.Atmos-GRP",
                    "size": 45 * GB,
                    "guid": "guid-pack-4k",
                }
            ]
        return [
            {
                "title": f"Andor.S01E{int(ep):02d}.1080p.WEB-DL.AAC2.0.H.264-GRP",
                "size": int(1.2 * GB),
                "guid": f"guid-ep-{int(ep)}",
            }
        ]

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(FAKE_VALID_NZB, "pack.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new=AsyncMock(return_value={"id": "tb-pack-1", "hash": "hash-pack-1"}),
        ) as mock_send,
    ):
        res = await execute_auto_push(
            db_session,
            show.id,
            {
                "season_ids": [s1.id],
                "allow_season_packs": True,
                "prefer_season_packs": False,
            },
        )

    # Queried pack (None) and probed E01 ('1'), stopped before E02 ('2') and E03 ('3')!
    assert queried_eps == [None, "1"]
    assert res["pushed"] is True
    assert res["dispatched_count"] == 1
    assert res["guids"] == ["guid-pack-4k"]
    assert mock_send.await_count == 1


@pytest.mark.asyncio
async def test_state_2_score_decides_episodes_win_when_all_present_and_higher_score(
    db_session: AsyncSession,
) -> None:
    """
    In Score Decides mode (`allow_season_packs=True, prefer_season_packs=False`):
    When `E01..E03` are 2160p Remux and `best_pack` is 720p HDTV, all 3 episodes are
    resolved in memory and dispatched to TorBox (0 Season Pack uploads).
    """
    show, s1, _ = await _seed_show_with_3_episodes(
        db_session, allow_season_packs=True, prefer_season_packs=False
    )

    async def fake_search_show(
        season=None, ep=None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        if ep is None:
            return [
                {
                    "title": "Andor.S01.720p.HDTV.x264-LOW",
                    "size": 4 * GB,
                    "guid": "guid-pack-720p",
                }
            ]
        ep_num = int(str(ep))
        return [
            {
                "title": f"Andor.S01E{ep_num:02d}.2160p.UHD.BluRay.REMUX.HEVC.TrueHD.7.1.Atmos-GRP",
                "size": 8 * GB,
                "guid": f"guid-ep-4k-{ep_num}",
            }
        ]

    tb_seq = 0

    async def fake_send_nzb(*args: Any, **kwargs: Any) -> dict[str, str]:
        nonlocal tb_seq
        tb_seq += 1
        return {"id": f"tb-ep-{tb_seq}", "hash": f"hash-ep-{tb_seq}"}

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(FAKE_VALID_NZB, "ep.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file", side_effect=fake_send_nzb
        ) as mock_send,
    ):
        res = await execute_auto_push(
            db_session,
            show.id,
            {
                "season_ids": [s1.id],
                "allow_season_packs": True,
                "prefer_season_packs": False,
            },
        )

    assert res["pushed"] is True
    assert res["dispatched_count"] == 3
    assert res["guids"] == ["guid-ep-4k-1", "guid-ep-4k-2", "guid-ep-4k-3"]
    assert mock_send.await_count == 3


@pytest.mark.asyncio
async def test_state_2_score_decides_missing_episode_falls_back_to_pack_with_zero_episode_uploads(
    db_session: AsyncSession,
) -> None:
    """
    CRITICAL PRE-FLIGHT GUARANTEE:
    In Score Decides mode (`allow_season_packs=True, prefer_season_packs=False`),
    if `E01` (4K Remux) beats `best_pack` (1080p WEB-DL), `E02` (4K Remux) exists,
    but `E03` is missing (`[]`), Smewlet aborts the episode route BEFORE uploading
    anything to TorBox and pushes ONLY the single Season Pack (`1` TorBox upload total, `0` episode uploads).
    """
    show, s1, eps = await _seed_show_with_3_episodes(
        db_session, allow_season_packs=True, prefer_season_packs=False
    )

    async def fake_search_show(
        season=None, ep=None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        if ep is None:
            return [
                {
                    "title": "Andor.S01.1080p.WEB-DL.DDP5.1.H.264-PACK",
                    "size": 12 * GB,
                    "guid": "guid-pack-1080p",
                }
            ]
        ep_num = int(str(ep))
        if ep_num in (1, 2):
            return [
                {
                    "title": f"Andor.S01E{ep_num:02d}.2160p.UHD.BluRay.REMUX.HEVC.TrueHD.7.1.Atmos-GRP",
                    "size": 8 * GB,
                    "guid": f"guid-ep-4k-{ep_num}",
                }
            ]
        # Episode 3 is missing!
        return []

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(FAKE_VALID_NZB, "pack.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new=AsyncMock(
                return_value={"id": "tb-pack-only", "hash": "hash-pack-only"}
            ),
        ) as mock_send,
    ):
        res = await execute_auto_push(
            db_session,
            show.id,
            {
                "season_ids": [s1.id],
                "allow_season_packs": True,
                "prefer_season_packs": False,
            },
        )

    # Strictly 1 upload to TorBox: the Season Pack! Zero episode uploads!
    assert mock_send.await_count == 1
    assert res["pushed"] is True
    assert res["dispatched_count"] == 1
    assert res["guids"] == ["guid-pack-1080p"]

    hist_rows = (
        (
            await db_session.execute(
                select(DownloadHistory).where(DownloadHistory.media_item_id == show.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(hist_rows) == 1
    assert hist_rows[0].season_id == s1.id
    assert hist_rows[0].episode_id is None


@pytest.mark.asyncio
async def test_state_2_score_decides_average_score_drop_falls_back_to_pack(
    db_session: AsyncSession,
) -> None:
    """
    In Score Decides mode (`allow_season_packs=True, prefer_season_packs=False`),
    if `E01` (2160p Remux) beats `best_pack` (1080p BluRay), but `E02` and `E03` are only
    480p SD (pulling the season's average episode score below `best_pack`), Smewlet
    pushes ONLY `best_pack` to TorBox (`0` episode uploads).
    """
    show, s1, _ = await _seed_show_with_3_episodes(
        db_session, allow_season_packs=True, prefer_season_packs=False
    )

    async def fake_search_show(
        season=None, ep=None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        if ep is None:
            return [
                {
                    "title": "Andor.S01.1080p.BluRay.x265.DTS-HD.MA.5.1-PACK",
                    "size": 24 * GB,
                    "guid": "guid-pack-bluray",
                }
            ]
        ep_num = int(str(ep))
        if ep_num == 1:
            return [
                {
                    "title": "Andor.S01E01.2160p.UHD.BluRay.REMUX.HEVC.TrueHD.7.1.Atmos-GRP",
                    "size": 10 * GB,
                    "guid": "guid-ep1-4k",
                }
            ]
        return [
            {
                "title": f"Andor.S01E{ep_num:02d}.480p.HDTV.x264.AAC-LOW",
                "size": int(0.3 * GB),
                "guid": f"guid-ep{ep_num}-480p",
            }
        ]

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(FAKE_VALID_NZB, "pack.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new=AsyncMock(return_value={"id": "tb-pack-avg", "hash": "hash-pack-avg"}),
        ) as mock_send,
    ):
        res = await execute_auto_push(
            db_session,
            show.id,
            {
                "season_ids": [s1.id],
                "allow_season_packs": True,
                "prefer_season_packs": False,
            },
        )

    assert mock_send.await_count == 1
    assert res["pushed"] is True
    assert res["dispatched_count"] == 1
    assert res["guids"] == ["guid-pack-bluray"]


@pytest.mark.asyncio
async def test_state_2_score_decides_skips_pack_when_season_partially_downloaded(
    db_session: AsyncSession,
) -> None:
    """
    When `E01` is already `COMPLETED` and `E02..E03` are `SEARCHING`:
    - In `Score Decides` (`allow=True, prefer=False`), Smewlet skips the Season Pack query
      and pushes only `E02` and `E03`.
    - In `Prefer Season Pack` (`allow=True, prefer=True`), Smewlet still grabs the Season Pack.
    """
    show, s1, eps = await _seed_show_with_3_episodes(
        db_session, allow_season_packs=True, prefer_season_packs=False
    )
    eps[0].status = EpisodeStatus.COMPLETED
    await db_session.commit()

    queried_eps: list[str | None] = []

    async def fake_search_show(
        season=None, ep=None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        queried_eps.append(ep)
        if ep is None:
            return [
                {
                    "title": "Andor.S01.2160p.WEB-DL.H265-PACK",
                    "size": 25 * GB,
                    "guid": "guid-pack-partial",
                }
            ]
        ep_num = int(str(ep))
        return [
            {
                "title": f"Andor.S01E{ep_num:02d}.1080p.WEB-DL.H264-GRP",
                "size": int(1.5 * GB),
                "guid": f"guid-ep-{ep_num}",
            }
        ]

    tb_seq = 0

    async def fake_send_nzb(*args: Any, **kwargs: Any) -> dict[str, str]:
        nonlocal tb_seq
        tb_seq += 1
        return {"id": f"tb-part-{tb_seq}", "hash": f"hash-part-{tb_seq}"}

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(FAKE_VALID_NZB, "ep.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file", side_effect=fake_send_nzb
        ),
    ):
        res = await execute_auto_push(
            db_session,
            show.id,
            {
                "season_ids": [s1.id],
                "allow_season_packs": True,
                "prefer_season_packs": False,
            },
        )

    assert None not in queried_eps
    assert res["pushed"] is True
    assert res["dispatched_count"] == 2
    assert res["guids"] == ["guid-ep-2", "guid-ep-3"]


@pytest.mark.asyncio
async def test_state_3_prefer_season_pack_always_grabs_pack_regardless_of_score(
    db_session: AsyncSession,
) -> None:
    """
    In Prefer Season Pack mode (`allow_season_packs=True, prefer_season_packs=True`):
    Even if `best_pack` is 720p HDTV and `E01` is 2160p Remux, Smewlet immediately grabs
    `best_pack` without probing `E01`.
    """
    show, s1, _ = await _seed_show_with_3_episodes(
        db_session, allow_season_packs=True, prefer_season_packs=True
    )

    queried_eps: list[str | None] = []

    async def fake_search_show(
        season=None, ep=None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        queried_eps.append(ep)
        if ep is None:
            return [
                {
                    "title": "Andor.S01.720p.HDTV.x264-PACK",
                    "size": 4 * GB,
                    "guid": "guid-pack-720p-preferred",
                }
            ]
        return [
            {
                "title": "Andor.S01E01.2160p.UHD.BluRay.REMUX.HEVC.TrueHD.7.1.Atmos-GRP",
                "size": 10 * GB,
                "guid": "guid-ep1-4k",
            }
        ]

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(FAKE_VALID_NZB, "pack.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new=AsyncMock(return_value={"id": "tb-pref-1", "hash": "hash-pref-1"}),
        ),
    ):
        res = await execute_auto_push(
            db_session,
            show.id,
            {
                "season_ids": [s1.id],
                "allow_season_packs": True,
                "prefer_season_packs": True,
            },
        )

    assert queried_eps == [None], (
        "Should not probe E01 when Prefer Season Pack is True and a valid pack exists"
    )
    assert res["pushed"] is True
    assert res["dispatched_count"] == 1
    assert res["guids"] == ["guid-pack-720p-preferred"]


@pytest.mark.asyncio
async def test_poller_failed_season_pack_replacement_falls_back_to_episodes(
    db_session: AsyncSession,
) -> None:
    """
    Verify that when an auto-pushed Season Pack fails on TorBox and is blacklisted,
    `_auto_replace_failed_target` re-runs the 2-checkbox season logic and falls back
    to pushing individual episodes if no other Season Pack exists.
    """
    sys_settings = SystemSettings(id=1, sh_max_retries=3, download_timeout_hours=24)
    db_session.add(sys_settings)
    show, s1, eps = await _seed_show_with_3_episodes(
        db_session, allow_season_packs=True, prefer_season_packs=True
    )
    s1.status = SeasonStatus.DOWNLOADING
    for ep in eps:
        ep.status = EpisodeStatus.DOWNLOADING

    hist = DownloadHistory(
        media_item_id=show.id,
        season_id=s1.id,
        episode_id=None,
        nzb_title="Andor.S01.1080p.WEB-DL-BROKENPACK",
        nzb_guid="guid-broken-pack",
        score=5000.0,
        size_bytes=10 * GB,
        downloader_transfer_id="tb-broken-pack",
        dispatched_at=datetime.now(timezone.utc),
        push_mode="auto",
    )
    db_session.add(hist)
    await db_session.commit()

    async def fake_search_show(
        season=None, ep=None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        if ep is None:
            # Only the now-blacklisted pack exists in the pack pool
            return [
                {
                    "title": "Andor.S01.1080p.WEB-DL-BROKENPACK",
                    "size": 10 * GB,
                    "guid": "guid-broken-pack",
                }
            ]
        ep_num = int(str(ep))
        return [
            {
                "title": f"Andor.S01E{ep_num:02d}.1080p.WEB-DL.H264-GOOD",
                "size": int(1.5 * GB),
                "guid": f"guid-good-ep-{ep_num}",
            }
        ]

    tb_seq = 0

    async def fake_send_nzb(*args: Any, **kwargs: Any) -> dict[str, str]:
        nonlocal tb_seq
        tb_seq += 1
        return {"id": f"tb-rep-ep-{tb_seq}", "hash": f"hash-rep-ep-{tb_seq}"}

    with (
        patch(
            "app.services.downloaders.torbox.delete_usenet_download",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(FAKE_VALID_NZB, "ep.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file", side_effect=fake_send_nzb
        ),
        patch("app.services.discord.dispatch_notification_event", new=AsyncMock()),
    ):
        tick_res = await run_transfer_poller_tick(
            session=db_session,
            pre_fetched_downloads=[
                {
                    "id": "tb-broken-pack",
                    "name": "Andor.S01.1080p.WEB-DL-BROKENPACK",
                    "download_state": "failed",
                    "progress": 0.0,
                }
            ],
        )

    assert tick_res["polled"] is True
    active_hists = (
        (
            await db_session.execute(
                select(DownloadHistory)
                .where(
                    DownloadHistory.media_item_id == show.id,
                    DownloadHistory.is_dismissed.is_(False),
                )
                .order_by(DownloadHistory.id.asc())
            )
        )
        .scalars()
        .all()
    )
    assert [h.nzb_guid for h in active_hists] == [
        "guid-good-ep-1",
        "guid-good-ep-2",
        "guid-good-ep-3",
    ]


@pytest.mark.asyncio
async def test_preset_scoped_season_pack_authority_and_custom_overrides(
    db_session: AsyncSession,
) -> None:
    """
    Verify ADR-099 Preset-Scoped Season Pack Authority:
    1. Updating a SearchPreset's allow_season_packs / prefer_season_packs via update_preset()
       immediately changes resolve_effective_search_config(session, item) for all MediaItems
       bound to that preset (even if item.allow_season_packs was stale).
    2. When preset_id is 'custom' / None (or when allow_season_packs / prefer_season_packs
       diverges from the selected preset), persist_sticky_preferences / update_item_sticky_search_config
       stores allow_season_packs and prefer_season_packs in custom_search_config_json and sets
       item.preset_id = None.
    3. Re-binding an item to a valid preset clears custom_search_config_json = None so live
       preset values govern again.
    """
    from app.core.push_engine import (
        persist_sticky_preferences,
        resolve_effective_search_config,
    )

    show_bound, _, _ = await _seed_show_with_3_episodes(
        db_session, allow_season_packs=False, prefer_season_packs=False
    )
    show_custom = MediaItem(
        provider_id=show_bound.provider_id,
        simkl_id=88002,
        tvdb_id=88002,
        title="Custom Override Show",
        year=2023,
        media_type=MediaType.SHOW,
        status=MediaStatus.SEARCHING,
    )
    db_session.add(show_custom)
    await db_session.flush()

    preset = await create_preset(
        db_session,
        {
            "name": "4K Pack Preset",
            "is_default": True,
            "allow_season_packs": True,
            "prefer_season_packs": True,
        },
    )
    await update_item_sticky_search_config(
        db_session,
        show_bound.id,
        {"preset_id": preset.id},
    )
    await db_session.commit()

    # 1. Bound show resolves live preset values (True, True)
    cfg = await resolve_effective_search_config(db_session, show_bound)
    assert cfg["preset_id"] == preset.id
    assert cfg["allow_season_packs"] is True
    assert cfg["prefer_season_packs"] is True

    # 2. Update preset in Settings to Score Decides (True, False) -> immediately reflected!
    await update_preset(
        db_session,
        preset.id,
        {"allow_season_packs": True, "prefer_season_packs": False},
    )
    cfg_after_prefer_off = await resolve_effective_search_config(db_session, show_bound)
    assert cfg_after_prefer_off["allow_season_packs"] is True
    assert cfg_after_prefer_off["prefer_season_packs"] is False

    # 3. Update preset in Settings to Episodes Only (False, False) -> immediately reflected
    # even though show_bound.allow_season_packs had been True!
    await update_preset(
        db_session,
        preset.id,
        {"allow_season_packs": False},
    )
    cfg_after_allow_off = await resolve_effective_search_config(db_session, show_bound)
    assert cfg_after_allow_off["allow_season_packs"] is False
    assert cfg_after_allow_off["prefer_season_packs"] is False

    # 4. Diverging season pack flags in persist_sticky_preferences detaches preset_id -> None
    # and persists custom flags in custom_search_config_json
    await persist_sticky_preferences(
        db_session,
        show_custom,
        {
            "preset_id": preset.id,
            "allow_season_packs": True,
            "prefer_season_packs": False,
        },
    )
    await db_session.refresh(show_custom)
    assert show_custom.preset_id is None
    assert show_custom.custom_search_config_json is not None
    cfg_custom = await resolve_effective_search_config(db_session, show_custom)
    assert cfg_custom["preset_id"] is None
    assert cfg_custom["allow_season_packs"] is True
    assert cfg_custom["prefer_season_packs"] is False

    # Updating the preset does NOT overwrite show_custom's unsaved Custom Overrides
    await update_preset(
        db_session,
        preset.id,
        {"allow_season_packs": True, "prefer_season_packs": True},
    )
    cfg_custom_still = await resolve_effective_search_config(db_session, show_custom)
    assert cfg_custom_still["preset_id"] is None
    assert cfg_custom_still["allow_season_packs"] is True
    assert cfg_custom_still["prefer_season_packs"] is False

    # Meanwhile, show_bound immediately sees the updated preset (True, True)
    cfg_bound_updated = await resolve_effective_search_config(db_session, show_bound)
    assert cfg_bound_updated["preset_id"] == preset.id
    assert cfg_bound_updated["allow_season_packs"] is True
    assert cfg_bound_updated["prefer_season_packs"] is True

    # 5. Re-binding show_custom to the preset clears custom_search_config_json
    await persist_sticky_preferences(
        db_session,
        show_custom,
        {
            "preset_id": preset.id,
            "allow_season_packs": True,
            "prefer_season_packs": True,
        },
    )
    await db_session.refresh(show_custom)
    assert show_custom.preset_id == preset.id
    assert show_custom.custom_search_config_json is None


@pytest.mark.asyncio
async def test_ui_renders_coupled_season_pack_checkboxes_in_modal_and_settings(
    db_session: AsyncSession,
    async_client: AsyncClient,
) -> None:
    """Verify Push Modal and Settings Presets tab render both Allow Season Pack and Prefer Season Pack controls."""
    show, _, _ = await _seed_show_with_3_episodes(
        db_session, allow_season_packs=False, prefer_season_packs=False
    )

    modal_resp = await async_client.get(
        f"/api/items/{show.id}/push-modal",
        headers={"Accept": "text/html"},
    )
    assert modal_resp.status_code == 200
    modal_html = modal_resp.text
    assert "Allow Season Pack" in modal_html
    assert "Prefer Season Pack" in modal_html
    assert "allowSeasonPacks" in modal_html
    assert ':disabled="!allowSeasonPacks"' in modal_html
    assert (
        "@change=\"if (!allowSeasonPacks) preferSeasonPacks = false; selectedPresetId = 'custom'\""
        in modal_html
    )
    assert "@change=\"selectedPresetId = 'custom'\"" in modal_html

    settings_resp = await async_client.get("/settings/preset/new/modal")
    assert settings_resp.status_code == 200
    settings_html = settings_resp.text
    assert "Allow Season Pack" in settings_html
    assert "Prefer Season Pack" in settings_html
    assert "presetAllowSeasonPacks" in settings_html
    assert ':disabled="!presetAllowSeasonPacks"' in settings_html


@pytest.mark.asyncio
async def test_state_2_score_decides_e02_layer2_fake_aborts_to_pack_with_zero_episode_uploads(
    db_session: AsyncSession,
) -> None:
    """
    Verify ADR-098:
    In Score Decides mode:
    - E01 has a valid 4K Remux NZB that beats best_pack (1080p WEB-DL), and also a lower-scoring
      candidate where the top E01 candidate is fake and #2 E01 candidate is valid and beats pack.
    - E02 has 2 NZB candidates that both fail Layer 2 in memory (.exe and sample-only).
    - 0 individual episodes are uploaded to TorBox, E02's fake NZBs are blacklisted in
      BlacklistedRelease, and best_pack is dispatched to TorBox instead.
    """
    from app.db.models import BlacklistedRelease

    show, s1, _ = await _seed_show_with_3_episodes(
        db_session, allow_season_packs=True, prefer_season_packs=False
    )

    fake_exe_nzb = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<nzb><file subject="Andor.S01E02.2160p.exe (1/1)">'
        b'<segments><segment bytes="5000000" number="1">seg1</segment></segments>'
        b"</file></nzb>"
    )
    fake_sample_nzb = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<nzb><file subject="Andor.S01E02.2160p.sample.mkv (1/1)">'
        b'<segments><segment bytes="20000000" number="1">seg2</segment></segments>'
        b"</file></nzb>"
    )

    async def fake_search_show(
        season=None, ep=None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        if ep is None:
            return [
                {
                    "title": "Andor.S01.1080p.WEB-DL.DDP5.1.H.264-PACK",
                    "size": 12 * GB,
                    "guid": "guid-pack-valid-1080p",
                }
            ]
        ep_num = int(str(ep))
        if ep_num == 1:
            return [
                {
                    "title": "Andor.S01E01.2160p.UHD.BluRay.REMUX.HEVC.TrueHD.7.1.Atmos-GRP",
                    "size": 8 * GB,
                    "guid": "guid-e01-valid-4k",
                }
            ]
        if ep_num == 2:
            return [
                {
                    "title": "Andor.S01E02.2160p.UHD.BluRay.REMUX.HEVC.TrueHD.7.1-FAKE1",
                    "size": 8 * GB,
                    "guid": "guid-e02-fake-exe",
                },
                {
                    "title": "Andor.S01E02.2160p.WEB-DL.DDP5.1.H.265-FAKE2",
                    "size": 5 * GB,
                    "guid": "guid-e02-fake-sample",
                },
            ]
        return [
            {
                "title": "Andor.S01E03.2160p.UHD.BluRay.REMUX.HEVC.TrueHD.7.1.Atmos-GRP",
                "size": 8 * GB,
                "guid": "guid-e03-valid-4k",
            }
        ]

    async def fake_fetch_nzb(guid: str, **_kwargs: Any) -> tuple[bytes, str]:
        if guid == "guid-e02-fake-exe":
            return fake_exe_nzb, "e02_fake1.nzb"
        if guid == "guid-e02-fake-sample":
            return fake_sample_nzb, "e02_fake2.nzb"
        return FAKE_VALID_NZB, f"{guid}.nzb"

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes", side_effect=fake_fetch_nzb
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new=AsyncMock(return_value={"id": "tb-pack-win", "hash": "hash-pack-win"}),
        ) as mock_send,
    ):
        res = await execute_auto_push(
            db_session,
            show.id,
            {
                "season_ids": [s1.id],
                "allow_season_packs": True,
                "prefer_season_packs": False,
            },
        )

    assert mock_send.await_count == 1
    assert mock_send.await_args.kwargs["filename"] == "guid-pack-valid-1080p.nzb"
    assert res["pushed"] is True
    assert res["dispatched_count"] == 1
    assert res["guids"] == ["guid-pack-valid-1080p"]

    bl_guids = {
        r.nzb_guid
        for r in (
            await db_session.execute(
                select(BlacklistedRelease).where(
                    BlacklistedRelease.media_item_id == show.id
                )
            )
        )
        .scalars()
        .all()
    }
    assert "guid-e02-fake-exe" in bl_guids
    assert "guid-e02-fake-sample" in bl_guids


@pytest.mark.asyncio
async def test_state_2_score_decides_top_season_pack_layer2_fake_falls_back_in_preflight(
    db_session: AsyncSession,
) -> None:
    """
    Verify ADR-098:
    In Score Decides mode, if the #1 Season Pack (4K Remux) is a Layer-2 fake (.exe),
    it is blacklisted in pre-flight BEFORE comparing against E01:
    - Case A: #2 Season Pack (720p HDTV) is valid, so E01..E03 (1080p BluRay) beat #2 Season Pack
      and all 3 episodes are dispatched instead of being blocked by the fake #1 Season Pack.
    - Case B: All Season Packs are Layer-2 fakes -> falls through to individual episodes cleanly.
    """
    from app.db.models import BlacklistedRelease

    show, s1, _ = await _seed_show_with_3_episodes(
        db_session, allow_season_packs=True, prefer_season_packs=False
    )

    fake_exe_nzb = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<nzb><file subject="Andor.S01.2160p.exe (1/1)">'
        b'<segments><segment bytes="5000000" number="1">seg1</segment></segments>'
        b"</file></nzb>"
    )

    async def fake_search_show(
        season=None, ep=None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        if ep is None:
            return [
                {
                    "title": "Andor.S01.2160p.UHD.BluRay.REMUX.HEVC.TrueHD.7.1.Atmos-FAKEPACK",
                    "size": 50 * GB,
                    "guid": "guid-pack-fake-4k",
                },
                {
                    "title": "Andor.S01.720p.HDTV.x264-LOWPACK",
                    "size": 4 * GB,
                    "guid": "guid-pack-valid-720p",
                },
            ]
        ep_num = int(str(ep))
        return [
            {
                "title": f"Andor.S01E{ep_num:02d}.1080p.BluRay.x265.DTS-HD.MA.5.1-GOOD",
                "size": 4 * GB,
                "guid": f"guid-ep-1080p-{ep_num}",
            }
        ]

    async def fake_fetch_nzb(guid: str, **_kwargs: Any) -> tuple[bytes, str]:
        if guid == "guid-pack-fake-4k":
            return fake_exe_nzb, "fake_pack.nzb"
        return FAKE_VALID_NZB, f"{guid}.nzb"

    tb_seq = 0

    async def fake_send_nzb(*args: Any, **kwargs: Any) -> dict[str, str]:
        nonlocal tb_seq
        tb_seq += 1
        return {"id": f"tb-ep-{tb_seq}", "hash": f"hash-ep-{tb_seq}"}

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes", side_effect=fake_fetch_nzb
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file", side_effect=fake_send_nzb
        ) as mock_send,
    ):
        res = await execute_auto_push(
            db_session,
            show.id,
            {
                "season_ids": [s1.id],
                "allow_season_packs": True,
                "prefer_season_packs": False,
            },
        )

    # Without pre-flight pack verification, the fake 4K Remux pack would have outscored 1080p E01!
    # With pre-flight verification, guid-pack-fake-4k is blacklisted in memory, verified_pack becomes
    # guid-pack-valid-720p, and 1080p BluRay E01..E03 beat it and get dispatched!
    assert res["pushed"] is True
    assert res["dispatched_count"] == 3
    assert res["guids"] == ["guid-ep-1080p-1", "guid-ep-1080p-2", "guid-ep-1080p-3"]
    assert mock_send.await_count == 3

    bl_guids = {
        r.nzb_guid
        for r in (
            await db_session.execute(
                select(BlacklistedRelease).where(
                    BlacklistedRelease.media_item_id == show.id
                )
            )
        )
        .scalars()
        .all()
    }
    assert "guid-pack-fake-4k" in bl_guids

    # Case B: #1 Season Pack (4K Remux) is fake, #2 Season Pack (1080p BluRay) is valid and beats E01 (720p HDTV)
    # -> #2 Season Pack is dispatched directly without probing E02..E03!
    show2 = MediaItem(
        provider_id=show.provider_id,
        simkl_id=88099,
        tvdb_id=88099,
        title="Foundation",
        year=2021,
        media_type=MediaType.SHOW,
        status=MediaStatus.SEARCHING,
    )
    db_session.add(show2)
    await db_session.flush()
    s1_b = Season(
        media_item_id=show2.id,
        season_number=1,
        watch_order=1,
        type_number=1,
        entry_type="season",
        title="Season 1",
        monitored=True,
        episode_count=2,
        air_date=datetime(2021, 9, 24, tzinfo=timezone.utc),
        status=SeasonStatus.SEARCHING,
    )
    db_session.add(s1_b)
    await db_session.flush()
    for ep_n in (1, 2):
        db_session.add(
            Episode(
                season_id=s1_b.id,
                episode_number=ep_n,
                monitored=True,
                air_date=datetime(2021, 9, 24, tzinfo=timezone.utc),
                status=EpisodeStatus.SEARCHING,
            )
        )
    await db_session.commit()

    async def fake_search_show_b(
        season=None, ep=None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        if ep is None:
            return [
                {
                    "title": "Foundation.S01.2160p.UHD.BluRay.REMUX.HEVC.TrueHD.7.1.Atmos-FAKEPACK",
                    "size": 40 * GB,
                    "guid": "guid-found-pack-fake",
                },
                {
                    "title": "Foundation.S01.1080p.BluRay.x265.DTS-HD.MA.5.1-VALIDPACK",
                    "size": 16 * GB,
                    "guid": "guid-found-pack-valid",
                },
            ]
        return [
            {
                "title": f"Foundation.S01E0{ep}.720p.HDTV.x264-LOW",
                "size": int(0.8 * GB),
                "guid": f"guid-found-ep-{ep}",
            }
        ]

    async def fake_fetch_nzb_b(guid: str, **_kwargs: Any) -> tuple[bytes, str]:
        if guid == "guid-found-pack-fake":
            return fake_exe_nzb, "fake_found_pack.nzb"
        return FAKE_VALID_NZB, f"{guid}.nzb"

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show_b
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            side_effect=fake_fetch_nzb_b,
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new=AsyncMock(return_value={"id": "tb-pack-2", "hash": "hash-pack-2"}),
        ) as mock_send_b,
    ):
        res_b = await execute_auto_push(
            db_session,
            show2.id,
            {
                "season_ids": [s1_b.id],
                "allow_season_packs": True,
                "prefer_season_packs": False,
            },
        )

    assert res_b["pushed"] is True
    assert res_b["dispatched_count"] == 1
    assert res_b["guids"] == ["guid-found-pack-valid"]
    assert mock_send_b.await_count == 1
