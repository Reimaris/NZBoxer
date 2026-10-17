import json

import pytest
from sqlalchemy import select, text

from app.db.models import NotificationChannel, Provider, SystemSettings
from app.services.indexers.catalog import get_preset

TRIGGER = 'hx-get="/onboarding/modal"'


async def test_fresh_system_settings_onboarding_pending(db_session):
    settings = SystemSettings(id=1)
    db_session.add(settings)
    await db_session.flush()
    await db_session.refresh(settings)
    assert settings.onboarding_completed is False


async def test_onboarding_column_exists_after_init_db(db_session):
    rows = (
        await db_session.execute(text("PRAGMA table_info(system_settings)"))
    ).fetchall()
    assert "onboarding_completed" in {r[1] for r in rows}


async def _set_completed(db_session, value: bool) -> None:
    db_session.add(SystemSettings(id=1, onboarding_completed=value))
    await db_session.commit()


async def test_dashboard_triggers_wizard_without_settings_row(async_client):
    resp = await async_client.get("/")
    assert resp.status_code == 200
    assert TRIGGER in resp.text
    assert "nzb_onboarding_dismissed" in resp.text


@pytest.mark.parametrize(("completed", "expected"), [(False, True), (True, False)])
async def test_dashboard_gating_follows_sentinel(
    async_client, db_session, completed, expected
):
    await _set_completed(db_session, completed)
    resp = await async_client.get("/")
    assert (TRIGGER in resp.text) is expected


async def test_modal_renders_on_empty_db(async_client):
    resp = await async_client.get("/onboarding/modal")
    assert resp.status_code == 200
    assert 'id="onboarding-modal"' in resp.text


async def test_modal_renders_with_seeded_records(async_client, db_session):
    db_session.add_all(
        [
            Provider(
                name="TMDB",
                type="tmdb",
                category="metadata",
                api_key="tmdb-key-0123456789abcdef",
            ),
            NotificationChannel(
                type="telegram",
                name="Bot",
                bot_token="123456:ABC",
                chat_id="-100123456",
            ),
        ]
    )
    await db_session.commit()
    resp = await async_client.get("/onboarding/modal")
    assert resp.status_code == 200
    # Field-level pre-population assertions arrive with the step 1/2 UI tickets.


async def test_modal_has_skip_affordances(async_client):
    html = (await async_client.get("/onboarding/modal")).text
    for needle in (
        "/api/onboarding/complete",
        "this.step = this.step + 1",
        "nzb_onboarding_dismissed",
    ):
        assert needle in html


async def test_complete_creates_row_sets_sentinel_and_is_idempotent(
    async_client, db_session
):
    for _ in range(2):
        resp = await async_client.post("/api/onboarding/complete")
        assert resp.status_code == 200
        assert resp.text == ""
        assert "onboardingCompleted" in resp.headers["HX-Trigger"]
    db_session.expire_all()
    row = (
        (await db_session.execute(select(SystemSettings).where(SystemSettings.id == 1)))
        .scalars()
        .one()
    )
    assert row.onboarding_completed is True
    assert TRIGGER not in (await async_client.get("/")).text


STEP1_FULL = {
    "simkl_client_id": "simkl-client-abc123",
    "simkl_username": "alice",
    "tmdb_api_key": "tmdb-key-0123456789abcdef",
    "torbox_api_key": "torbox-key-xyz",
    "torbox_api_url": "",
    "indexer_name": "NZBgeek",
    "indexer_url": "https://api.nzbgeek.example/api",
    "indexer_api_key": "idx-key-777",
}


async def _providers(db_session) -> dict[str, Provider]:
    db_session.expire_all()
    rows = (await db_session.execute(select(Provider))).scalars().all()
    return {p.type: p for p in rows}


async def test_step1_full_creates_four_providers(async_client, db_session):
    resp = await async_client.post("/api/onboarding/step1", data=STEP1_FULL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert all(isinstance(v, int) for v in body["provider_ids"].values())
    provs = await _providers(db_session)
    assert {k: (v.category) for k, v in provs.items()} == {
        "simkl": "watchlist",
        "tmdb": "metadata",
        "torbox": "downloader",
        "newznab": "indexer",
    }
    assert provs["torbox"].api_url == "https://api.torbox.app/v1/api"
    assert provs["torbox"].is_active is True
    assert provs["newznab"].name == "NZBgeek"
    assert provs["simkl"].client_id == "simkl-client-abc123"


async def test_step1_empty_payload_is_noop(async_client, db_session):
    resp = await async_client.post("/api/onboarding/step1", data={})
    assert resp.status_code == 200
    assert resp.json()["provider_ids"] == {
        "watchlist": None,
        "simkl": None,
        "tmdb": None,
        "torbox": None,
        "indexer": None,
        "downloader": None,
    }
    assert await _providers(db_session) == {}


async def test_step1_blank_section_keeps_existing_and_returns_id(
    async_client, db_session
):
    db_session.add(
        Provider(name="TMDB", type="tmdb", category="metadata", api_key="keep-me")
    )
    await db_session.commit()
    resp = await async_client.post(
        "/api/onboarding/step1", data={"torbox_api_key": "tb"}
    )
    assert resp.json()["provider_ids"]["tmdb"] is not None
    assert (await _providers(db_session))["tmdb"].api_key == "keep-me"


async def test_step1_update_reuses_rows_and_preserves_simkl_tokens(
    async_client, db_session
):
    db_session.add(
        Provider(
            name="Simkl",
            type="simkl",
            category="watchlist",
            access_token="tok",
            config_json='{"refresh_token": "ref", "token_expires_at": 99}',
        )
    )
    await db_session.commit()
    await async_client.post("/api/onboarding/step1", data=STEP1_FULL)
    await async_client.post(
        "/api/onboarding/step1",
        data={"tmdb_api_key": "new-tmdb", "simkl_client_id": "cid2"},
    )
    db_session.expire_all()
    rows = (await db_session.execute(select(Provider))).scalars().all()
    assert len(rows) == 4
    provs = {p.type: p for p in rows}
    assert provs["tmdb"].api_key == "new-tmdb"
    simkl = provs["simkl"]
    assert simkl.access_token == "tok"
    cfg = json.loads(simkl.config_json)
    assert cfg["refresh_token"] == "ref"
    assert cfg["token_expires_at"] == 99
    assert cfg["client_id"] == "cid2"


@pytest.mark.parametrize(
    "indexer", [{"indexer_url": "https://x.example/api"}, {"indexer_api_key": "k"}]
)
async def test_step1_indexer_partial_is_400_and_atomic(
    async_client, db_session, indexer
):
    resp = await async_client.post(
        "/api/onboarding/step1", data={"tmdb_api_key": "t", **indexer}
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "ok": False,
        "error": "Indexer requires both URL and API key.",
    }
    assert await _providers(db_session) == {}


async def test_modal_step1_cards_prefilled_and_wired(async_client, db_session):
    db_session.add(
        Provider(
            name="TMDB",
            type="tmdb",
            category="metadata",
            api_key="tmdb-key-0123456789abcdef",
        )
    )
    await db_session.commit()
    html = (await async_client.get("/onboarding/modal")).text
    assert 'value="tmdb-key-0123456789abcdef"' in html
    assert 'value="https://api.torbox.app/v1/api"' in html
    for needle in (
        "/api/onboarding/step1",
        "/simkl/auth/start",
        'id="onboarding-simkl-pin"',
    ):
        assert needle in html
    assert 'name="tmdb_api_key"' in html
    assert 'rel="noopener noreferrer"' in html


STEP2_FULL = {
    "discord_webhook_url": "https://discord.com/api/webhooks/123/abcDEF",
    "discord_enabled": "on",
    "telegram_name": "My Bot",
    "telegram_bot_token": "123456:ABC-DEF",
    "telegram_chat_id": "-100123456",
}


async def _channels(db_session) -> list[NotificationChannel]:
    db_session.expire_all()
    return list((await db_session.execute(select(NotificationChannel))).scalars())


async def test_step2_full_then_update_does_not_duplicate(async_client, db_session):
    resp = await async_client.post("/api/onboarding/step2", data=STEP2_FULL)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    row = await db_session.get(SystemSettings, 1)
    assert row.discord_webhook_url == STEP2_FULL["discord_webhook_url"]
    assert row.discord_enabled is True
    chans = await _channels(db_session)
    assert len(chans) == 1
    assert (chans[0].type, chans[0].name) == ("telegram", "My Bot")
    assert (chans[0].bot_token, chans[0].chat_id) == ("123456:ABC-DEF", "-100123456")

    resp = await async_client.post(
        "/api/onboarding/step2",
        data={**STEP2_FULL, "telegram_name": "", "telegram_chat_id": "42"},
    )
    assert resp.status_code == 200
    chans = await _channels(db_session)
    assert len(chans) == 1
    assert (chans[0].name, chans[0].chat_id) == ("Telegram", "42")


@pytest.mark.parametrize(
    "tg", [{"telegram_bot_token": "tok"}, {"telegram_chat_id": "123"}]
)
async def test_step2_telegram_partial_is_400_and_atomic(async_client, db_session, tg):
    resp = await async_client.post(
        "/api/onboarding/step2",
        data={"discord_webhook_url": "https://discord.com/api/webhooks/1/x", **tg},
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "ok": False,
        "error": "Telegram requires both Bot Token and Chat ID.",
    }
    assert await _channels(db_session) == []
    assert await db_session.get(SystemSettings, 1) is None


async def test_step2_blank_webhook_ignores_discord_section(async_client, db_session):
    db_session.add(
        SystemSettings(
            id=1, discord_webhook_url="https://old.example/hook", discord_enabled=False
        )
    )
    await db_session.commit()
    resp = await async_client.post(
        "/api/onboarding/step2",
        data={"discord_webhook_url": "  ", "discord_enabled": "on"},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    row = await db_session.get(SystemSettings, 1)
    assert row.discord_webhook_url == "https://old.example/hook"
    assert row.discord_enabled is False


async def test_modal_step2_cards_prefilled_and_wired(async_client, db_session):
    await async_client.post("/api/onboarding/step2", data=STEP2_FULL)
    html = (await async_client.get("/onboarding/modal")).text
    for needle in (
        "saveStep2()",
        'name="discord_webhook_url"',
        'name="telegram_chat_id"',
        'value="-100123456"',
        'value="My Bot"',
        'value="https://discord.com/api/webhooks/123/abcDEF"',
        "Optional",
    ):
        assert needle in html
    assert 'name="discord_enabled" value="on" checked' in html
    assert 'id="onboarding-error"' in html


async def test_settings_page_has_launch_wizard_button(async_client):
    html = (await async_client.get("/settings")).text
    assert 'hx-get="/onboarding/modal"' in html
    assert "Launch Setup Wizard" in html


async def test_modal_step3_wiring_is_user_triggered(async_client):
    html = (await async_client.get("/onboarding/modal")).text
    for needle in (
        'hx-get="/system/check/run"',
        'hx-target="#onboarding-check-results"',
        'hx-post="/sync"',
        'hx-target="#onboarding-sync-result"',
        'id="onboarding-check-results"',
        'id="onboarding-sync-result"',
        "Finish Setup",
    ):
        assert needle in html
    assert 'hx-trigger="load' not in html


# --- m56-13: Step 1 watchlist selection -------------------------------------

STEP1_ANILIST = {
    "watchlist_type": "anilist",
    "anilist_username": "alice-anime",
    "anilist_access_token": "",
    "simkl_client_id": "stale-simkl-id",
    "simkl_username": "stale",
}
STEP1_PLEX_MANUAL = {"watchlist_type": "plex", "plex_auth_token": "plex-token-123"}
STEP1_PLEX_PREPARE = {"watchlist_type": "plex", "watchlist_prepare": "1"}
STEP1_MAL = {"watchlist_type": "mal", "mal_client_id": "mal-client-0123"}


async def _step1(client, data):
    return await client.post("/api/onboarding/step1", data=data)


async def test_step1_anilist_ignores_stale_simkl(async_client, db_session):
    resp = await _step1(async_client, STEP1_ANILIST)
    assert resp.status_code == 200
    provs = await _providers(db_session)
    assert "simkl" not in provs
    al = provs["anilist"]
    assert (al.category, al.username) == ("watchlist", "alice-anime")
    assert resp.json()["provider_ids"]["watchlist"] == al.id
    assert json.loads(al.config_json) == {"sync_interval_minutes": 0}


async def test_step1_anilist_token_only_is_400_and_writes_nothing(
    async_client, db_session
):
    resp = await _step1(
        async_client, {"watchlist_type": "anilist", "anilist_access_token": "t"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "AniList requires a username."
    assert await _providers(db_session) == {}


async def test_step1_anilist_blank_token_keeps_existing(async_client, db_session):
    await _step1(async_client, {**STEP1_ANILIST, "anilist_access_token": "al-tok"})
    await _step1(async_client, STEP1_ANILIST)
    assert (await _providers(db_session))["anilist"].access_token == "al-tok"


async def test_step1_plex_manual_token_and_stable_client_identifier(
    async_client, db_session
):
    import uuid

    await _step1(async_client, STEP1_PLEX_MANUAL)
    plex_p = (await _providers(db_session))["plex"]
    assert plex_p.access_token == "plex-token-123"
    assert plex_p.watchlist_connection_status == "connected"
    ident = json.loads(plex_p.config_json)["plex_client_identifier"]
    uuid.UUID(ident)
    await _step1(async_client, STEP1_PLEX_MANUAL)
    assert (
        json.loads((await _providers(db_session))["plex"].config_json)[
            "plex_client_identifier"
        ]
        == ident
    )


async def test_step1_plex_prepare_creates_row_without_token(async_client, db_session):
    resp = await _step1(async_client, STEP1_PLEX_PREPARE)
    plex_p = (await _providers(db_session))["plex"]
    assert not plex_p.access_token
    assert plex_p.watchlist_connection_status == "not_connected"
    assert resp.json()["provider_ids"]["watchlist"] == plex_p.id


async def test_step1_mal_preserves_oauth_tokens(async_client, db_session):
    await _step1(async_client, STEP1_MAL)
    mal_p = (await _providers(db_session))["mal"]
    mal_p.access_token = "mal-a"
    mal_p.config_json = json.dumps({"refresh_token": "mal-r"})
    await db_session.commit()
    await _step1(async_client, {**STEP1_MAL, "mal_client_id": "mal-client-0123"})
    mal_p = (await _providers(db_session))["mal"]
    assert mal_p.client_id == "mal-client-0123"
    assert mal_p.access_token == "mal-a"
    assert json.loads(mal_p.config_json)["refresh_token"] == "mal-r"


async def test_step1_blank_skip_unknown_and_legacy(async_client, db_session):
    resp = await _step1(
        async_client,
        {"watchlist_type": "", "simkl_client_id": "x", "simkl_username": "y"},
    )
    assert resp.status_code == 200
    assert await _providers(db_session) == {}
    assert resp.json()["provider_ids"]["watchlist"] is None

    resp = await _step1(async_client, {"watchlist_type": "letterboxd"})
    assert (resp.status_code, resp.json()["error"]) == (400, "Unknown watchlist type.")

    resp = await _step1(async_client, STEP1_FULL)
    simkl_id = (await _providers(db_session))["simkl"].id
    assert resp.json()["provider_ids"]["simkl"] == simkl_id
    assert resp.json()["provider_ids"]["watchlist"] == simkl_id


async def test_step1_selected_provider_synced_into_sources(
    async_client, db_session, monkeypatch
):
    from unittest.mock import AsyncMock

    from app.core import automation
    from app.db.models import MediaItem, MediaItemWatchlistSource
    from app.services import anilist, tmdb
    from app.services.watchlists import mal, plex

    monkeypatch.setattr(tmdb, "get_movie_details", AsyncMock(return_value=None))
    monkeypatch.setattr(tmdb, "get_digital_release_date", AsyncMock(return_value=None))
    monkeypatch.setattr(
        anilist, "get_anime_root_and_hierarchy", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        anilist, "get_anime_ids_by_mal_ids", AsyncMock(return_value={5114: 5114})
    )
    monkeypatch.setattr(
        anilist,
        "get_user_media_list",
        AsyncMock(
            return_value=[
                {
                    "status": "CURRENT",
                    "progress": 3,
                    "media": {
                        "id": 16498,
                        "idMal": 16498,
                        "format": "TV",
                        "title": {"romaji": "Shingeki no Kyojin"},
                        "startDate": {"year": 2013},
                    },
                }
            ]
        ),
    )
    monkeypatch.setattr(
        plex,
        "get_watchlist",
        AsyncMock(
            return_value=[
                {
                    "ratingKey": "5d776825",
                    "type": "movie",
                    "title": "The Matrix",
                    "year": 1999,
                    "Guid": [{"id": "imdb://tt0133093"}, {"id": "tmdb://603"}],
                }
            ]
        ),
    )
    monkeypatch.setattr(
        mal,
        "get_animelist",
        AsyncMock(
            return_value=[
                {
                    "node": {
                        "id": 5114,
                        "title": "Fullmetal Alchemist: Brotherhood",
                        "media_type": "tv",
                        "num_episodes": 64,
                    },
                    "list_status": {
                        "status": "plan_to_watch",
                        "num_episodes_watched": 0,
                    },
                }
            ]
        ),
    )

    for data in (STEP1_ANILIST, STEP1_PLEX_MANUAL, STEP1_MAL):
        resp = await _step1(async_client, data)
        pid = resp.json()["provider_ids"]["watchlist"]
        if data is STEP1_MAL:
            prov = await db_session.get(Provider, pid)
            assert prov is not None
            prov.access_token = "mal-a"
            prov.config_json = json.dumps(
                {
                    "refresh_token": "mal-r",
                    "token_expires_at": "2099-01-01T00:00:00+00:00",
                }
            )
            await db_session.commit()
        result = await automation.sync_single_provider(pid)
        assert result["status"] == "ok", result
        db_session.expire_all()
        rows = (
            (
                await db_session.execute(
                    select(MediaItemWatchlistSource).where(
                        MediaItemWatchlistSource.provider_id == pid
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows
    items = (await db_session.execute(select(MediaItem))).scalars().all()
    assert items and all(i.simkl_id == 0 for i in items)


async def test_modal_watchlist_dropdown_and_panels(async_client, db_session):
    fresh = (await async_client.get("/onboarding/modal")).text
    assert 'name="watchlist_type"' in fresh
    assert 'id="onboarding-watchlist-type"' in fresh
    assert "watchlistType: &#34;&#34;" in fresh or 'watchlistType: ""' in fresh
    for label in ("None / Skip", "Simkl", "AniList", "Plex", "MyAnimeList"):
        assert f">{label}</option>" in fresh
    assert (
        "selected"
        not in fresh.split('id="onboarding-watchlist-type"')[1].split("</select>")[0]
    )
    for t in ("simkl", "anilist", "plex", "mal"):
        assert f'id="onboarding-watchlist-panel-{t}"' in fresh
    for needle in (
        "getPlexPin()",
        "connectMal()",
        'id="onboarding-watchlist-prepare"',
        'id="onboarding-plex-pin"',
        'id="onboarding-mal-connect"',
        "data.provider_ids.watchlist",
        "/settings/provider/mal/auth/callback",
    ):
        assert needle in fresh

    db_session.add(
        Provider(
            name="AniList",
            type="anilist",
            category="watchlist",
            is_active=True,
            priority=1,
            username="alice-anime",
            access_token="anilist-secret-token",
        )
    )
    await db_session.commit()
    seeded = (await async_client.get("/onboarding/modal")).text
    assert "watchlistType: &#34;anilist&#34;" in seeded or (
        'watchlistType: "anilist"' in seeded
    )
    assert 'value="alice-anime"' in seeded
    assert "anilist-secret-token" not in seeded


STEP1_SAB = {
    "downloader_type": "sabnzbd",
    "sabnzbd_host": "192.168.1.5",
    "sabnzbd_port": "9090",
    "sabnzbd_api_key": "sab-key",
    "sabnzbd_ssl": "on",
}
STEP1_PMZ = {"downloader_type": "premiumize", "premiumize_api_key": "pmz-key"}


async def test_step1_sabnzbd_creates_active_provider(async_client, db_session):
    resp = await _step1(async_client, {**STEP1_SAB, "torbox_api_key": "stale"})
    assert resp.status_code == 200
    provs = await _providers(db_session)
    assert "torbox" not in provs
    sab = provs["sabnzbd"]
    assert (sab.category, sab.name, sab.is_active, sab.api_key) == (
        "downloader",
        "SABnzbd",
        True,
        "sab-key",
    )
    assert sab.api_url == "https://192.168.1.5:9090/sabnzbd/api"
    assert json.loads(sab.config_json) == {
        "category_movie": "movies",
        "category_series": "tv",
        "category_anime": "anime",
    }
    ids = resp.json()["provider_ids"]
    assert ids["downloader"] == sab.id and ids["torbox"] is None


async def test_step1_sabnzbd_defaults_and_update_keeps_config(async_client, db_session):
    data = {"downloader_type": "sabnzbd", "sabnzbd_host": "sab", "sabnzbd_api_key": "k"}
    await _step1(async_client, data)
    sab = (await _providers(db_session))["sabnzbd"]
    assert sab.api_url == "http://sab:8080/sabnzbd/api"
    sab.config_json = '{"category_movie": "custom"}'
    await db_session.commit()
    resp = await _step1(async_client, {**data, "sabnzbd_api_key": "k2"})
    assert resp.status_code == 200
    db_session.expire_all()
    rows = (await db_session.execute(select(Provider))).scalars().all()
    assert len(rows) == 1
    assert rows[0].api_key == "k2"
    assert rows[0].config_json == '{"category_movie": "custom"}'


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"sabnzbd_host": "h"}, "SABnzbd requires host and API key."),
        ({"sabnzbd_api_key": "k"}, "SABnzbd requires host and API key."),
        (
            {"sabnzbd_host": "http://h", "sabnzbd_api_key": "k"},
            "SABnzbd host must be a hostname or IP address.",
        ),
        (
            {"sabnzbd_host": "h/x", "sabnzbd_api_key": "k"},
            "SABnzbd host must be a hostname or IP address.",
        ),
        (
            {"sabnzbd_host": "h", "sabnzbd_api_key": "k", "sabnzbd_port": "0"},
            "SABnzbd port must be between 1 and 65535.",
        ),
        (
            {"sabnzbd_host": "h", "sabnzbd_api_key": "k", "sabnzbd_port": "abc"},
            "SABnzbd port must be between 1 and 65535.",
        ),
    ],
)
async def test_step1_sabnzbd_validation_400_writes_nothing(
    async_client, db_session, data, error
):
    resp = await _step1(
        async_client, {"downloader_type": "sabnzbd", "tmdb_api_key": "t", **data}
    )
    assert (resp.status_code, resp.json()["error"]) == (400, error)
    assert await _providers(db_session) == {}


async def test_step1_premiumize_and_single_active_switch(async_client, db_session):
    await _step1(async_client, {"torbox_api_key": "tb"})  # legacy payload
    provs = await _providers(db_session)
    assert provs["torbox"].is_active is True

    resp = await _step1(async_client, STEP1_PMZ)
    assert resp.status_code == 200
    provs = await _providers(db_session)
    pmz = provs["premiumize"]
    assert (pmz.name, pmz.api_key, pmz.api_url, pmz.is_active) == (
        "Premiumize",
        "pmz-key",
        None,
        True,
    )
    assert provs["torbox"].is_active is False
    assert resp.json()["provider_ids"]["downloader"] == pmz.id
    assert resp.json()["provider_ids"]["torbox"] == provs["torbox"].id

    await _step1(async_client, STEP1_PMZ)  # re-save: no duplicate
    rows = (await db_session.execute(select(Provider))).scalars().all()
    assert sorted(r.type for r in rows) == ["premiumize", "torbox"]


async def test_step1_downloader_skip_leaves_active_downloader(async_client, db_session):
    await _step1(async_client, {"torbox_api_key": "tb"})
    for data in (
        {"downloader_type": ""},
        {"downloader_type": "premiumize", "premiumize_api_key": ""},
        {"downloader_type": "sabnzbd", "sabnzbd_port": "1234"},
    ):
        resp = await _step1(async_client, data)
        assert resp.status_code == 200
        provs = await _providers(db_session)
        assert list(provs) == ["torbox"] and provs["torbox"].is_active is True
    resp = await _step1(async_client, {"downloader_type": "usenetx"})
    assert (resp.status_code, resp.json()["error"]) == (
        400,
        "Unknown downloader type.",
    )


async def test_step1_torbox_selected_ignores_stale_sab_fields(async_client, db_session):
    resp = await _step1(
        async_client,
        {
            "downloader_type": "torbox",
            "torbox_api_key": "tb",
            "sabnzbd_host": "ignored",
            "sabnzbd_api_key": "ignored",
        },
    )
    assert resp.status_code == 200
    assert list(await _providers(db_session)) == ["torbox"]


async def test_modal_downloader_context_prefill(async_client, db_session, monkeypatch):
    from app import main

    captured: dict = {}
    real = main.templates.TemplateResponse

    def spy(*args, **kwargs):
        captured.update(kwargs["context"])
        return real(*args, **kwargs)

    monkeypatch.setattr(main.templates, "TemplateResponse", spy)
    await async_client.get("/onboarding/modal")
    assert captured["downloader_type"] == ""
    assert captured["sabnzbd"] == {
        "host": "",
        "port": "8080",
        "ssl": False,
        "api_key": "",
    }
    assert captured["premiumize"] == {"api_key": ""}

    await _step1(async_client, STEP1_SAB)
    await async_client.get("/onboarding/modal")
    assert captured["downloader_type"] == "sabnzbd"
    assert captured["sabnzbd"] == {
        "host": "192.168.1.5",
        "port": "9090",
        "ssl": True,
        "api_key": "sab-key",
    }


async def test_modal_downloader_dropdown_and_panels(async_client, db_session):
    fresh = (await async_client.get("/onboarding/modal")).text
    assert 'name="downloader_type"' in fresh
    assert 'id="onboarding-downloader-type"' in fresh
    assert "downloaderType: &#34;&#34;" in fresh or 'downloaderType: ""' in fresh
    for label in ("None / Skip", "TorBox", "SABnzbd", "Premiumize"):
        assert f">{label}</option>" in fresh
    select = fresh.split('id="onboarding-downloader-type"')[1].split("</select>")[0]
    assert "selected" not in select
    for t in ("torbox", "sabnzbd", "premiumize"):
        assert f'id="onboarding-downloader-panel-{t}"' in fresh
    assert 'name="sabnzbd_port" value="8080"' in fresh
    assert "https://www.premiumize.me/account" in fresh

    await _step1(async_client, {**STEP1_SAB, "sabnzbd_port": "9090"})
    seeded = (await async_client.get("/onboarding/modal")).text
    assert "downloaderType: &#34;sabnzbd&#34;" in seeded or (
        'downloaderType: "sabnzbd"' in seeded
    )
    assert 'name="sabnzbd_host" value="192.168.1.5"' in seeded
    assert 'name="sabnzbd_port" value="9090"' in seeded
    assert "checked" in seeded.split('name="sabnzbd_ssl"')[1][:60]


async def test_step1_indexer_preset_creates_typed_provider(async_client, db_session):
    resp = await _step1(
        async_client,
        {"indexer_type": "nzbgeek", "indexer_url": "", "indexer_api_key": "k1"},
    )
    assert resp.status_code == 200
    prov = (await _providers(db_session))["nzbgeek"]
    assert (prov.category, prov.name, prov.is_active, prov.priority) == (
        "indexer",
        "NZBGeek",
        True,
        1,
    )
    assert prov.api_url == get_preset("nzbgeek").api_url
    assert resp.json()["provider_ids"]["indexer"] == prov.id


@pytest.mark.parametrize("bad", ["bogus", "torbox"])
async def test_step1_indexer_unknown_type_is_400(async_client, db_session, bad):
    resp = await _step1(async_client, {"indexer_type": bad, "indexer_api_key": "k"})
    assert (resp.status_code, resp.json()["error"]) == (400, "Unknown indexer type.")
    assert await _providers(db_session) == {}


async def test_step1_indexer_custom_newznab(async_client, db_session):
    resp = await _step1(
        async_client, {"indexer_type": "newznab", "indexer_api_key": "k"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "Indexer requires both URL and API key."
    assert await _providers(db_session) == {}

    resp = await _step1(
        async_client,
        {
            "indexer_type": "newznab",
            "indexer_url": "https://x.example/api",
            "indexer_api_key": "k",
        },
    )
    assert resp.status_code == 200
    assert (await _providers(db_session))["newznab"].name == "Custom Newznab"


async def test_step1_indexer_name_override_and_blank_skip(async_client, db_session):
    resp = await _step1(
        async_client,
        {
            "indexer_type": "",
            "indexer_url": "https://stale.example/api",
            "indexer_api_key": "stale",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["provider_ids"]["indexer"] is None
    assert await _providers(db_session) == {}

    await _step1(
        async_client,
        {"indexer_type": "dognzb", "indexer_name": "My Dog", "indexer_api_key": "k"},
    )
    assert (await _providers(db_session))["dognzb"].name == "My Dog"


async def test_step1_legacy_indexer_and_second_indexer_added(async_client, db_session):
    resp = await _step1(async_client, STEP1_FULL)
    tm_id = (await _providers(db_session))["newznab"].id
    assert resp.status_code == 200

    resp = await _step1(
        async_client,
        {"indexer_type": "nzbgeek", "indexer_api_key": "k2", "watchlist_type": ""},
    )
    provs = await _providers(db_session)
    assert provs["newznab"].is_active and provs["newznab"].id == tm_id
    assert provs["nzbgeek"].category == "indexer"
    assert resp.json()["provider_ids"]["indexer"] == provs["nzbgeek"].id

    resp = await _step1(async_client, {"indexer_type": ""})
    assert resp.json()["provider_ids"]["indexer"] == tm_id


async def test_modal_indexer_context_from_first_indexer(async_client, db_session):

    db_session.add(
        Provider(
            name="NZBgeek",
            type="nzbgeek",
            category="indexer",
            api_url="https://api.nzbgeek.info/api",
            api_key="kk",
        )
    )
    await db_session.commit()
    resp = await async_client.get("/onboarding/modal")
    assert resp.status_code == 200
    assert "https://api.nzbgeek.info/api" in resp.text


async def test_modal_indexer_dropdown_fresh_and_preselected(async_client, db_session):
    import html as html_lib

    # x-data values are attribute-escaped (&#34;); compare against the parsed form.
    html = html_lib.unescape((await async_client.get("/onboarding/modal")).text)
    for needle in (
        'id="onboarding-indexer-type"',
        'name="indexer_type"',
        "None / Skip",
        'value="nzbgeek"',
        "Custom Newznab",
        "onboarding-indexer-key-link",
        'id="onboarding-indexer-panel"',
        '"api_url"',
    ):
        assert needle in html
    assert 'value="nzbgeek" selected' not in html
    assert 'indexerType: ""' in html

    db_session.add(
        Provider(
            name="NZBGeek",
            type="nzbgeek",
            category="indexer",
            api_url="https://api.nzbgeek.info/api",
            api_key="kk",
        )
    )
    await db_session.commit()
    html = (await async_client.get("/onboarding/modal")).text
    assert 'value="nzbgeek" selected' in html
    assert "indexerType: &#34;nzbgeek&#34;" in html


# --- Jellyseerr / Overseerr onboarding --------------------------------------

STEP1_SEERR = {
    "watchlist_type": "jellyseerr",
    "seerr_server_url": "http://seerr.example.com:5055/",
    "seerr_api_key": "seerr-key-0123",
}


@pytest.mark.parametrize("ptype", ["jellyseerr", "overseerr"])
async def test_step1_seerr_creates_provider(async_client, db_session, ptype):
    resp = await _step1(async_client, {**STEP1_SEERR, "watchlist_type": ptype})
    assert resp.status_code == 200
    prov = (await _providers(db_session))[ptype]
    assert (prov.category, prov.is_active) == ("watchlist", True)
    assert resp.json()["provider_ids"]["watchlist"] == prov.id
    cfg = json.loads(prov.config_json)
    assert cfg["server_url"] == "http://seerr.example.com:5055"
    assert cfg["api_key"] == "seerr-key-0123"
    assert not prov.api_key
    assert (cfg["sync_movies"], cfg["sync_series"], cfg["notify_available"]) == (
        True,
        True,
        True,
    )
    assert cfg["sync_admin_watchlist"] is False


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"seerr_server_url": "seerr.example.com"}, "Server URL is required"),
        ({"seerr_api_key": ""}, "API key is required."),
    ],
)
async def test_step1_seerr_validation_writes_nothing(
    async_client, db_session, data, error
):
    resp = await _step1(async_client, {**STEP1_SEERR, **data})
    assert resp.status_code == 400
    assert error in resp.json()["error"]
    assert await _providers(db_session) == {}


async def test_step1_seerr_blank_key_keeps_existing(async_client, db_session):
    await _step1(async_client, STEP1_SEERR)
    resp = await _step1(
        async_client,
        {
            **STEP1_SEERR,
            "seerr_api_key": "",
            "seerr_server_url": "https://new.example.com",
        },
    )
    assert resp.status_code == 200
    cfg = json.loads((await _providers(db_session))["jellyseerr"].config_json)
    assert cfg["api_key"] == "seerr-key-0123"
    assert cfg["server_url"] == "https://new.example.com"


async def test_onboarding_modal_lists_seerr_without_echoing_key(
    async_client, db_session
):
    await _step1(async_client, STEP1_SEERR)
    html = (await async_client.get("/onboarding/modal")).text
    assert '<option value="jellyseerr"' in html
    assert '<option value="overseerr"' in html
    assert "http://seerr.example.com:5055" in html
    assert "seerr-key-0123" not in html
