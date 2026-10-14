from pathlib import Path

import pytest
from starlette.testclient import TestClient

import app.db.models as models
import app.services.anilist as anilist
from app.db.models import FailureLog, ProviderCategory, SystemSettings
from app.main import app


def test_print_orm_models_and_categories_removed() -> None:
    """Verify Print Media ORM models and provider categories are removed."""
    for removed_model in (
        "BookItem",
        "MangaItem",
        "MangaVolume",
        "MagazineSubscription",
        "MagazineIssue",
    ):
        assert not hasattr(models, removed_model), f"{removed_model} should be removed"

    assert not hasattr(ProviderCategory, "PRINT_MEDIA")
    assert "print_media" not in [c.value for c in ProviderCategory]

    # Verify SystemSettings no longer has print-specific columns
    settings_cols = {c.name for c in SystemSettings.__table__.columns}
    for col in (
        "print_search_interval",
        "reading_download_dir",
        "manga_blacklisted_formats",
        "book_blacklisted_formats",
        "magazine_blacklisted_formats",
    ):
        assert col not in settings_cols, f"{col} should be removed from SystemSettings"

    # Verify FailureLog no longer has print-specific foreign keys
    failure_cols = {c.name for c in FailureLog.__table__.columns}
    assert "book_item_id" not in failure_cols
    assert "manga_volume_id" not in failure_cols


def test_print_services_and_modules_deleted() -> None:
    """Verify Print Media service and core modules are deleted."""
    for module_name in (
        "app.services.hardcover",
        "app.services.openlibrary",
        "app.core.reading_scorer",
        "app.core.storage_manager",
    ):
        with pytest.raises(ModuleNotFoundError):
            __import__(module_name)

    assert not hasattr(anilist, "search_manga")
    assert not hasattr(anilist, "get_manga_details")
    assert hasattr(anilist, "search_anime_id_by_title")
    assert hasattr(anilist, "get_anime_season_details")


def test_print_templates_deleted() -> None:
    """Verify Print Media HTML templates are deleted from disk."""
    root = Path(__file__).resolve().parent.parent
    removed_templates = [
        "templates/print_dashboard.html",
        "templates/print_manual_search.html",
        "templates/partials/print_search_results.html",
        "templates/partials/manga_volumes.html",
        "templates/modals/add_print_media.html",
    ]
    for rel_path in removed_templates:
        assert not (root / rel_path).exists(), f"{rel_path} should be deleted"


def test_print_routes_and_ui_elements_removed() -> None:
    """Verify Print Media HTTP routes return 404 and UI pages omit Print Media."""
    with TestClient(app) as client:
        for removed_route in (
            "/dashboard/print",
            "/dashboard/print/manual-search",
            "/print/add/modal",
            "/dashboard/print/manga/1/volumes",
        ):
            resp = client.get(removed_route)
            assert resp.status_code == 404, f"GET {removed_route} should return 404"

        for removed_post in (
            "/api/search/print/manual",
            "/api/automation/run/print",
            "/api/print/add",
        ):
            resp = client.post(removed_post)
            assert resp.status_code == 404, f"POST {removed_post} should return 404"

        # Video dashboard and navbar should not show Print Media link or print history table
        dash_resp = client.get("/")
        assert dash_resp.status_code == 200
        assert "/dashboard/print" not in dash_resp.text
        assert "Print Media" not in dash_resp.text
        assert "print-history-table" not in dash_resp.text
