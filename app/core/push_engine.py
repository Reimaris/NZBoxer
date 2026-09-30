"""
On-Demand Search & Push Engine (v3.0.0)
=======================================
Powers the Unified Push Modal with two execution modes:
1. Auto-Push Best (`push_mode = "auto"`):
   - Sticky search preference persistence on MediaItem
   - Multi-indexer search with Preset / Custom Scorer evaluation
   - Layer 1 & Layer 2 fake detection
   - Automatic Pack-to-Episode fallback when `prefer_season_packs` is ON
   - Franchise Movie category routing (`entry_type == "movie"`)
   - Season Expansion & Movie Bridge Lookahead when `auto_advance_seasons` is ON
2. Search & Pick Manually (`push_mode = "manual"`):
   - Returns scored candidates partitioned into Season Packs / Movie Releases
     and Episode Accordions with 3-tier language grouping (`primary`, `fallback`, `mismatched`)
   - 1-click Manual Grab dispatch to TorBox with `push_mode = "manual"`
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.fake_detector import is_nzb_content_fake
from app.core.parser import parse_release_name
from app.core.scorer import score_release
from app.db.models import (
    BlacklistedRelease,
    DownloadHistory,
    Episode,
    EpisodeStatus,
    MediaItem,
    MediaStatus,
    MediaType,
    Season,
    SeasonStatus,
)
from app.services import torbox, treasure_maps
from app.services.preset_service import (
    CUSTOM_CONFIG_KEYS,
    extract_custom_config_json,
    get_default_preset,
    get_preset,
    list_presets,
    update_item_sticky_search_config,
)
from app.services.provider_service import get_active_indexers

logger = logging.getLogger(__name__)


def _parse_int_list(val: Any) -> list[int]:
    if val is None:
        return []
    if isinstance(val, int):
        return [val]
    if isinstance(val, str):
        out: list[int] = []
        for part in val.split(","):
            p = part.strip()
            if p:
                try:
                    out.append(int(p))
                except ValueError:
                    pass
        return out
    if isinstance(val, (list, tuple, set)):
        res: list[int] = []
        for x in val:
            try:
                res.append(int(x))
            except (TypeError, ValueError):
                pass
        return res
    return []


async def resolve_effective_search_config(
    session: AsyncSession,
    item: MediaItem,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the active search & scoring configuration from payload, sticky item config, or preset."""
    payload = payload or {}

    # 1. Resolve preset
    preset_id: int | None = item.preset_id
    if "preset_id" in payload:
        raw_pid = payload.get("preset_id")
        if raw_pid in (None, "", "custom", "none"):
            preset_id = None
        else:
            try:
                preset_id = int(raw_pid)
            except (TypeError, ValueError):
                preset_id = None

    preset = await get_preset(session, preset_id) if preset_id else None
    if preset is None and not item.custom_search_config_json and not payload:
        preset = await get_default_preset(session)

    # 2. Base values from preset
    primary_language: str | None = preset.primary_language if preset else "en"
    fallback_language: str | None = preset.fallback_language if preset else None
    video_quality_mode: str = preset.video_quality_mode if preset else "best"
    audio_quality_mode: str = preset.audio_quality_mode if preset else "best"
    custom_config: dict[str, Any] = dict(preset.custom_config) if preset else {}

    # 3. Overlay item sticky custom_search_config_json if present
    if item.custom_search_config_json:
        try:
            sticky_cfg = json.loads(item.custom_search_config_json)
            if isinstance(sticky_cfg, dict):
                if sticky_cfg.get("primary_language") is not None:
                    primary_language = sticky_cfg.get("primary_language")
                if "fallback_language" in sticky_cfg:
                    fallback_language = sticky_cfg.get("fallback_language")
                if sticky_cfg.get("video_quality_mode"):
                    video_quality_mode = str(sticky_cfg["video_quality_mode"])
                if sticky_cfg.get("audio_quality_mode"):
                    audio_quality_mode = str(sticky_cfg["audio_quality_mode"])
                for k in CUSTOM_CONFIG_KEYS:
                    if k in sticky_cfg and sticky_cfg[k] is not None:
                        custom_config[k] = sticky_cfg[k]
        except Exception:
            pass

    # 4. Overlay explicit payload parameters
    if payload.get("primary_language") is not None:
        primary_language = str(payload["primary_language"]).strip() or "en"
    if "fallback_language" in payload:
        fb = payload.get("fallback_language")
        fallback_language = (
            None
            if fb is None or str(fb).strip().lower() in ("", "none", "null")
            else str(fb).strip()
        )
    if payload.get("video_quality_mode"):
        video_quality_mode = str(payload["video_quality_mode"]).strip().lower()
    if payload.get("audio_quality_mode"):
        audio_quality_mode = str(payload["audio_quality_mode"]).strip().lower()

    if (
        "custom_config" in payload
        or "custom_config_json" in payload
        or any(k in payload for k in CUSTOM_CONFIG_KEYS)
    ):
        extracted_str = extract_custom_config_json(payload)
        try:
            extracted_dict = json.loads(extracted_str)
            if isinstance(extracted_dict, dict):
                custom_config.update(extracted_dict)
        except Exception:
            pass

    prefer_season_packs = bool(item.prefer_season_packs)
    if "prefer_season_packs" in payload and payload["prefer_season_packs"] is not None:
        val = payload["prefer_season_packs"]
        prefer_season_packs = (
            val
            if isinstance(val, bool)
            else str(val).strip().lower() in ("1", "true", "yes", "on")
        )

    auto_advance_seasons = bool(item.auto_advance_seasons)
    if (
        "auto_advance_seasons" in payload
        and payload["auto_advance_seasons"] is not None
    ):
        val = payload["auto_advance_seasons"]
        auto_advance_seasons = (
            val
            if isinstance(val, bool)
            else str(val).strip().lower() in ("1", "true", "yes", "on")
        )

    return {
        "preset_id": preset.id if preset else preset_id,
        "primary_language": primary_language,
        "fallback_language": fallback_language,
        "video_quality_mode": video_quality_mode,
        "audio_quality_mode": audio_quality_mode,
        "custom_config": custom_config,
        "prefer_season_packs": prefer_season_packs,
        "auto_advance_seasons": auto_advance_seasons,
    }


async def persist_sticky_preferences(
    session: AsyncSession, item: MediaItem, payload: dict[str, Any]
) -> None:
    """Persist sticky search configuration from a Push Modal request onto MediaItem."""
    sticky_payload: dict[str, Any] = {}
    if "preset_id" in payload:
        sticky_payload["preset_id"] = payload["preset_id"]
    if "prefer_season_packs" in payload:
        sticky_payload["prefer_season_packs"] = payload["prefer_season_packs"]
    if "auto_advance_seasons" in payload:
        sticky_payload["auto_advance_seasons"] = payload["auto_advance_seasons"]

    has_custom_overrides = (
        "custom_search_config_json" in payload
        or "custom_config" in payload
        or "custom_config_json" in payload
        or "video_quality_mode" in payload
        or "audio_quality_mode" in payload
        or any(k in payload for k in CUSTOM_CONFIG_KEYS)
    )
    if has_custom_overrides and "preset_id" not in payload:
        custom_str = extract_custom_config_json(payload)
        try:
            custom_dict = json.loads(custom_str)
        except Exception:
            custom_dict = {}
        for meta_key in (
            "primary_language",
            "fallback_language",
            "video_quality_mode",
            "audio_quality_mode",
        ):
            if meta_key in payload and payload[meta_key] is not None:
                custom_dict[meta_key] = payload[meta_key]
        sticky_payload["custom_search_config_json"] = json.dumps(custom_dict)
    elif "custom_search_config_json" in payload:
        sticky_payload["custom_search_config_json"] = payload[
            "custom_search_config_json"
        ]

    if sticky_payload:
        await update_item_sticky_search_config(session, item.id, sticky_payload)


def _score_and_partition_candidates(
    raw_results: list[dict[str, Any]],
    blacklisted_guids: set[str],
    blacklisted_titles: set[str],
    expected_title: str | None,
    expected_year: int | None,
    expected_alt_title: str | None,
    expected_season: int | None,
    expected_episode: int | None,
    expected_season_title: str | None,
    runtime_minutes: int | None,
    effective_cfg: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Score raw indexer results and partition them into primary, fallback, mismatched, and all_valid."""
    primary_list: list[dict[str, Any]] = []
    fallback_list: list[dict[str, Any]] = []
    mismatched_list: list[dict[str, Any]] = []

    seen_guids: set[str] = set()

    primary_lang = effective_cfg.get("primary_language")
    fallback_lang = effective_cfg.get("fallback_language")
    v_mode = effective_cfg.get("video_quality_mode", "best")
    a_mode = effective_cfg.get("audio_quality_mode", "best")
    custom_cfg = effective_cfg.get("custom_config") or {}

    for raw in raw_results:
        title = str(raw.get("title") or "")
        guid = str(raw.get("guid") or raw.get("link") or "")
        size_bytes = int(raw.get("size") or 0)

        if not title or not guid:
            continue
        if guid in seen_guids:
            continue
        seen_guids.add(guid)

        if guid in blacklisted_guids or title in blacklisted_titles:
            continue

        parsed = parse_release_name(title)
        score_res = score_release(
            parsed=parsed,
            size_bytes=size_bytes,
            runtime_minutes=runtime_minutes,
            expected_title=expected_title,
            expected_year=expected_year,
            expected_alt_title=expected_alt_title,
            expected_season=expected_season,
            expected_episode=expected_episode,
            expected_season_title=expected_season_title,
            primary_language=primary_lang,
            fallback_language=fallback_lang,
            api_language=raw.get("api_language"),
            video_quality_mode=v_mode,
            audio_quality_mode=a_mode,
            custom_config=custom_cfg,
        )

        entry = {
            "title": title,
            "guid": guid,
            "size_bytes": size_bytes,
            "size_gb": round(size_bytes / (1024**3), 2),
            "parsed": parsed,
            "resolution": parsed.resolution,
            "source": parsed.source,
            "video_codec": parsed.video_codec,
            "audio_codec": parsed.audio_codec,
            "release_group": parsed.release_group,
            "indexer_name": raw.get("_indexer_name"),
            "indexer_url": raw.get("_indexer_url"),
            "indexer_key": raw.get("_indexer_key"),
        }

        if not score_res.is_rejected:
            entry["score"] = round(score_res.score, 1)
            entry["score_res"] = score_res
            entry["is_primary"] = score_res.is_primary
            entry["is_fallback"] = score_res.is_fallback
            entry["matched_language"] = score_res.matched_language
            if score_res.is_primary:
                primary_list.append(entry)
            else:
                fallback_list.append(entry)
        elif score_res.reject_reason and score_res.reject_reason.startswith(
            "Language mismatch:"
        ):
            # Evaluate without language restriction to see if it qualifies for the Mismatched tier
            any_lang_res = score_release(
                parsed=parsed,
                size_bytes=size_bytes,
                runtime_minutes=runtime_minutes,
                expected_title=expected_title,
                expected_year=expected_year,
                expected_alt_title=expected_alt_title,
                expected_season=expected_season,
                expected_episode=expected_episode,
                expected_season_title=expected_season_title,
                primary_language="any",
                fallback_language=None,
                api_language=raw.get("api_language"),
                video_quality_mode=v_mode,
                audio_quality_mode=a_mode,
                custom_config=custom_cfg,
            )
            if not any_lang_res.is_rejected:
                entry["score"] = round(any_lang_res.score, 1)
                entry["score_res"] = any_lang_res
                entry["is_primary"] = False
                entry["is_fallback"] = False
                entry["matched_language"] = any_lang_res.matched_language
                mismatched_list.append(entry)

    primary_list.sort(key=lambda x: x["score"], reverse=True)
    fallback_list.sort(key=lambda x: x["score"], reverse=True)
    mismatched_list.sort(key=lambda x: x["score"], reverse=True)

    all_valid = primary_list + fallback_list
    return {
        "primary": primary_list,
        "fallback": fallback_list,
        "mismatched": mismatched_list,
        "all_valid": all_valid,
    }


def _serialize_candidate(cand: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-serializable candidate dictionary (stripping internal dataclasses)."""
    return {k: v for k, v in cand.items() if k not in ("parsed", "score_res")}


async def _query_movie_across_indexers(
    session: AsyncSession,
    item: MediaItem,
    title_override: str | None = None,
    use_external_ids: bool = True,
) -> list[dict[str, Any]]:
    """Query active indexers for a standalone movie or franchise movie entry."""
    cat_id = (
        item.provider.movie_category_id
        if item.provider and item.provider.movie_category_id
        else 2000
    )
    search_title = title_override or item.title
    imdb_id = item.imdb_id if use_external_ids else None
    tmdb_id = item.tmdb_id if use_external_ids else None

    indexers = await get_active_indexers(session)
    all_results: list[dict[str, Any]] = []

    if indexers:
        for idx in indexers:
            try:
                res = await treasure_maps.search_movie(
                    imdb_id=imdb_id,
                    tmdb_id=tmdb_id,
                    title=search_title,
                    category=cat_id,
                    api_url=idx.api_url,
                    api_key=idx.api_key,
                    session=session,
                )
                for r in res:
                    r["_indexer_name"] = idx.name
                    r["_indexer_url"] = idx.api_url
                    r["_indexer_key"] = idx.api_key
                all_results.extend(res)
            except Exception as exc:
                logger.warning("Indexer '%s' movie search error: %s", idx.name, exc)
    else:
        res = await treasure_maps.search_movie(
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            title=search_title,
            category=cat_id,
            session=session,
        )
        all_results.extend(res)

    return all_results


async def _query_show_across_indexers(
    session: AsyncSession,
    item: MediaItem,
    season_number: int | None,
    episode_number: int | None = None,
) -> list[dict[str, Any]]:
    """Query active indexers for a TV/Anime season pack or episode."""
    if item.media_type == MediaType.ANIME:
        cat_id = (
            item.provider.anime_category_id
            if item.provider and item.provider.anime_category_id
            else 5070
        )
    else:
        cat_id = (
            item.provider.series_category_id
            if item.provider and item.provider.series_category_id
            else 5000
        )

    ep_str = str(episode_number) if episode_number is not None else None
    indexers = await get_active_indexers(session)
    all_results: list[dict[str, Any]] = []

    if indexers:
        for idx in indexers:
            try:
                res = await treasure_maps.search_show(
                    tvdb_id=item.tvdb_id,
                    tmdb_id=item.tmdb_id,
                    imdb_id=item.imdb_id,
                    title=item.title,
                    season=season_number,
                    ep=ep_str,
                    category=cat_id,
                    api_url=idx.api_url,
                    api_key=idx.api_key,
                    session=session,
                )
                for r in res:
                    r["_indexer_name"] = idx.name
                    r["_indexer_url"] = idx.api_url
                    r["_indexer_key"] = idx.api_key
                all_results.extend(res)
            except Exception as exc:
                logger.warning("Indexer '%s' show search error: %s", idx.name, exc)
    else:
        res = await treasure_maps.search_show(
            tvdb_id=item.tvdb_id,
            tmdb_id=item.tmdb_id,
            imdb_id=item.imdb_id,
            title=item.title,
            season=season_number,
            ep=ep_str,
            category=cat_id,
            session=session,
        )
        all_results.extend(res)

    return all_results


async def _dispatch_candidate_list_to_torbox(
    session: AsyncSession,
    candidates: list[dict[str, Any]],
    item: MediaItem,
    season: Season | None = None,
    episode: Episode | None = None,
    push_mode: str = "auto",
    event_type: str = "push_initiated",
    status_reason: str | None = None,
) -> DownloadHistory | None:
    """Iterate through scored candidates, verify Layer 2 NZB fake check, and dispatch to TorBox."""
    from app.core.automation import increment_today_grab_count
    from app.services import discord

    is_movie = episode is None and (
        season is None or getattr(season, "entry_type", "season") == "movie"
    )
    media_type_str = "movie" if is_movie else "episode"

    for cand in candidates:
        guid = cand["guid"]
        title = cand["title"]
        try:
            nzb_bytes, filename = await treasure_maps.fetch_nzb_bytes(
                guid,
                api_url=cand.get("indexer_url"),
                api_key=cand.get("indexer_key"),
                session=session,
            )
        except Exception as exc:
            logger.warning("Failed fetching NZB bytes for '%s': %s", title, exc)
            continue

        is_fake, fake_reason = is_nzb_content_fake(nzb_bytes, media_type=media_type_str)
        if is_fake:
            err_msg = f"NZB flagged as fake/executable: {fake_reason}"
            logger.warning("Rejected fake NZB '%s': %s", title, err_msg)
            bl = BlacklistedRelease(
                media_item_id=item.id,
                nzb_guid=guid,
                nzb_title=title,
                reason=err_msg,
            )
            session.add(bl)
            await session.flush()
            continue

        torbox_result = await torbox.send_nzb_file(
            nzb_bytes, filename=filename, session=session, is_manual=True
        )
        if not torbox_result or (
            not torbox_result.get("hash") and not torbox_result.get("id")
        ):
            logger.warning("TorBox dispatch failed for '%s': %s", title, torbox_result)
            continue

        await increment_today_grab_count(session)
        parsed = cand.get("parsed")
        score_res = cand.get("score_res")

        history = DownloadHistory(
            media_item_id=item.id,
            season_id=season.id if season else (episode.season_id if episode else None),
            episode_id=episode.id if episode else None,
            nzb_title=title,
            nzb_guid=guid,
            score=cand.get("score"),
            size_bytes=cand.get("size_bytes"),
            resolution=parsed.resolution if parsed else cand.get("resolution"),
            video_codec=parsed.video_codec if parsed else cand.get("video_codec"),
            audio_codec=parsed.audio_codec if parsed else cand.get("audio_codec"),
            source=parsed.source if parsed else cand.get("source"),
            release_group=parsed.release_group if parsed else cand.get("release_group"),
            bitrate_mbps=score_res.bitrate_mbps if score_res else None,
            torbox_hash=str(torbox_result.get("hash"))
            if torbox_result.get("hash")
            else None,
            torbox_id=str(torbox_result.get("id")) if torbox_result.get("id") else None,
            torbox_sent_at=datetime.now(timezone.utc),
            is_fallback=bool(cand.get("is_fallback", False)),
            grabbed_language=cand.get("matched_language"),
            push_mode=push_mode,
        )
        session.add(history)

        if episode is not None:
            episode.status = EpisodeStatus.DOWNLOADING
            episode.fail_count = 0
            episode.last_error = None
        elif season is not None:
            season.status = SeasonStatus.DOWNLOADING
            season.fail_count = 0
            season.last_error = None
            for ep in season.episodes:
                if ep.status != EpisodeStatus.FUTURE:
                    ep.status = EpisodeStatus.DOWNLOADING
                    ep.fail_count = 0
                    ep.last_error = None
        else:
            item.status = MediaStatus.DOWNLOADING
            item.fail_count = 0
            item.last_error = None

        await session.flush()

        from app.core.transfer_poller import wake_transfer_poller

        wake_transfer_poller()

        target_lbl = discord.format_target_label(
            item=item, season=season, episode=episode
        )
        default_reason = (
            "Auto-Advance Season Expansion triggered"
            if event_type == "auto_advance"
            else (
                "Auto-Push Best dispatched to TorBox"
                if push_mode == "auto"
                else "Manual Pick dispatched to TorBox"
            )
        )
        await discord.dispatch_notification_event(
            session=session,
            event_type=event_type,
            media_title=item.title,
            media_year=item.year,
            target_label=target_lbl,
            release_name=title,
            resolution=history.resolution,
            source=history.source,
            video_codec=history.video_codec,
            audio_codec=history.audio_codec,
            language=history.grabbed_language,
            status_reason=status_reason or default_reason,
            poster_url=item.poster_url,
        )
        return history

    return None


async def _load_blacklisted_sets(
    session: AsyncSession, media_item_id: int
) -> tuple[set[str], set[str]]:
    stmt = select(BlacklistedRelease).where(
        BlacklistedRelease.media_item_id == media_item_id
    )
    rows = (await session.execute(stmt)).scalars().all()
    guids = {r.nzb_guid for r in rows if r.nzb_guid}
    titles = {r.nzb_title for r in rows if r.nzb_title}
    return guids, titles


async def execute_auto_push(
    session: AsyncSession,
    item_id: int,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute Auto-Push Best (`push_mode = 'auto'`) for a MediaItem and its selected seasons/episodes."""
    payload = payload or {}
    stmt = (
        select(MediaItem)
        .where(MediaItem.id == item_id)
        .options(
            selectinload(MediaItem.provider),
            selectinload(MediaItem.seasons).selectinload(Season.episodes),
        )
    )
    item = (await session.execute(stmt)).scalars().first()
    if item is None:
        return {"pushed": False, "error": "MediaItem not found", "status_code": 404}

    # 1. Persist sticky search preferences on the MediaItem
    await persist_sticky_preferences(session, item, payload)

    # 2. Resolve effective scoring config
    effective_cfg = await resolve_effective_search_config(session, item, payload)
    prefer_season_packs = bool(effective_cfg["prefer_season_packs"])
    auto_advance_seasons = bool(effective_cfg["auto_advance_seasons"])

    blacklisted_guids, blacklisted_titles = await _load_blacklisted_sets(
        session, item.id
    )

    season_ids = _parse_int_list(
        payload.get("season_ids")
        if "season_ids" in payload
        else payload.get("season_ids[]")
    )
    episode_ids = _parse_int_list(
        payload.get("episode_ids")
        if "episode_ids" in payload
        else payload.get("episode_ids[]")
    )

    dispatched_histories: list[DownloadHistory] = []
    pushed_seasons: list[Season] = []

    async def _push_single_season_or_movie_entry(
        season: Season, is_auto_advance: bool = False
    ) -> bool:
        ev_type = "auto_advance" if is_auto_advance else "push_initiated"
        entry_type = getattr(season, "entry_type", "season") or "season"
        if entry_type == "movie":
            raw_results = await _query_movie_across_indexers(
                session,
                item,
                title_override=season.title or item.title,
                use_external_ids=False,
            )
            partitioned = _score_and_partition_candidates(
                raw_results=raw_results,
                blacklisted_guids=blacklisted_guids,
                blacklisted_titles=blacklisted_titles,
                expected_title=season.title or item.title,
                expected_year=season.air_date.year if season.air_date else None,
                expected_alt_title=item.title,
                expected_season=None,
                expected_episode=None,
                expected_season_title=season.title,
                runtime_minutes=item.runtime_minutes,
                effective_cfg=effective_cfg,
            )
            hist = await _dispatch_candidate_list_to_torbox(
                session,
                partitioned["all_valid"],
                item=item,
                season=season,
                episode=None,
                push_mode="auto",
                event_type=ev_type,
            )
            if hist is not None:
                dispatched_histories.append(hist)
                pushed_seasons.append(season)
                return True
            return False

        # TV / Anime Season entry (`entry_type == "season"`)
        effective_s_num = int(season.type_number or season.season_number)
        pack_grabbed = False
        if prefer_season_packs:
            raw_pack_results = await _query_show_across_indexers(
                session, item, season_number=effective_s_num, episode_number=None
            )
            pack_partitioned = _score_and_partition_candidates(
                raw_results=raw_pack_results,
                blacklisted_guids=blacklisted_guids,
                blacklisted_titles=blacklisted_titles,
                expected_title=item.title,
                expected_year=None,
                expected_alt_title=item.alt_title,
                expected_season=effective_s_num,
                expected_episode=None,
                expected_season_title=(
                    season.title
                    if season.title and season.title != item.title
                    else None
                ),
                runtime_minutes=None,
                effective_cfg=effective_cfg,
            )
            hist = await _dispatch_candidate_list_to_torbox(
                session,
                pack_partitioned["all_valid"],
                item=item,
                season=season,
                episode=None,
                push_mode="auto",
                event_type=ev_type,
            )
            if hist is not None:
                dispatched_histories.append(hist)
                pushed_seasons.append(season)
                pack_grabbed = True

        if pack_grabbed:
            return True

        # Pack-to-Episode Fallback (or when prefer_season_packs is False)
        any_ep_grabbed = False
        sorted_eps = sorted(season.episodes, key=lambda e: e.episode_number)
        for ep in sorted_eps:
            if ep.status in (
                EpisodeStatus.DOWNLOADED,
                EpisodeStatus.COMPLETED,
                EpisodeStatus.DOWNLOADING,
                EpisodeStatus.FUTURE,
            ):
                continue
            raw_ep_results = await _query_show_across_indexers(
                session,
                item,
                season_number=effective_s_num,
                episode_number=ep.episode_number,
            )
            ep_partitioned = _score_and_partition_candidates(
                raw_results=raw_ep_results,
                blacklisted_guids=blacklisted_guids,
                blacklisted_titles=blacklisted_titles,
                expected_title=item.title,
                expected_year=None,
                expected_alt_title=item.alt_title,
                expected_season=effective_s_num,
                expected_episode=ep.episode_number,
                expected_season_title=(
                    season.title
                    if season.title and season.title != item.title
                    else None
                ),
                runtime_minutes=None,
                effective_cfg=effective_cfg,
            )
            ep_hist = await _dispatch_candidate_list_to_torbox(
                session,
                ep_partitioned["all_valid"],
                item=item,
                season=season,
                episode=ep,
                push_mode="auto",
                event_type=ev_type,
            )
            if ep_hist is not None:
                dispatched_histories.append(ep_hist)
                any_ep_grabbed = True

        if any_ep_grabbed:
            pushed_seasons.append(season)
        return any_ep_grabbed

    # Case 1: Standalone Movie (or movie item with no seasons)
    if item.media_type == MediaType.MOVIE and not season_ids and not episode_ids:
        raw_movie_results = await _query_movie_across_indexers(session, item)
        movie_partitioned = _score_and_partition_candidates(
            raw_results=raw_movie_results,
            blacklisted_guids=blacklisted_guids,
            blacklisted_titles=blacklisted_titles,
            expected_title=item.title,
            expected_year=item.year,
            expected_alt_title=item.alt_title,
            expected_season=None,
            expected_episode=None,
            expected_season_title=None,
            runtime_minutes=item.runtime_minutes,
            effective_cfg=effective_cfg,
        )
        hist = await _dispatch_candidate_list_to_torbox(
            session,
            movie_partitioned["all_valid"],
            item=item,
            season=None,
            episode=None,
            push_mode="auto",
            event_type="push_initiated",
        )
        if hist is not None:
            dispatched_histories.append(hist)

    # Case 2: Series / Anime selected seasons (or default to first monitored/searching season if none specified)
    else:
        seasons_by_id = {s.id: s for s in item.seasons}
        if not season_ids and not episode_ids and item.seasons:
            # Default to monitored or first released season
            default_seasons = [
                s
                for s in sorted(item.seasons, key=lambda x: x.watch_order)
                if s.monitored and s.status != SeasonStatus.FUTURE
            ]
            if not default_seasons:
                default_seasons = [
                    s
                    for s in sorted(item.seasons, key=lambda x: x.watch_order)
                    if s.status != SeasonStatus.FUTURE
                ][:1]
            season_ids = [s.id for s in default_seasons]

        selected_seasons = sorted(
            [seasons_by_id[sid] for sid in season_ids if sid in seasons_by_id],
            key=lambda s: s.watch_order,
        )
        covered_season_ids: set[int] = set()
        for season in selected_seasons:
            await _push_single_season_or_movie_entry(season, is_auto_advance=False)
            covered_season_ids.add(season.id)

        # Case 3: Explicitly selected individual episodes not already covered by a season push
        if episode_ids:
            eps_by_id = {ep.id: (s, ep) for s in item.seasons for ep in s.episodes}
            for eid in episode_ids:
                if eid not in eps_by_id:
                    continue
                parent_s, ep_obj = eps_by_id[eid]
                if parent_s.id in covered_season_ids:
                    continue
                raw_ep_results = await _query_show_across_indexers(
                    session,
                    item,
                    season_number=parent_s.season_number,
                    episode_number=ep_obj.episode_number,
                )
                ep_partitioned = _score_and_partition_candidates(
                    raw_results=raw_ep_results,
                    blacklisted_guids=blacklisted_guids,
                    blacklisted_titles=blacklisted_titles,
                    expected_title=item.title,
                    expected_year=None,
                    expected_alt_title=item.alt_title,
                    expected_season=parent_s.season_number,
                    expected_episode=ep_obj.episode_number,
                    expected_season_title=(
                        parent_s.title
                        if parent_s.title and parent_s.title != item.title
                        else None
                    ),
                    runtime_minutes=None,
                    effective_cfg=effective_cfg,
                )
                ep_hist = await _dispatch_candidate_list_to_torbox(
                    session,
                    ep_partitioned["all_valid"],
                    item=item,
                    season=parent_s,
                    episode=ep_obj,
                    push_mode="auto",
                    event_type="push_initiated",
                )
                if ep_hist is not None:
                    dispatched_histories.append(ep_hist)

        # Season Expansion & Movie Bridge Lookahead when auto_advance_seasons is ON
        if auto_advance_seasons and pushed_seasons:
            by_order = {s.watch_order: s for s in item.seasons}
            max_order = max(s.watch_order for s in pushed_seasons)
            pushed_ids = {s.id for s in pushed_seasons}

            next_entry = by_order.get(max_order + 1)
            if (
                next_entry is not None
                and next_entry.id not in pushed_ids
                and next_entry.status
                not in (
                    SeasonStatus.FUTURE,
                    SeasonStatus.DOWNLOADED,
                    SeasonStatus.COMPLETED,
                    SeasonStatus.DOWNLOADING,
                )
                and not next_entry.is_tba
            ):
                await _push_single_season_or_movie_entry(
                    next_entry, is_auto_advance=True
                )
                pushed_ids.add(next_entry.id)
                # Movie Bridge Lookahead: if N+1 is a movie, also advance to N+2
                if getattr(next_entry, "entry_type", "season") == "movie":
                    bridge_entry = by_order.get(max_order + 2)
                    if (
                        bridge_entry is not None
                        and bridge_entry.id not in pushed_ids
                        and bridge_entry.status
                        not in (
                            SeasonStatus.FUTURE,
                            SeasonStatus.DOWNLOADED,
                            SeasonStatus.COMPLETED,
                            SeasonStatus.DOWNLOADING,
                        )
                        and not bridge_entry.is_tba
                    ):
                        await _push_single_season_or_movie_entry(
                            bridge_entry, is_auto_advance=True
                        )

    await session.commit()
    return {
        "pushed": len(dispatched_histories) > 0,
        "push_mode": "auto",
        "dispatched_count": len(dispatched_histories),
        "history_ids": [h.id for h in dispatched_histories],
        "guids": [h.nzb_guid for h in dispatched_histories],
        "status_code": 200,
    }


async def execute_manual_search(
    session: AsyncSession,
    item_id: int,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute Search & Pick Manually (`push_mode = 'manual'`) for the Unified Push Modal."""
    payload = payload or {}
    stmt = (
        select(MediaItem)
        .where(MediaItem.id == item_id)
        .options(
            selectinload(MediaItem.provider),
            selectinload(MediaItem.seasons).selectinload(Season.episodes),
        )
    )
    item = (await session.execute(stmt)).scalars().first()
    if item is None:
        return {"error": "MediaItem not found", "status_code": 404}

    effective_cfg = await resolve_effective_search_config(session, item, payload)
    blacklisted_guids, blacklisted_titles = await _load_blacklisted_sets(
        session, item.id
    )

    season_ids = _parse_int_list(
        payload.get("season_ids")
        if "season_ids" in payload
        else payload.get("season_ids[]")
    )
    episode_ids = _parse_int_list(
        payload.get("episode_ids")
        if "episode_ids" in payload
        else payload.get("episode_ids[]")
    )

    pack_primary: list[dict[str, Any]] = []
    pack_fallback: list[dict[str, Any]] = []
    pack_mismatched: list[dict[str, Any]] = []
    episode_accordions: list[dict[str, Any]] = []

    if item.media_type == MediaType.MOVIE and not season_ids and not episode_ids:
        raw_movie_results = await _query_movie_across_indexers(session, item)
        partitioned = _score_and_partition_candidates(
            raw_results=raw_movie_results,
            blacklisted_guids=blacklisted_guids,
            blacklisted_titles=blacklisted_titles,
            expected_title=item.title,
            expected_year=item.year,
            expected_alt_title=item.alt_title,
            expected_season=None,
            expected_episode=None,
            expected_season_title=None,
            runtime_minutes=item.runtime_minutes,
            effective_cfg=effective_cfg,
        )
        pack_primary.extend(_serialize_candidate(c) for c in partitioned["primary"])
        pack_fallback.extend(_serialize_candidate(c) for c in partitioned["fallback"])
        pack_mismatched.extend(
            _serialize_candidate(c) for c in partitioned["mismatched"]
        )
    else:
        seasons_by_id = {s.id: s for s in item.seasons}
        if not season_ids and not episode_ids and item.seasons:
            first_s = sorted(item.seasons, key=lambda x: x.watch_order)[0]
            season_ids = [first_s.id]

        for sid in season_ids:
            season = seasons_by_id.get(sid)
            if season is None:
                continue
            entry_type = getattr(season, "entry_type", "season") or "season"
            if entry_type == "movie":
                raw_res = await _query_movie_across_indexers(
                    session,
                    item,
                    title_override=season.title or item.title,
                    use_external_ids=False,
                )
                partitioned = _score_and_partition_candidates(
                    raw_results=raw_res,
                    blacklisted_guids=blacklisted_guids,
                    blacklisted_titles=blacklisted_titles,
                    expected_title=season.title or item.title,
                    expected_year=season.air_date.year if season.air_date else None,
                    expected_alt_title=item.title,
                    expected_season=None,
                    expected_episode=None,
                    expected_season_title=season.title,
                    runtime_minutes=item.runtime_minutes,
                    effective_cfg=effective_cfg,
                )
            else:
                raw_res = await _query_show_across_indexers(
                    session,
                    item,
                    season_number=season.season_number,
                    episode_number=None,
                )
                partitioned = _score_and_partition_candidates(
                    raw_results=raw_res,
                    blacklisted_guids=blacklisted_guids,
                    blacklisted_titles=blacklisted_titles,
                    expected_title=item.title,
                    expected_year=None,
                    expected_alt_title=item.alt_title,
                    expected_season=season.season_number,
                    expected_episode=None,
                    expected_season_title=(
                        season.title
                        if season.title and season.title != item.title
                        else None
                    ),
                    runtime_minutes=None,
                    effective_cfg=effective_cfg,
                )

            for tier_key, target_list in (
                ("primary", pack_primary),
                ("fallback", pack_fallback),
                ("mismatched", pack_mismatched),
            ):
                for c in partitioned[tier_key]:
                    sc = _serialize_candidate(c)
                    sc["season_id"] = season.id
                    sc["season_number"] = season.season_number
                    sc["entry_type"] = entry_type
                    target_list.append(sc)

        # Build episode accordions for requested episode_ids (or episodes of requested TV seasons if none explicitly passed)
        target_episodes: list[tuple[Season, Episode]] = []
        if episode_ids:
            eps_map = {ep.id: (s, ep) for s in item.seasons for ep in s.episodes}
            for eid in episode_ids:
                if eid in eps_map:
                    target_episodes.append(eps_map[eid])
        else:
            for sid in season_ids:
                season = seasons_by_id.get(sid)
                if season and getattr(season, "entry_type", "season") == "season":
                    for ep in sorted(season.episodes, key=lambda e: e.episode_number):
                        target_episodes.append((season, ep))

        for parent_s, ep_obj in target_episodes:
            raw_ep_res = await _query_show_across_indexers(
                session,
                item,
                season_number=parent_s.season_number,
                episode_number=ep_obj.episode_number,
            )
            ep_part = _score_and_partition_candidates(
                raw_results=raw_ep_res,
                blacklisted_guids=blacklisted_guids,
                blacklisted_titles=blacklisted_titles,
                expected_title=item.title,
                expected_year=None,
                expected_alt_title=item.alt_title,
                expected_season=parent_s.season_number,
                expected_episode=ep_obj.episode_number,
                expected_season_title=(
                    parent_s.title
                    if parent_s.title and parent_s.title != item.title
                    else None
                ),
                runtime_minutes=None,
                effective_cfg=effective_cfg,
            )
            ep_primary = [_serialize_candidate(c) for c in ep_part["primary"]]
            ep_fallback = [_serialize_candidate(c) for c in ep_part["fallback"]]
            ep_mismatched = [_serialize_candidate(c) for c in ep_part["mismatched"]]
            episode_accordions.append(
                {
                    "episode_id": ep_obj.id,
                    "season_id": parent_s.id,
                    "season_number": parent_s.season_number,
                    "episode_number": ep_obj.episode_number,
                    "primary": ep_primary,
                    "fallback": ep_fallback,
                    "mismatched": ep_mismatched,
                    "releases": ep_primary + ep_fallback + ep_mismatched,
                }
            )

    return {
        "item_id": item.id,
        "pack_or_movie_releases": {
            "primary": pack_primary,
            "fallback": pack_fallback,
            "mismatched": pack_mismatched,
            "releases": pack_primary + pack_fallback + pack_mismatched,
        },
        "episode_accordions": episode_accordions,
        "status_code": 200,
    }


async def execute_manual_grab(
    session: AsyncSession,
    item_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a manually selected release from the Push Modal to TorBox with `push_mode = 'manual'`."""
    stmt = (
        select(MediaItem)
        .where(MediaItem.id == item_id)
        .options(selectinload(MediaItem.seasons).selectinload(Season.episodes))
    )
    item = (await session.execute(stmt)).scalars().first()
    if item is None:
        return {"pushed": False, "error": "MediaItem not found", "status_code": 404}

    guid = str(payload.get("guid") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not guid or not title:
        return {
            "pushed": False,
            "error": "Both guid and title are required",
            "status_code": 400,
        }

    season_id = payload.get("season_id")
    episode_id = payload.get("episode_id")

    season_obj: Season | None = None
    episode_obj: Episode | None = None

    if episode_id is not None and str(episode_id).strip() != "":
        try:
            eid = int(episode_id)
            for s in item.seasons:
                for ep in s.episodes:
                    if ep.id == eid:
                        season_obj = s
                        episode_obj = ep
                        break
        except (TypeError, ValueError):
            pass

    if season_obj is None and season_id is not None and str(season_id).strip() != "":
        try:
            sid = int(season_id)
            season_obj = next((s for s in item.seasons if s.id == sid), None)
        except (TypeError, ValueError):
            pass

    parsed = parse_release_name(title)
    candidate = {
        "guid": guid,
        "title": title,
        "size_bytes": int(payload.get("size_bytes") or 0),
        "score": float(payload.get("score") or 0.0),
        "parsed": parsed,
        "score_res": None,
        "indexer_url": payload.get("indexer_url"),
        "indexer_key": payload.get("indexer_key"),
        "is_fallback": bool(payload.get("is_fallback", False)),
        "matched_language": payload.get("matched_language"),
    }

    hist = await _dispatch_candidate_list_to_torbox(
        session,
        [candidate],
        item=item,
        season=season_obj,
        episode=episode_obj,
        push_mode="manual",
    )
    if hist is None:
        await session.commit()
        return {
            "pushed": False,
            "push_mode": "manual",
            "error": "Release failed fake verification or TorBox dispatch",
            "status_code": 400,
        }

    await session.commit()
    return {
        "pushed": True,
        "push_mode": "manual",
        "history_id": hist.id,
        "guid": hist.nzb_guid,
        "torbox_id": hist.torbox_id,
        "status_code": 200,
    }


async def get_push_modal_context(
    session: AsyncSession, item_id: int
) -> dict[str, Any] | None:
    """Build context dictionary for `GET /api/items/{item_id}/push-modal`."""
    stmt = (
        select(MediaItem)
        .where(MediaItem.id == item_id)
        .options(selectinload(MediaItem.seasons).selectinload(Season.episodes))
    )
    item = (await session.execute(stmt)).scalars().first()
    if item is None:
        return None

    presets = await list_presets(session)
    effective_cfg = await resolve_effective_search_config(session, item)
    entries = sorted(item.seasons, key=lambda s: (s.watch_order, s.season_number))

    return {
        "item": item,
        "entries": entries,
        "presets": presets,
        "effective_config": effective_cfg,
    }
