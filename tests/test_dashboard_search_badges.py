import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MediaItem, MediaStatus, MediaType, Provider, ProviderCategory
from app.main import app
from tests.conftest import seed_core_prerequisites

client = TestClient(app)


def test_dashboard_template_search_retention_and_badges():
    """Verify templates/dashboard.html does not clear search on setTab and contains count badges."""
    env = Environment(loader=FileSystemLoader("templates"))
    template_source = env.loader.get_source(env, "dashboard.html")[0]

    # 1. setTab must NOT clear search
    assert "this.search = ''" not in template_source
    assert "this.search=''" not in template_source

    # 2. getTabCount method must be implemented
    assert "getTabCount(category)" in template_source

    # 3. Dynamic badges must be present on all 3 tabs (movie, show, anime)
    assert "getTabCount('movie')" in template_source
    assert "getTabCount('show')" in template_source
    assert "getTabCount('anime')" in template_source

    # 4. Badges must check search.trim().length > 0
    assert "search.trim().length > 0" in template_source


@pytest.mark.asyncio
async def test_dashboard_endpoint_renders_search_items_and_badges(
    db_session: AsyncSession,
):
    """Verify GET / renders valid Alpine items array and tab badges."""
    provider = Provider(
        name="Dashboard Test Indexer",
        type="newznab",
        category=ProviderCategory.INDEXER.value,
        api_url="http://indexer.local",
        api_key="secret",
        is_active=True,
    )
    db_session.add(provider)
    db_session.add(
        Provider(
            name="Simkl",
            type="simkl",
            category=ProviderCategory.WATCHLIST.value,
            client_id="cid",
            access_token="tok",
            is_active=True,
        )
    )
    await seed_core_prerequisites(db_session)
    await db_session.flush()

    movie = MediaItem(
        simkl_id=20001,
        title="Inception",
        media_type=MediaType.MOVIE,
        status=MediaStatus.SEARCHING,
        provider_id=provider.id,
    )
    show = MediaItem(
        simkl_id=20002,
        title="Breaking Bad",
        media_type=MediaType.SHOW,
        status=MediaStatus.SEARCHING,
        provider_id=provider.id,
    )
    anime = MediaItem(
        simkl_id=20003,
        title="Attack on Titan",
        media_type=MediaType.ANIME,
        status=MediaStatus.SEARCHING,
        provider_id=provider.id,
    )
    db_session.add_all([movie, show, anime])
    await db_session.commit()

    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    # Tab count badge spans must be rendered in HTML
    assert "getTabCount('movie')" in html
    assert "getTabCount('show')" in html
    assert "getTabCount('anime')" in html

    # Items array must be embedded for Alpine
    assert "Inception" in html
    assert "Breaking Bad" in html
    assert "Attack on Titan" in html
