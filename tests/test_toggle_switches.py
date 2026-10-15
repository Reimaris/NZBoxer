"""Toggle-switch markup for binary on/off controls (templates only, no backend change)."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.anyio

SETTINGS_FIELDS = [
    "debug_mode",
    "dry_run",
    "history_autoclear_pruned",
    "discord_enabled",
    "notify_on_push_initiated",
    "notify_on_completed",
    "notify_on_failure",
    "notify_on_auto_advance",
    "notify_on_errors",
    "notify_on_dry_run",
]


def _input_tag(html: str, attr: str) -> str:
    match = re.search(r"<input[^>]*" + re.escape(attr) + r"[^>]*>", html, re.S)
    assert match, attr
    return match.group(0)


def _track_after(html: str, tag: str) -> str:
    rest = html[html.index(tag) + len(tag) :]
    return re.search(r"<div class=\"([^\"]*)\"", rest).group(1)


@pytest.fixture
async def settings_html(async_client):
    resp = await async_client.get("/settings")
    assert resp.status_code == 200
    return resp.text


@pytest.mark.parametrize("field", SETTINGS_FIELDS)
async def test_settings_field_is_switch_and_keeps_autosave(settings_html, field):
    tag = _input_tag(settings_html, f'name="{field}"')
    assert "sr-only peer" in tag
    assert 'hx-post="/settings/auto-save"' in tag
    assert 'hx-trigger="change"' in tag
    assert f'js:{{"{field}": event.target.checked ? "true" : "false"}}' in tag
    track = _track_after(settings_html, tag)
    expected = (
        "peer-checked:bg-amber-500"
        if field in ("dry_run", "debug_mode")
        else "peer-checked:bg-[#d40060]"
    )
    assert expected in track


def test_modal_switches_and_non_switch_checkboxes():
    onboarding = Path("templates/modals/onboarding.html").read_text()
    for name in ("sabnzbd_ssl", "discord_enabled"):
        assert "sr-only peer" in _input_tag(onboarding, f'name="{name}"')

    provider = Path("templates/modals/provider.html").read_text()
    for name in ("sync_movies", "sync_series", "sync_anime"):
        assert "sr-only peer" in _input_tag(provider, f'name="{name}"')

    push = Path("templates/modals/push_modal.html").read_text()
    assert "sr-only peer" in _input_tag(push, 'x-model="autoAdvanceSeasons"')
    for attr in ('x-model="allowSeasonPacks"', 'x-model="preferSeasonPacks"'):
        assert "sr-only peer" not in _input_tag(push, attr)

    dashboard = Path("templates/dashboard.html").read_text()
    assert "sr-only peer" not in _input_tag(dashboard, "selectedHistoryIds")
    assert "sr-only peer" not in _input_tag(dashboard, 'name="save_defaults"')
