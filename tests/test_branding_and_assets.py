"""
Tests for Smewlet SVG Icon and Branding Integration.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_static_icon_svg_endpoint():
    """Verify that /static/icon.svg is served with 200 OK and correct content type."""
    response = client.get("/static/icon.svg")
    assert response.status_code == 200
    assert "image/svg+xml" in response.headers.get("content-type", "")
    assert "<svg" in response.text
    assert "nzboxer" not in response.text.lower()


def test_static_favicon_svg_endpoint():
    """Verify that /static/favicon.svg is served as SVG."""
    response = client.get("/static/favicon.svg")
    assert response.status_code == 200
    assert "image/svg+xml" in response.headers.get("content-type", "")
    assert "<svg" in response.text


def test_base_template_branding_elements():
    """Verify favicon link in head and navbar brand logo image in base template."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    # Favicon link in head
    assert '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">' in html

    assert "<title>Smewlet</title>" in html

    # Navbar logo image
    assert '<img src="/static/icon.svg" alt="Smewlet Logo"' in html
