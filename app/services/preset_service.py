"""
Search Preset & Sticky Item Configuration Service (v3.0.0)
==========================================================
Provides CRUD operations, single-default enforcement, inline modal save,
and per-item sticky configuration persistence for SearchPreset and MediaItem.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MediaItem, SearchPreset

CUSTOM_CONFIG_KEYS = (
    "resolutions",
    "sources",
    "video_codecs",
    "min_size_gb",
    "max_size_gb",
    "required_keywords",
    "excluded_keywords",
    "audio_formats",
)


def _normalize_language(val: Any, default: str | None = "en") -> str | None:
    if val is None:
        return default
    s = str(val).strip()
    if not s or s.lower() in ("none", "null"):
        return None
    return s


def _normalize_mode(val: Any) -> str:
    s = str(val or "best").strip().lower()
    return "custom" if s == "custom" else "best"


def _normalize_bool(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def normalize_season_pack_flags(
    allow: Any,
    prefer: Any,
    default_allow: bool = False,
    default_prefer: bool = False,
) -> tuple[bool, bool]:
    """Normalize (allow_season_packs, prefer_season_packs) enforcing !allow => !prefer."""
    allow_bool = _normalize_bool(allow, default_allow)
    prefer_bool = _normalize_bool(prefer, default_prefer) if allow_bool else False
    return allow_bool, prefer_bool


def extract_custom_config_json(payload: dict[str, Any]) -> str:
    """Extract and normalize custom_config_json from a JSON or form payload."""
    raw_cfg = payload.get("custom_config_json")
    if raw_cfg is None:
        raw_cfg = payload.get("custom_config")

    base_dict: dict[str, Any] = {}
    if isinstance(raw_cfg, dict):
        base_dict.update(raw_cfg)
    elif isinstance(raw_cfg, str) and raw_cfg.strip():
        try:
            parsed = json.loads(raw_cfg)
            if isinstance(parsed, dict):
                base_dict.update(parsed)
        except Exception:
            pass

    # Merge any top-level custom config keys if provided directly in payload
    for key in CUSTOM_CONFIG_KEYS:
        if key in payload and payload[key] is not None:
            val = payload[key]
            if key in ("min_size_gb", "max_size_gb"):
                if val == "":
                    base_dict[key] = None
                else:
                    try:
                        base_dict[key] = float(val)
                    except (TypeError, ValueError):
                        pass
            elif isinstance(val, str):
                # Split comma-separated strings for list fields
                items = [x.strip() for x in val.split(",") if x.strip()]
                base_dict[key] = items
            elif isinstance(val, list):
                base_dict[key] = val

    return json.dumps(base_dict)


async def list_presets(session: AsyncSession) -> list[SearchPreset]:
    """Return all SearchPreset rows ordered with default first, then by id."""
    stmt = select(SearchPreset).order_by(
        SearchPreset.is_default.desc(), SearchPreset.id.asc()
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_preset(session: AsyncSession, preset_id: int) -> SearchPreset | None:
    """Return a single SearchPreset by ID."""
    return await session.get(SearchPreset, preset_id)


async def get_default_preset(session: AsyncSession) -> SearchPreset | None:
    """Return the active default SearchPreset (or first preset if none marked)."""
    stmt = select(SearchPreset).order_by(
        SearchPreset.is_default.desc(), SearchPreset.id.asc()
    )
    return (await session.execute(stmt)).scalars().first()


async def create_preset(session: AsyncSession, payload: dict[str, Any]) -> SearchPreset:
    """Create a new SearchPreset (or update if same name exists) and enforce single default."""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("Preset name is required.")

    primary_language = (
        _normalize_language(payload.get("primary_language"), "en") or "en"
    )
    fallback_language = _normalize_language(payload.get("fallback_language"), None)
    video_quality_mode = _normalize_mode(payload.get("video_quality_mode"))
    audio_quality_mode = _normalize_mode(payload.get("audio_quality_mode"))
    allow_season_packs, prefer_season_packs = normalize_season_pack_flags(
        payload.get("allow_season_packs"),
        payload.get("prefer_season_packs"),
        default_allow=False,
        default_prefer=False,
    )
    custom_config_json = extract_custom_config_json(payload)
    requested_default = _normalize_bool(payload.get("is_default"), False)

    # Check if any presets currently exist
    existing_all = await list_presets(session)
    is_default = requested_default or len(existing_all) == 0

    # Check if preset with same name exists
    existing_same_name = next((p for p in existing_all if p.name == name), None)
    if existing_same_name is not None:
        if is_default:
            await session.execute(
                update(SearchPreset)
                .where(SearchPreset.id != existing_same_name.id)
                .values(is_default=False)
            )
            existing_same_name.is_default = True
        existing_same_name.primary_language = primary_language
        existing_same_name.fallback_language = fallback_language
        existing_same_name.video_quality_mode = video_quality_mode
        existing_same_name.audio_quality_mode = audio_quality_mode
        existing_same_name.allow_season_packs = allow_season_packs
        existing_same_name.prefer_season_packs = prefer_season_packs
        existing_same_name.custom_config_json = custom_config_json
        await session.flush()
        return existing_same_name

    if is_default:
        await session.execute(update(SearchPreset).values(is_default=False))

    preset = SearchPreset(
        name=name,
        is_default=is_default,
        primary_language=primary_language,
        fallback_language=fallback_language,
        video_quality_mode=video_quality_mode,
        audio_quality_mode=audio_quality_mode,
        allow_season_packs=allow_season_packs,
        prefer_season_packs=prefer_season_packs,
        custom_config_json=custom_config_json,
    )
    session.add(preset)
    await session.flush()
    return preset


async def update_preset(
    session: AsyncSession, preset_id: int, payload: dict[str, Any]
) -> SearchPreset | None:
    """Update an existing SearchPreset by ID and enforce single default."""
    preset = await session.get(SearchPreset, preset_id)
    if preset is None:
        return None

    if "name" in payload and payload["name"] is not None:
        new_name = str(payload["name"]).strip()
        if new_name:
            preset.name = new_name

    if "primary_language" in payload:
        preset.primary_language = (
            _normalize_language(payload.get("primary_language"), "en") or "en"
        )
    if "fallback_language" in payload:
        preset.fallback_language = _normalize_language(
            payload.get("fallback_language"), None
        )
    if "video_quality_mode" in payload:
        preset.video_quality_mode = _normalize_mode(payload.get("video_quality_mode"))
    if "audio_quality_mode" in payload:
        preset.audio_quality_mode = _normalize_mode(payload.get("audio_quality_mode"))

    if "allow_season_packs" in payload or "prefer_season_packs" in payload:
        allow_sp, prefer_sp = normalize_season_pack_flags(
            payload.get("allow_season_packs", preset.allow_season_packs),
            payload.get("prefer_season_packs", preset.prefer_season_packs),
            default_allow=bool(preset.allow_season_packs),
            default_prefer=bool(preset.prefer_season_packs),
        )
        preset.allow_season_packs = allow_sp
        preset.prefer_season_packs = prefer_sp
        await session.execute(
            update(MediaItem)
            .where(MediaItem.preset_id == preset.id)
            .values(
                allow_season_packs=allow_sp,
                prefer_season_packs=prefer_sp,
            )
        )

    if (
        "custom_config_json" in payload
        or "custom_config" in payload
        or any(k in payload for k in CUSTOM_CONFIG_KEYS)
    ):
        preset.custom_config_json = extract_custom_config_json(payload)

    if "is_default" in payload and _normalize_bool(payload.get("is_default"), False):
        await session.execute(
            update(SearchPreset)
            .where(SearchPreset.id != preset.id)
            .values(is_default=False)
        )
        preset.is_default = True

    await session.flush()
    return preset


async def set_default_preset(
    session: AsyncSession, preset_id: int
) -> SearchPreset | None:
    """Mark the specified preset as default and unset all others."""
    preset = await session.get(SearchPreset, preset_id)
    if preset is None:
        return None

    await session.execute(
        update(SearchPreset)
        .where(SearchPreset.id != preset_id)
        .values(is_default=False)
    )
    preset.is_default = True
    await session.flush()
    return preset


async def delete_preset(session: AsyncSession, preset_id: int) -> bool:
    """Delete a SearchPreset, clear references on MediaItem, and promote next default if needed."""
    preset = await session.get(SearchPreset, preset_id)
    if preset is None:
        return False

    was_default = bool(preset.is_default)

    await session.execute(
        update(MediaItem).where(MediaItem.preset_id == preset_id).values(preset_id=None)
    )
    await session.delete(preset)
    await session.flush()

    if was_default:
        remaining = await list_presets(session)
        if remaining and not any(p.is_default for p in remaining):
            remaining[0].is_default = True
            await session.flush()

    return True


async def save_inline_preset(
    session: AsyncSession, payload: dict[str, Any]
) -> SearchPreset:
    """Save a preset inline from the Push Modal and optionally bind it to a MediaItem."""
    preset = await create_preset(session, payload)

    raw_item_id = payload.get("media_item_id") or payload.get("item_id")
    if raw_item_id is not None:
        try:
            item_id = int(raw_item_id)
            item = await session.get(MediaItem, item_id)
            if item is not None:
                item.preset_id = preset.id
                item.custom_search_config_json = None
                item.allow_season_packs = bool(preset.allow_season_packs)
                item.prefer_season_packs = (
                    bool(preset.prefer_season_packs)
                    if item.allow_season_packs
                    else False
                )
                if "auto_advance_seasons" in payload:
                    item.auto_advance_seasons = _normalize_bool(
                        payload.get("auto_advance_seasons"), False
                    )
                await session.flush()
        except (TypeError, ValueError):
            pass

    return preset


async def update_item_sticky_search_config(
    session: AsyncSession, item_id: int, payload: dict[str, Any]
) -> MediaItem | None:
    """Persist sticky search configuration (preset_id, custom overrides, toggles) on a MediaItem."""
    item = await session.get(MediaItem, item_id)
    if item is None:
        return None

    previous_preset_id = item.preset_id
    explicit_custom = False
    resolved_pid: int | None = item.preset_id

    if "preset_id" in payload:
        raw_pid = payload.get("preset_id")
        if raw_pid in (None, "", "custom", "none"):
            resolved_pid = None
            explicit_custom = True
        else:
            try:
                resolved_pid = int(raw_pid)
            except (TypeError, ValueError):
                resolved_pid = None
                explicit_custom = True

    preset = await session.get(SearchPreset, resolved_pid) if resolved_pid else None
    if preset is None:
        resolved_pid = None

    has_custom_payload = (
        "custom_search_config_json" in payload
        or "custom_config" in payload
        or "custom_config_json" in payload
        or "video_quality_mode" in payload
        or "audio_quality_mode" in payload
        or any(k in payload for k in CUSTOM_CONFIG_KEYS)
    )

    packs_diverge = False
    if preset is not None and (
        "allow_season_packs" in payload or "prefer_season_packs" in payload
    ):
        req_allow, req_prefer = normalize_season_pack_flags(
            payload.get("allow_season_packs", preset.allow_season_packs),
            payload.get("prefer_season_packs", preset.prefer_season_packs),
            default_allow=bool(preset.allow_season_packs),
            default_prefer=bool(preset.prefer_season_packs),
        )
        p_allow, p_prefer = normalize_season_pack_flags(
            preset.allow_season_packs,
            preset.prefer_season_packs,
            default_allow=bool(preset.allow_season_packs),
            default_prefer=bool(preset.prefer_season_packs),
        )
        packs_diverge = (req_allow, req_prefer) != (p_allow, p_prefer)

    use_preset = (
        preset is not None
        and not explicit_custom
        and not packs_diverge
        and not ("preset_id" not in payload and has_custom_payload)
        and not (
            "custom_search_config_json" in payload
            and payload["custom_search_config_json"] not in (None, "")
        )
    )

    if use_preset and preset is not None:
        allow_sp, prefer_sp = normalize_season_pack_flags(
            preset.allow_season_packs,
            preset.prefer_season_packs,
            default_allow=bool(preset.allow_season_packs),
            default_prefer=bool(preset.prefer_season_packs),
        )
        item.preset_id = preset.id
        item.custom_search_config_json = None
        item.allow_season_packs = allow_sp
        item.prefer_season_packs = prefer_sp
    else:
        base_preset = preset
        if base_preset is None and previous_preset_id:
            base_preset = await session.get(SearchPreset, previous_preset_id)

        custom_dict: dict[str, Any] = {}
        if base_preset is not None:
            custom_dict.update(base_preset.custom_config)
            custom_dict["primary_language"] = base_preset.primary_language
            custom_dict["fallback_language"] = base_preset.fallback_language
            custom_dict["video_quality_mode"] = base_preset.video_quality_mode
            custom_dict["audio_quality_mode"] = base_preset.audio_quality_mode
            custom_dict["allow_season_packs"] = bool(base_preset.allow_season_packs)
            custom_dict["prefer_season_packs"] = bool(base_preset.prefer_season_packs)

        if item.custom_search_config_json:
            try:
                existing_cfg = json.loads(item.custom_search_config_json)
                if isinstance(existing_cfg, dict):
                    custom_dict.update(existing_cfg)
            except Exception:
                pass

        explicitly_cleared_custom = (
            "custom_search_config_json" in payload
            and payload["custom_search_config_json"] in (None, "")
            and "allow_season_packs" not in payload
            and "prefer_season_packs" not in payload
            and "custom_config" not in payload
            and "custom_config_json" not in payload
        )

        if "custom_search_config_json" in payload:
            raw_custom = payload.get("custom_search_config_json")
            if raw_custom is None or raw_custom == "":
                custom_dict = {}
            elif isinstance(raw_custom, dict):
                custom_dict.update(raw_custom)
            else:
                try:
                    parsed_raw = json.loads(str(raw_custom))
                    if isinstance(parsed_raw, dict):
                        custom_dict.update(parsed_raw)
                except Exception:
                    pass

        if (
            "custom_config" in payload
            or "custom_config_json" in payload
            or any(k in payload for k in CUSTOM_CONFIG_KEYS)
        ):
            try:
                extracted_cfg = json.loads(extract_custom_config_json(payload))
                if isinstance(extracted_cfg, dict):
                    custom_dict.update(extracted_cfg)
            except Exception:
                pass

        for meta_key in (
            "primary_language",
            "fallback_language",
            "video_quality_mode",
            "audio_quality_mode",
        ):
            if meta_key in payload and payload[meta_key] is not None:
                custom_dict[meta_key] = payload[meta_key]

        def_allow = custom_dict.get("allow_season_packs", item.allow_season_packs)
        def_prefer = custom_dict.get("prefer_season_packs", item.prefer_season_packs)
        allow_sp, prefer_sp = normalize_season_pack_flags(
            payload.get("allow_season_packs", def_allow),
            payload.get("prefer_season_packs", def_prefer),
            default_allow=bool(def_allow),
            default_prefer=bool(def_prefer),
        )
        item.allow_season_packs = allow_sp
        item.prefer_season_packs = prefer_sp
        item.preset_id = None

        if explicitly_cleared_custom:
            item.custom_search_config_json = None
        else:
            if (
                "allow_season_packs" in payload
                or "prefer_season_packs" in payload
                or custom_dict
            ):
                custom_dict["allow_season_packs"] = allow_sp
                custom_dict["prefer_season_packs"] = prefer_sp
            item.custom_search_config_json = (
                json.dumps(custom_dict) if custom_dict else None
            )

    if (
        "auto_advance_seasons" in payload
        and payload["auto_advance_seasons"] is not None
    ):
        item.auto_advance_seasons = _normalize_bool(
            payload.get("auto_advance_seasons"), False
        )

    await session.flush()
    return item
