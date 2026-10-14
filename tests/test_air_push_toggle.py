"""Per-series Auto-Push toggle endpoint and Unified Push Modal rendering (m45/04)."""

import re

import pytest

from app.db.models import MediaType
from tests.conftest import make_movie, make_series

pytestmark = pytest.mark.anyio

TOGGLE_RE = re.compile(r"<input[^>]*toggle-auto-push[^>]*>", re.S)


async def test_toggle_round_trip(async_client, db_session):
    item, _, _ = await make_series(db_session)
    await db_session.commit()
    url = f"/api/items/{item.id}/toggle-auto-push"

    for expected in (True, False):
        resp = await async_client.post(url)
        assert resp.status_code == 200
        assert resp.json() == {"auto_push_episodes": expected}
        assert "HX-Trigger" in resp.headers
        await db_session.refresh(item)
        assert item.auto_push_episodes is expected


async def test_toggle_unknown_item_404(async_client):
    resp = await async_client.post("/api/items/99999/toggle-auto-push")
    assert resp.status_code == 404


async def test_toggle_movie_400(async_client, db_session):
    movie = await make_movie(db_session)
    await db_session.commit()
    resp = await async_client.post(f"/api/items/{movie.id}/toggle-auto-push")
    assert resp.status_code == 400


@pytest.mark.parametrize("enabled", [False, True])
async def test_modal_shows_toggle_for_series(async_client, db_session, enabled):
    item, _, _ = await make_series(db_session, auto_push_episodes=enabled)
    await db_session.commit()
    resp = await async_client.get(f"/api/items/{item.id}/push-modal")
    assert resp.status_code == 200
    assert "Auto-Push New Episodes" in resp.text
    match = TOGGLE_RE.search(resp.text)
    assert match
    assert f"/api/items/{item.id}/toggle-auto-push" in match.group(0)
    assert ("checked" in match.group(0).split()) is enabled


async def test_modal_omits_toggle_for_movie(async_client, db_session):
    movie = await make_movie(db_session)
    await db_session.commit()
    assert movie.media_type == MediaType.MOVIE
    resp = await async_client.get(f"/api/items/{movie.id}/push-modal")
    assert resp.status_code == 200
    assert "toggle-auto-push" not in resp.text
