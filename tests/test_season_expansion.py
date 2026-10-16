"""
Unit and Integration Tests for Ticket 07:
Simkl Watch-Progress Season Expansion & Movie Bridge Lookahead
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.push_engine import (
    advance_season_buffer,
    evaluate_watch_progress_and_advance,
    execute_auto_push,
)
from app.db.models import (
    DownloadHistory,
    MediaItem,
    MediaStatus,
    MediaType,
    NotificationChannel,
    Provider,
    SearchPreset,
    Season,
    SeasonStatus,
    SystemSettings,
)
from tests.conftest import ingest_simkl_payloads

FAKE_VALID_NZB = b"""<?xml version="1.0" encoding="UTF-8"?>
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
  <file poster="test" date="1700000000" subject="Test.Release.1080p.WEB-DL.mkv (1/1)">
    <groups><group>alt.binaries.test</group></groups>
    <segments><segment bytes="500000000" number="1">seg1@test</segment></segments>
  </file>
</nzb>
"""


async def _seed_environment(session: AsyncSession) -> tuple[Provider, SearchPreset]:
    """Seed SystemSettings, Indexer, Simkl Provider, Discord/Telegram notifications, and a SearchPreset."""
    sys_settings = (
        (await session.execute(select(SystemSettings).where(SystemSettings.id == 1)))
        .scalars()
        .first()
    )
    if sys_settings is None:
        sys_settings = SystemSettings(
            id=1,
            discord_webhook_url="https://discord.com/api/webhooks/123/abc",
            discord_enabled=True,
            notify_on_push_initiated=False,
            notify_on_completed=True,
            notify_on_failure=True,
            notify_on_auto_advance=True,
        )
        session.add(sys_settings)
    else:
        sys_settings.discord_webhook_url = "https://discord.com/api/webhooks/123/abc"
        sys_settings.discord_enabled = True
        sys_settings.notify_on_auto_advance = True

    tg = NotificationChannel(
        name="Telegram Main",
        type="telegram",
        bot_token="123:ABC",
        chat_id="999",
    )
    indexer = Provider(
        name="PrimaryIndexer",
        type="newznab",
        category="indexer",
        is_active=True,
        priority=1,
        api_url="https://indexer.example.com",
        api_key="idx-key",
    )
    simkl_prov = Provider(
        name="Simkl Main",
        type="simkl",
        category="watchlist",
        is_active=True,
        client_id="simkl-client",
        access_token="simkl-token",
    )
    preset = SearchPreset(
        name="English 1080p Best",
        is_default=True,
        primary_language="en",
        fallback_language="de",
        video_quality_mode="best",
        audio_quality_mode="best",
        allow_season_packs=True,
        prefer_season_packs=True,
    )
    session.add_all([tg, indexer, simkl_prov, preset])
    await session.flush()
    return simkl_prov, preset


@pytest.mark.asyncio
async def test_initial_season_expansion_n_to_n_plus_1_and_notification(
    db_session: AsyncSession,
) -> None:
    """Pushing entry N on a Series with auto_advance_seasons=True automatically triggers Auto-Push Best for N+1 and dispatches Auto-Advance notification."""
    simkl_prov, preset = await _seed_environment(db_session)

    show = MediaItem(
        provider_id=simkl_prov.id,
        simkl_id=71001,
        tmdb_id=81001,
        title="Severance",
        year=2022,
        media_type=MediaType.SHOW,
        status=MediaStatus.SEARCHING,
        release_date=datetime(2022, 2, 18, tzinfo=timezone.utc),
        preset_id=preset.id,
        prefer_season_packs=True,
        auto_advance_seasons=True,
    )
    db_session.add(show)
    await db_session.flush()

    s1 = Season(
        media_item_id=show.id,
        season_number=1,
        watch_order=1,
        type_number=1,
        entry_type="season",
        title="Season 1",
        monitored=True,
        episode_count=9,
        air_date=datetime(2022, 2, 18, tzinfo=timezone.utc),
        status=SeasonStatus.SEARCHING,
    )
    s2 = Season(
        media_item_id=show.id,
        season_number=2,
        watch_order=2,
        type_number=2,
        entry_type="season",
        title="Season 2",
        monitored=False,
        episode_count=10,
        air_date=datetime(2025, 1, 17, tzinfo=timezone.utc),
        status=SeasonStatus.PENDING,
    )
    s3_future = Season(
        media_item_id=show.id,
        season_number=3,
        watch_order=3,
        type_number=3,
        entry_type="season",
        title="Season 3",
        monitored=False,
        episode_count=10,
        air_date=None,
        status=SeasonStatus.FUTURE,
    )
    db_session.add_all([s1, s2, s3_future])
    await db_session.commit()

    async def fake_search_show(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        season_num = kwargs.get("season")
        if season_num == 1:
            return [
                {
                    "title": "Severance.S01.1080p.WEB-DL.DDP5.1.H.264-english",
                    "guid": "guid-sev-s01",
                    "size_bytes": 12 * 1024**3,
                }
            ]
        if season_num == 2:
            return [
                {
                    "title": "Severance.S02.1080p.WEB-DL.DDP5.1.H.264-english",
                    "guid": "guid-sev-s02",
                    "size_bytes": 14 * 1024**3,
                }
            ]
        return []

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(FAKE_VALID_NZB, "test.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new=AsyncMock(
                side_effect=[
                    {"id": "tb-sev-s1", "hash": "hash-sev-s1"},
                    {"id": "tb-sev-s2", "hash": "hash-sev-s2"},
                ]
            ),
        ),
        patch(
            "app.services.discord.send_discord_webhook",
            new=AsyncMock(return_value=True),
        ) as mock_discord,
        patch(
            "app.services.telegram.send_notification",
            new=AsyncMock(return_value=True),
        ) as mock_telegram,
    ):
        res = await execute_auto_push(
            db_session,
            show.id,
            {
                "season_ids": [s1.id],
                "auto_advance_seasons": True,
                "prefer_season_packs": True,
            },
        )

    assert res["pushed"] is True
    assert res["dispatched_count"] == 2
    assert res["guids"] == ["guid-sev-s01", "guid-sev-s02"]

    await db_session.refresh(s1)
    await db_session.refresh(s2)
    await db_session.refresh(s3_future)
    assert s1.status == SeasonStatus.DOWNLOADING
    assert s2.status == SeasonStatus.DOWNLOADING
    assert s3_future.status == SeasonStatus.FUTURE

    # Push Initiated is disabled by default, while Auto-Advance is enabled -> 1 notification for S2 auto-advance
    assert mock_discord.await_count == 1
    assert mock_telegram.await_count == 1
    embed_sent = mock_discord.await_args_list[0].kwargs["embed"]
    assert "Auto-Advance" in embed_sent["title"]
    assert "Severance" in embed_sent["title"]


@pytest.mark.asyncio
async def test_movie_bridge_lookahead_n_to_movie_and_next_season(
    db_session: AsyncSession,
) -> None:
    """When N+1 has entry_type == 'movie', auto-advance pushes both N+1 (Movie) and N+2 (Season)."""
    simkl_prov, preset = await _seed_environment(db_session)

    anime = MediaItem(
        provider_id=simkl_prov.id,
        simkl_id=72001,
        anilist_id=101922,
        title="Demon Slayer",
        year=2019,
        media_type=MediaType.ANIME,
        status=MediaStatus.SEARCHING,
        release_date=datetime(2019, 4, 6, tzinfo=timezone.utc),
        preset_id=preset.id,
        prefer_season_packs=True,
        auto_advance_seasons=True,
    )
    db_session.add(anime)
    await db_session.flush()

    s1 = Season(
        media_item_id=anime.id,
        season_number=1,
        watch_order=1,
        type_number=1,
        entry_type="season",
        title="Demon Slayer: Kimetsu no Yaiba",
        monitored=True,
        episode_count=26,
        air_date=datetime(2019, 4, 6, tzinfo=timezone.utc),
        status=SeasonStatus.SEARCHING,
    )
    m1 = Season(
        media_item_id=anime.id,
        season_number=2,
        watch_order=2,
        type_number=1,
        entry_type="movie",
        title="Demon Slayer: Mugen Train",
        monitored=True,
        episode_count=1,
        air_date=datetime(2020, 10, 16, tzinfo=timezone.utc),
        status=SeasonStatus.SEARCHING,
    )
    s2 = Season(
        media_item_id=anime.id,
        season_number=3,
        watch_order=3,
        type_number=2,
        entry_type="season",
        title="Demon Slayer: Entertainment District Arc",
        monitored=True,
        episode_count=11,
        air_date=datetime(2021, 12, 5, tzinfo=timezone.utc),
        status=SeasonStatus.SEARCHING,
    )
    s3 = Season(
        media_item_id=anime.id,
        season_number=4,
        watch_order=4,
        type_number=3,
        entry_type="season",
        title="Demon Slayer: Swordsmith Village Arc",
        monitored=False,
        episode_count=11,
        air_date=datetime(2023, 4, 9, tzinfo=timezone.utc),
        status=SeasonStatus.PENDING,
    )
    db_session.add_all([s1, m1, s2, s3])
    await db_session.commit()

    async def fake_search_show(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        season_num = kwargs.get("season")
        if season_num == 1:
            return [
                {
                    "title": "Demon.Slayer.S01.1080p.BluRay.x264-english",
                    "guid": "guid-ds-s01",
                    "size_bytes": 18 * 1024**3,
                }
            ]
        if season_num == 2:
            return [
                {
                    "title": "Demon.Slayer.S02.1080p.BluRay.x264-english",
                    "guid": "guid-ds-s02",
                    "size_bytes": 10 * 1024**3,
                }
            ]
        return []

    async def fake_search_movie(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "title": "Demon.Slayer.Mugen.Train.2020.1080p.BluRay.x264-english",
                "guid": "guid-ds-m01",
                "size_bytes": 8 * 1024**3,
            }
        ]

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.search_movie", side_effect=fake_search_movie
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(FAKE_VALID_NZB, "test.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new=AsyncMock(
                side_effect=[
                    {"id": "tb-ds-s1", "hash": "hash-ds-s1"},
                    {"id": "tb-ds-m1", "hash": "hash-ds-m1"},
                    {"id": "tb-ds-s2", "hash": "hash-ds-s2"},
                ]
            ),
        ),
        patch(
            "app.services.discord.send_discord_webhook",
            new=AsyncMock(return_value=True),
        ) as mock_discord,
        patch(
            "app.services.telegram.send_notification",
            new=AsyncMock(return_value=True),
        ),
    ):
        res = await execute_auto_push(
            db_session,
            anime.id,
            {
                "season_ids": [s1.id],
                "auto_advance_seasons": True,
                "prefer_season_packs": True,
            },
        )

    assert res["pushed"] is True
    assert res["dispatched_count"] == 3
    assert res["guids"] == ["guid-ds-s01", "guid-ds-m01", "guid-ds-s02"]

    await db_session.refresh(s1)
    await db_session.refresh(m1)
    await db_session.refresh(s2)
    await db_session.refresh(s3)
    assert s1.status == SeasonStatus.DOWNLOADING
    assert m1.status == SeasonStatus.DOWNLOADING
    assert s2.status == SeasonStatus.DOWNLOADING
    assert s3.status == SeasonStatus.PENDING

    # 1 consolidated Auto-Advance batch notification sent covering Movie 1 + Season 2
    assert mock_discord.await_count == 1


@pytest.mark.asyncio
async def test_simkl_watch_progress_season_expansion_during_sync(
    db_session: AsyncSession,
) -> None:
    """Simkl watchlist sync detects watched progress in entry K > last_watched_order, updates last_watched_order = K, and auto-pushes K+1."""
    simkl_prov, preset = await _seed_environment(db_session)

    show = MediaItem(
        provider_id=simkl_prov.id,
        simkl_id=73001,
        tmdb_id=83001,
        title="The Bear",
        year=2022,
        media_type=MediaType.SHOW,
        status=MediaStatus.SEARCHING,
        release_date=datetime(2022, 6, 23, tzinfo=timezone.utc),
        preset_id=preset.id,
        prefer_season_packs=True,
        auto_advance_seasons=True,
        last_watched_order=1,
    )
    db_session.add(show)
    await db_session.flush()

    s1 = Season(
        media_item_id=show.id,
        season_number=1,
        watch_order=1,
        type_number=1,
        entry_type="season",
        title="Season 1",
        monitored=True,
        episode_count=8,
        air_date=datetime(2022, 6, 23, tzinfo=timezone.utc),
        status=SeasonStatus.COMPLETED,
    )
    s2 = Season(
        media_item_id=show.id,
        season_number=2,
        watch_order=2,
        type_number=2,
        entry_type="season",
        title="Season 2",
        monitored=True,
        episode_count=10,
        air_date=datetime(2023, 6, 22, tzinfo=timezone.utc),
        status=SeasonStatus.COMPLETED,
    )
    s3 = Season(
        media_item_id=show.id,
        season_number=3,
        watch_order=3,
        type_number=3,
        entry_type="season",
        title="Season 3",
        monitored=False,
        episode_count=10,
        air_date=datetime(2024, 6, 26, tzinfo=timezone.utc),
        status=SeasonStatus.SEARCHING,
    )
    db_session.add_all([s1, s2, s3])
    await db_session.commit()

    # Simkl sync payload reporting watched episodes in Season 2 (K = 2 > last_watched_order=1)
    simkl_shows_payload = [
        {
            "status": "watching",
            "last_watched": "S02E01",
            "watched_episodes_count": 9,
            "seasons": [
                {"number": 1, "episodes": [{"number": i} for i in range(1, 9)]},
                {"number": 2, "episodes": [{"number": 1}]},
            ],
            "show": {
                "title": "The Bear",
                "year": 2022,
                "ids": {"simkl": 73001, "tmdb": "83001"},
            },
        }
    ]

    async def fake_search_show(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        season_num = kwargs.get("season")
        if season_num == 3:
            return [
                {
                    "title": "The.Bear.S03.1080p.WEB-DL.DDP5.1.H.264-english",
                    "guid": "guid-bear-s03",
                    "size_bytes": 11 * 1024**3,
                }
            ]
        return []

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(FAKE_VALID_NZB, "test.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new=AsyncMock(return_value={"id": "tb-bear-s3", "hash": "hash-bear-s3"}),
        ) as mock_torbox_send,
        patch(
            "app.services.discord.send_discord_webhook",
            new=AsyncMock(return_value=True),
        ) as mock_discord,
        patch(
            "app.services.telegram.send_notification",
            new=AsyncMock(return_value=True),
        ),
    ):
        await ingest_simkl_payloads(
            db_session, simkl_prov, simkl_shows_payload, MediaType.SHOW
        )
        await db_session.commit()

        await db_session.refresh(show)
        await db_session.refresh(s3)
        assert show.last_watched_order == 2
        assert s3.status == SeasonStatus.DOWNLOADING
        assert mock_torbox_send.await_count == 1
        assert mock_discord.await_count == 1

        # Re-running sync with identical progress (K=2 == last_watched_order) should NOT push again
        await ingest_simkl_payloads(
            db_session, simkl_prov, simkl_shows_payload, MediaType.SHOW
        )
        await db_session.commit()
        assert mock_torbox_send.await_count == 1


@pytest.mark.asyncio
async def test_simkl_watch_progress_with_movie_bridge_and_consolidated_anime_season(
    db_session: AsyncSession,
) -> None:
    """Simkl watch progress in Season 1 (K=1) when Movie 1 (order 2) is next pushes both Movie 1 and Season 2 (order 3); and progress in consolidated Season 2 (order 3) pushes Season 3 (order 4)."""
    simkl_prov, preset = await _seed_environment(db_session)

    anime = MediaItem(
        provider_id=simkl_prov.id,
        simkl_id=74001,
        anilist_id=94001,
        title="Made in Abyss",
        year=2017,
        media_type=MediaType.ANIME,
        status=MediaStatus.SEARCHING,
        release_date=datetime(2017, 7, 7, tzinfo=timezone.utc),
        preset_id=preset.id,
        prefer_season_packs=True,
        auto_advance_seasons=True,
        last_watched_order=0,
    )
    db_session.add(anime)
    await db_session.flush()

    s1 = Season(
        media_item_id=anime.id,
        season_number=1,
        watch_order=1,
        type_number=1,
        entry_type="season",
        simkl_id=74001,
        anilist_id=94001,
        title="Made in Abyss",
        monitored=True,
        episode_count=13,
        air_date=datetime(2017, 7, 7, tzinfo=timezone.utc),
        status=SeasonStatus.COMPLETED,
    )
    m1 = Season(
        media_item_id=anime.id,
        season_number=2,
        watch_order=2,
        type_number=1,
        entry_type="movie",
        simkl_id=74002,
        anilist_id=94002,
        title="Made in Abyss: Dawn of the Deep Soul",
        monitored=True,
        episode_count=1,
        air_date=datetime(2020, 1, 17, tzinfo=timezone.utc),
        status=SeasonStatus.SEARCHING,
    )
    s2 = Season(
        media_item_id=anime.id,
        season_number=3,
        watch_order=3,
        type_number=2,
        entry_type="season",
        simkl_id=74003,
        anilist_id=94003,
        title="Made in Abyss: The Golden City of the Scorching Sun",
        monitored=True,
        episode_count=12,
        air_date=datetime(2022, 7, 6, tzinfo=timezone.utc),
        status=SeasonStatus.SEARCHING,
    )
    s3 = Season(
        media_item_id=anime.id,
        season_number=4,
        watch_order=4,
        type_number=3,
        entry_type="season",
        simkl_id=74004,
        anilist_id=94004,
        title="Made in Abyss Season 3",
        monitored=False,
        episode_count=12,
        air_date=datetime(2024, 7, 1, tzinfo=timezone.utc),
        status=SeasonStatus.SEARCHING,
    )
    db_session.add_all([s1, m1, s2, s3])
    await db_session.commit()

    async def fake_search_movie(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "title": "Made.in.Abyss.Dawn.of.the.Deep.Soul.2020.1080p.BluRay.x264-english",
                "guid": "guid-mia-m01",
                "size_bytes": 7 * 1024**3,
            }
        ]

    async def fake_search_show(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        season_num = kwargs.get("season")
        if season_num == 2:
            return [
                {
                    "title": "Made.in.Abyss.S02.1080p.BluRay.x264-english",
                    "guid": "guid-mia-s02",
                    "size_bytes": 12 * 1024**3,
                }
            ]
        if season_num == 3:
            return [
                {
                    "title": "Made.in.Abyss.S03.1080p.BluRay.x264-english",
                    "guid": "guid-mia-s03",
                    "size_bytes": 12 * 1024**3,
                }
            ]
        return []

    # Step 1: Simkl sync reports watched_episodes=4 on Season 1 (K=1 > 0)
    simkl_step1 = [
        {
            "status": "watching",
            "watched_episodes": 4,
            "anime": {
                "title": "Made in Abyss",
                "year": 2017,
                "ids": {"simkl": 74001, "anilist": "94001"},
            },
        }
    ]

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.search_movie", side_effect=fake_search_movie
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(FAKE_VALID_NZB, "test.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new=AsyncMock(
                side_effect=[
                    {"id": "tb-mia-m1", "hash": "hash-mia-m1"},
                    {"id": "tb-mia-s2", "hash": "hash-mia-s2"},
                    {"id": "tb-mia-s3", "hash": "hash-mia-s3"},
                ]
            ),
        ) as mock_torbox_send,
        patch(
            "app.services.discord.send_discord_webhook",
            new=AsyncMock(return_value=True),
        ) as mock_discord,
        patch(
            "app.services.telegram.send_notification",
            new=AsyncMock(return_value=True),
        ),
    ):
        await ingest_simkl_payloads(
            db_session, simkl_prov, simkl_step1, MediaType.ANIME
        )
        await db_session.commit()

        await db_session.refresh(anime)
        await db_session.refresh(m1)
        await db_session.refresh(s2)
        await db_session.refresh(s3)
        assert anime.last_watched_order == 1
        assert m1.status == SeasonStatus.DOWNLOADING
        assert s2.status == SeasonStatus.DOWNLOADING
        assert s3.status == SeasonStatus.SEARCHING
        assert mock_torbox_send.await_count == 2
        assert mock_discord.await_count == 1

        # Step 2: Simkl sync reports watched progress on consolidated sequel Season 2 (simkl_id=74003, watch_order=3)
        simkl_step2 = [
            {
                "status": "watching",
                "watched_episodes": 2,
                "anime": {
                    "title": "Made in Abyss: The Golden City of the Scorching Sun",
                    "year": 2022,
                    "ids": {"simkl": 74003, "anilist": "94003"},
                },
            }
        ]
        await ingest_simkl_payloads(
            db_session, simkl_prov, simkl_step2, MediaType.ANIME
        )
        await db_session.commit()

        await db_session.refresh(anime)
        await db_session.refresh(s3)
        assert anime.last_watched_order == 3
        assert s3.status == SeasonStatus.DOWNLOADING
        assert mock_torbox_send.await_count == 3
        assert mock_discord.await_count == 2

    # Verify DownloadHistory entries recorded with push_mode == 'auto'
    h_rows = (
        (
            await db_session.execute(
                select(DownloadHistory).where(DownloadHistory.media_item_id == anime.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(h_rows) == 3
    assert all(h.push_mode == "auto" for h in h_rows)
    assert callable(advance_season_buffer)
    assert callable(evaluate_watch_progress_and_advance)


@pytest.mark.asyncio
async def test_disabled_auto_advance_and_manual_grab_expansion(
    db_session: AsyncSession,
) -> None:
    """When auto_advance_seasons is False, Simkl sync updates last_watched_order without pushing; when enabled on manual grab, N+1 is auto-pushed."""
    from app.core.push_engine import execute_manual_grab

    simkl_prov, preset = await _seed_environment(db_session)

    show = MediaItem(
        provider_id=simkl_prov.id,
        simkl_id=75001,
        tmdb_id=85001,
        title="Silo",
        year=2023,
        media_type=MediaType.SHOW,
        status=MediaStatus.SEARCHING,
        release_date=datetime(2023, 5, 5, tzinfo=timezone.utc),
        preset_id=preset.id,
        prefer_season_packs=True,
        auto_advance_seasons=False,
        last_watched_order=0,
    )
    db_session.add(show)
    await db_session.flush()

    s1 = Season(
        media_item_id=show.id,
        season_number=1,
        watch_order=1,
        type_number=1,
        entry_type="season",
        title="Season 1",
        monitored=True,
        episode_count=10,
        air_date=datetime(2023, 5, 5, tzinfo=timezone.utc),
        status=SeasonStatus.SEARCHING,
    )
    s2 = Season(
        media_item_id=show.id,
        season_number=2,
        watch_order=2,
        type_number=2,
        entry_type="season",
        title="Season 2",
        monitored=False,
        episode_count=10,
        air_date=datetime(2024, 11, 15, tzinfo=timezone.utc),
        status=SeasonStatus.SEARCHING,
    )
    db_session.add_all([s1, s2])
    await db_session.commit()

    simkl_payload = [
        {
            "status": "watching",
            "seasons": [{"number": 1, "episodes": [{"number": 1}]}],
            "show": {
                "title": "Silo",
                "year": 2023,
                "ids": {"simkl": 75001, "tmdb": "85001"},
            },
        }
    ]

    with patch(
        "app.services.downloaders.torbox.send_nzb_file",
        new=AsyncMock(return_value={"id": "tb-silo", "hash": "hash-silo"}),
    ) as mock_send:
        await ingest_simkl_payloads(
            db_session, simkl_prov, simkl_payload, MediaType.SHOW
        )
        await db_session.commit()
        await db_session.refresh(show)
        await db_session.refresh(s2)
        assert show.last_watched_order == 1
        assert s2.status == SeasonStatus.SEARCHING
        assert mock_send.await_count == 0

    # Now manually grab S1 with auto_advance_seasons=True -> S1 (manual) + S2 (auto) are dispatched
    async def fake_search_show(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if kwargs.get("season") == 2:
            return [
                {
                    "title": "Silo.S02.1080p.WEB-DL.DDP5.1.H.264-english",
                    "guid": "guid-silo-s02",
                    "size": 10 * 1024**3,
                }
            ]
        return []

    with (
        patch(
            "app.services.indexers.newznab.search_show", side_effect=fake_search_show
        ),
        patch(
            "app.services.indexers.newznab.fetch_nzb_bytes",
            new=AsyncMock(return_value=(FAKE_VALID_NZB, "test.nzb")),
        ),
        patch(
            "app.services.downloaders.torbox.send_nzb_file",
            new=AsyncMock(
                side_effect=[
                    {"id": "tb-silo-s1", "hash": "hash-silo-s1"},
                    {"id": "tb-silo-s2", "hash": "hash-silo-s2"},
                ]
            ),
        ),
        patch(
            "app.services.discord.send_discord_webhook",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.services.telegram.send_notification",
            new=AsyncMock(return_value=True),
        ),
    ):
        grab_res = await execute_manual_grab(
            db_session,
            show.id,
            {
                "guid": "guid-silo-s01",
                "title": "Silo.S01.1080p.WEB-DL.DDP5.1.H.264-english",
                "season_id": s1.id,
                "size_bytes": 10 * 1024**3,
                "score": 4500,
                "auto_advance_seasons": True,
            },
        )

    assert grab_res["pushed"] is True
    assert grab_res["auto_advanced_count"] == 1
    await db_session.refresh(s1)
    await db_session.refresh(s2)
    assert s1.status == SeasonStatus.DOWNLOADING
    assert s2.status == SeasonStatus.DOWNLOADING


@pytest.mark.asyncio
async def test_auto_advance_promotes_ignored_season_to_searching(
    db_session: AsyncSession,
) -> None:
    """An IGNORED season (unrequested on Seerr) is promoted to SEARCHING when auto-advance reaches it."""
    from tests.conftest import make_series

    item, seasons, _ = await make_series(
        db_session,
        seasons=[
            {"season_number": 1, "status": SeasonStatus.COMPLETED},
            {"season_number": 2, "status": SeasonStatus.IGNORED},
        ],
        episodes_per_season=1,
        episode_statuses=None,
    )
    seasons[1].air_date = datetime(2020, 1, 1, tzinfo=timezone.utc)
    await db_session.commit()

    with patch(
        "app.core.push_engine._push_single_season_or_movie_entry",
        new=AsyncMock(return_value=[]),
    ):
        await advance_season_buffer(db_session, item, base_watch_order=1)

    await db_session.refresh(seasons[1])
    assert seasons[1].status == SeasonStatus.SEARCHING
