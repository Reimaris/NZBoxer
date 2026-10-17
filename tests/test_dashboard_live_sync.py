"""Dashboard live sync & push modal navigation: template-level contract tests."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MediaStatus
from tests.conftest import make_history, make_movie


def _element_html(html: str, element_id: str) -> str:
    """Return the HTML of the element with *element_id* (balanced <div> scan)."""
    start = html.index(f'id="{element_id}"')
    start = html.rindex("<div", 0, start)
    depth = 0
    for m in re.finditer(r"<div\b|</div>", html[start:]):
        depth += 1 if m.group().startswith("<div") else -1
        if depth == 0:
            return html[start : start + m.end()]
    raise AssertionError(f"unbalanced element {element_id}")


@pytest.fixture
async def dashboard_html(async_client: AsyncClient, db_session: AsyncSession) -> str:
    movie = await make_movie(
        db_session, title="Test Movie", status=MediaStatus.COMPLETED
    )
    for n in (1, 2):
        await make_history(
            db_session,
            item=movie,
            nzb_guid=f"guid-f-{n}",
            downloader_transfer_id=f"tb-f-{n}",
            status_detail="failed: boom",
        )
    await make_history(
        db_session,
        item=movie,
        nzb_guid="guid-sim",
        downloader_transfer_id="tb-sim",
        is_dry_run=True,
    )
    await db_session.commit()
    return (await async_client.get("/?tab=history")).text


@pytest.mark.asyncio
async def test_history_toolbar_container_wraps_whole_toolbar(dashboard_html: str):
    assert dashboard_html.count('id="history-toolbar-container"') == 1
    container = _element_html(dashboard_html, "history-toolbar-container")
    for marker in (
        'id="history-filter-bar"',
        'id="history-toolbar-actions"',
        "Search push history ledger...",
        'x-show="isSelectingHistory"',
    ):
        assert marker in container
    for text in ("Failed (2)", "Simulated (1)", "Delete Dry-Run (1)"):
        assert text in container


@pytest.mark.asyncio
async def test_swap_ids_use_unified_container(dashboard_html: str):
    swap = dashboard_html[dashboard_html.index("const swapIds") :]
    swap = swap[: swap.index("];")]
    assert "'history-toolbar-container'" in swap
    assert "'history-toolbar-actions'" not in swap
    assert "'dashboard-history-table-body'" in swap
    assert "document.activeElement" in dashboard_html


@pytest.mark.asyncio
async def test_hybrid_polling_and_tab_switch_refresh(dashboard_html: str):
    timer = dashboard_html[dashboard_html.index("_activePushesPollTimer") :]
    timer = timer[: timer.index("}, 3000)")]
    assert "this.activeCard === 'active_pushes'" not in timer
    assert "this.lastInFlightCount > 0" in timer
    assert "inFlightDropped" in dashboard_html
    assert "_refreshQueued" in dashboard_html
    assert "async refreshDashboard()" in dashboard_html
    set_card = dashboard_html[dashboard_html.index("setCard(cardName) {") :]
    set_card = set_card[: set_card.index("setTab(")]
    assert "cardName === 'history'" in set_card
    assert "this.refreshDashboard()" in set_card
    assert "cardName === 'active_pushes'" in set_card
    assert "refreshActivePushes" in set_card


@pytest.mark.asyncio
async def test_toast_and_push_dispatched_listener(dashboard_html: str):
    assert 'id="dashboard-toast"' in dashboard_html
    assert "showDashboardToast(message)" in dashboard_html
    assert "nzboxer:push-dispatched" in dashboard_html
    assert "Auto-Push started for" in dashboard_html
    assert "Dry-run started for" in dashboard_html


@pytest.mark.asyncio
@pytest.mark.parametrize(("dry_run", "mode"), [(False, "auto"), (True, "dry_run")])
async def test_push_modal_dispatches_event_without_card_switch(
    async_client: AsyncClient,
    db_session: AsyncSession,
    dry_run: bool,
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.config import settings

    movie = await make_movie(db_session, title="Test Movie")
    monkeypatch.setattr(settings, "dry_run", dry_run)
    await db_session.commit()

    resp = await async_client.get(f"/api/items/{movie.id}/push-modal")
    assert resp.status_code == 200
    html = resp.text
    body = html[html.index("async runAutoPush()") :]
    body = body[: body.index("async runManualSearch()")]
    assert "nzboxer:push-dispatched" in body
    assert 'card: "active_pushes"' not in body
    assert "nzboxer:switch-card" not in body
    assert f'mode: "{mode}"' in body
    # x-data is single-quoted: the script must not contain a bare single quote
    assert "'" not in body


@pytest.mark.asyncio
async def test_dashboard_script_is_valid_javascript(
    dashboard_html: str, tmp_path: Path
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    idx = dashboard_html.index("window.videoDashboard = initVideoDashboard")
    start = dashboard_html.rindex("<script", 0, idx)
    end = dashboard_html.index("</script>", idx)
    js = dashboard_html[dashboard_html.index(">", start) + 1 : end]
    (tmp_path / "dash.js").write_text(js)
    result = subprocess.run(
        [node, "--check", str(tmp_path / "dash.js")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
