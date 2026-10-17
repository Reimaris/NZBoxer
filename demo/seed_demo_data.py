"""Build a fully fictional demo database for README screenshots.

Usage (from the repo root):  .venv/bin/python demo/seed_demo_data.py [--out demo/config]

Writes <out>/nzboxer.db from scratch with the app's own schema/migrations. All content
comes from demo/catalog.py, so it is safe to publish. API keys are obvious placeholders and
every provider syncs manually only, so the demo never contacts a real service.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog  # noqa: E402

PUBLIC = os.environ.get("DEMO_PUBLIC_URL", "http://localhost:9100")
INDEXER_URL = "http://127.0.0.1:9100/newznab/api"
NOW = datetime.now(timezone.utc)


# (progress %, bytes/s, eta seconds) for the in-flight transfers, in seeding order
LIVE_TRANSFERS = [(27.0, 14_200_000, 412), (62.0, 38_500_000, 95)]


def poster(tmdb: int) -> str:
    return f"{PUBLIC}/poster/{tmdb}.svg"


async def seed(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    db_file = out / "nzboxer.db"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_file}{suffix}").unlink(missing_ok=True)

    from app.db import database
    from app.db.models import (
        DownloadHistory,
        Episode,
        EpisodeStatus,
        MediaItem,
        MediaItemWatchlistSource,
        MediaStatus,
        MediaType,
        Provider,
        SearchPreset,
        Season,
        SeasonStatus,
        SystemSettings,
    )

    await database.init_db(f"sqlite+aiosqlite:///{db_file}")
    async with database.async_session_factory() as s:
        # --- Providers -----------------------------------------------------
        def prov(**kw):
            p = Provider(**{"is_active": True, "priority": 1, **kw})
            s.add(p)
            return p

        manual = {"sync_interval_minutes": 0}
        simkl = prov(
            category="watchlist",
            type="simkl",
            name="Simkl",
            client_id="demo-client-id",
            username="demo-user",
            access_token="demo-token",
            config_json=json.dumps(
                {
                    **manual,
                    "client_id": "demo-client-id",
                    "refresh_token": "demo-refresh",
                }
            ),
        )
        anilist = prov(
            category="watchlist",
            type="anilist",
            name="AniList",
            username="demo-user",
            config_json=json.dumps(manual),
        )
        prov(
            category="watchlist",
            type="jellyseerr",
            name="Jellyseerr",
            config_json=json.dumps(
                {
                    **manual,
                    "server_url": "http://jellyseerr.demo.invalid:5055",
                    "api_key": "demo-api-key",
                    "sync_movies": True,
                    "sync_series": True,
                    "notify_available": True,
                    "sync_admin_watchlist": False,
                }
            ),
        )
        prov(category="metadata", type="tmdb", name="TMDB", api_key="demo-tmdb-key")
        prov(
            category="downloader",
            type="torbox",
            name="TorBox",
            api_key="demo-torbox-key",
            api_url="http://127.0.0.1:9100/torbox/v1/api",
        )
        prov(
            category="downloader",
            type="sabnzbd",
            name="SABnzbd",
            api_key="demo-sab-key",
            api_url="http://sabnzbd.demo.invalid:8080/sabnzbd/api",
            is_active=False,
            config_json=json.dumps(
                {
                    "category_movie": "movies",
                    "category_series": "tv",
                    "category_anime": "anime",
                }
            ),
        )
        prov(
            category="indexer",
            type="newznab",
            name="Demo Indexer",
            api_key="demo-indexer-key",
            api_url=INDEXER_URL,
        )
        await s.flush()

        # --- Settings & presets --------------------------------------------
        settings = await s.get(SystemSettings, 1)
        if settings is None:
            settings = SystemSettings(id=1)
            s.add(settings)
        settings.onboarding_completed = True
        settings.dry_run = True
        preset = (
            await s.execute(
                SearchPreset.__table__.select().where(SearchPreset.is_default)
            )
        ).first()
        if preset is None:
            s.add(SearchPreset(name="Default (Best)", is_default=True))
        await s.flush()

        # --- Library -------------------------------------------------------
        def link(item: MediaItem, provider: Provider, status: str, order: int) -> None:
            s.add(
                MediaItemWatchlistSource(
                    media_item_id=item.id,
                    provider_id=provider.id,
                    remote_id=str(item.simkl_id),
                    list_status=status,
                    last_watched_order=order,
                    last_synced_at=NOW,
                )
            )

        def new_item(provider: Provider, **kw) -> MediaItem:
            tmdb = kw["tmdb_id"]
            item = MediaItem(
                provider_id=provider.id,
                simkl_id=tmdb,
                poster_url=poster(tmdb),
                overview=f"{kw['title']} is a fictional demo title.",
                watchlist_synced_at=NOW,
                **kw,
            )
            s.add(item)
            return item

        history_targets: list[tuple[MediaItem, Season | None, Episode | None]] = []
        # Finished titles are COMPLETED: startup maintenance resets a bare DOWNLOADED
        # (a push awaiting confirmation) back to SEARCHING.
        status_map = {
            "downloaded": (
                MediaStatus.COMPLETED,
                SeasonStatus.COMPLETED,
                EpisodeStatus.COMPLETED,
            ),
            "searching": (
                MediaStatus.SEARCHING,
                SeasonStatus.SEARCHING,
                EpisodeStatus.SEARCHING,
            ),
            "future": (MediaStatus.FUTURE, SeasonStatus.FUTURE, EpisodeStatus.FUTURE),
            "downloading": (
                MediaStatus.DOWNLOADING,
                SeasonStatus.DOWNLOADING,
                EpisodeStatus.DOWNLOADING,
            ),
        }

        for m in catalog.MOVIES:
            state = "searching" if m["state"] == "ready" else m["state"]
            future = state == "future"
            item = new_item(
                simkl,
                tmdb_id=m["tmdb"],
                title=m["title"],
                year=m["year"],
                media_type=MediaType.MOVIE,
                status=status_map[state][0],
                release_date=(NOW + timedelta(days=75))
                if future
                else NOW - timedelta(days=40 + (m["tmdb"] % 5) * 23),
            )
            await s.flush()
            link(item, simkl, "plantowatch", 0)
            if state in ("downloaded", "downloading"):
                history_targets.append((item, None, None))

        async def add_seasons(item, specs, watched, *, entries=None):
            """specs: list of (entry_type, title, status)."""
            seasons = []
            for idx, (etype, title, status) in enumerate(specs, start=1):
                future = status == "future"
                season = Season(
                    media_item_id=item.id,
                    season_number=idx,
                    watch_order=idx,
                    type_number=idx,
                    entry_type=etype,
                    title=title,
                    title_english=title,
                    title_romaji=title,
                    monitored=True,
                    status=status_map[status][1],
                    episode_count=None if etype == "movie" else 8,
                    air_date=(NOW + timedelta(days=90 + idx))
                    if future
                    else NOW
                    - timedelta(
                        days=max(30, 400 - idx * 90 - (item.tmdb_id % 7) * 31)
                    ),  # released seasons always dated in the past
                )
                s.add(season)
                seasons.append(season)
            await s.flush()
            for season, (etype, _t, status) in zip(seasons, specs):
                if etype == "movie":
                    continue
                for e in range(1, 9):
                    # transfers are counted per downloading entity; keep episodes of an in-flight season out of it
                    ep_status = status_map[
                        "searching" if status == "downloading" else status
                    ][2]
                    s.add(
                        Episode(
                            season_id=season.id,
                            episode_number=e,
                            monitored=True,
                            status=ep_status,
                            air_date=season.air_date + timedelta(days=7 * e)
                            if season.air_date
                            else None,
                        )
                    )
            await s.flush()
            return seasons

        for sh in catalog.SHOWS:
            specs = []
            for i, st in enumerate(sh["seasons"], start=1):
                if sh.get("downloading_season") == i:
                    st = "downloading"
                specs.append(("season", f"Season {i}", st))
            item = new_item(
                simkl,
                tmdb_id=sh["tmdb"],
                title=sh["title"],
                year=sh["year"],
                media_type=MediaType.SHOW,
                status=MediaStatus.SEARCHING,
                auto_advance_seasons=sh.get("auto_advance", False),
                last_watched_order=sh["watched"],
            )
            await s.flush()
            seasons = await add_seasons(item, specs, sh["watched"])
            link(
                item,
                simkl,
                sh.get("list_status", "watching" if sh["watched"] else "plantowatch"),
                sh["watched"],
            )
            for season, spec in zip(seasons, specs):
                if spec[2] in ("downloaded", "downloading"):
                    history_targets.append((item, season, None))

        for an in catalog.ANIME:
            item = new_item(
                anilist,
                tmdb_id=an["tmdb"],
                title=an["title"],
                title_english=an["title"],
                title_romaji=an["title"],
                year=an["year"],
                media_type=MediaType.ANIME,
                status=MediaStatus.SEARCHING,
                anilist_id=an["anilist"],
                last_watched_order=an["watched"],
                auto_advance_seasons=True,
            )
            await s.flush()
            seasons = await add_seasons(item, an["entries"], an["watched"])
            link(
                item,
                anilist,
                "watching" if an["watched"] else "plantowatch",
                an["watched"],
            )
            for season, spec in zip(seasons, an["entries"]):
                if spec[2] == "downloaded":
                    history_targets.append((item, season, None))

        await s.flush()

        # --- Download history ------------------------------------------------
        def rel_name(item: MediaItem, season: Season | None, grp: str) -> str:
            base = "".join(c if c.isalnum() else "." for c in item.title).replace(
                "..", "."
            )
            if season is None:
                return f"{base}.{item.year}.1080p.WEB-DL.DDP5.1.H264-{grp}"
            return f"{base}.S{season.season_number:02d}.1080p.WEB-DL.DDP5.1.H264-{grp}"

        def hist(item, season, *, grp="NOVA", detail="completed", age_h=30, **kw):
            when = NOW - timedelta(hours=age_h)
            row = DownloadHistory(
                media_item_id=item.id,
                season_id=season.id if season else None,
                media_title=item.title,
                media_year=item.year,
                media_type_label={"movie": "MOVIE", "show": "SERIES", "anime": "ANIME"}[
                    item.media_type.value
                ],
                target_label=f"Season {season.season_number}" if season else None,
                poster_url=item.poster_url,
                tmdb_id=item.tmdb_id,
                nzb_title=rel_name(item, season, grp),
                nzb_guid=f"demo-{item.id}-{season.id if season else 0}-{grp}-{age_h}",
                downloader_type="torbox",
                downloader_transfer_id=f"demo-{item.id}-{age_h}",
                score=8650.0,
                size_bytes=int((4.6 if season is None else 22.0) * 1024**3),
                resolution="1080p",
                video_codec="H264",
                audio_codec="DDP5.1",
                source="WEB-DL",
                release_group=grp,
                push_mode="auto",
                status_detail=detail,
                dispatched_at=when,
                created_at=when,
                **{"is_dismissed": True, **kw},
            )
            s.add(row)
            return row

        live_idx = 0
        for item, season, _ in history_targets:
            if item.status == MediaStatus.DOWNLOADING or (
                season and season.status == SeasonStatus.DOWNLOADING
            ):
                # Real in-flight transfers (dry-run never shows live progress); the mock
                # TorBox reports these same values back to the poller.
                live = LIVE_TRANSFERS[min(live_idx, len(LIVE_TRANSFERS) - 1)]
                live_idx += 1
                hist(
                    item,
                    season,
                    detail="downloading",
                    age_h=0,
                    is_dismissed=False,
                    progress_pct=live[0],
                    download_speed_bytes=live[1],
                    eta_seconds=live[2],
                )
            else:
                hist(item, season, completed_at=NOW - timedelta(hours=29))

        # extra ledger variety: failed, canceled, replaced, simulated
        pool = [t for t in history_targets if t[1] is not None] or history_targets
        item, season, _ = pool[0]
        hist(
            item,
            season,
            grp="TIDE",
            detail="failed: stalled transfer",
            age_h=80,
            failed_at=NOW - timedelta(hours=79),
        )
        hist(item, season, grp="FORGE", detail="canceled", age_h=70)
        hist(item, season, grp="LUMEN", detail="replaced", age_h=60)
        hist(
            item,
            season,
            grp="NOVA",
            detail="completed",
            age_h=5,
            is_dry_run=True,
        )

        await s.commit()
    await database.close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "demo" / "config")
    args = parser.parse_args()
    asyncio.run(seed(args.out.resolve()))
    print(f"Demo database written to {args.out.resolve() / 'nzboxer.db'}")


if __name__ == "__main__":
    main()
