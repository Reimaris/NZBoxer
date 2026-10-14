import re

import pytest
from starlette.routing import Mount, Route

from app.core.route_policy import classify_path, is_reviewed
from app.main import app


@pytest.mark.parametrize(
    ("path", "method", "expected"),
    [
        ("/sync", "POST", "watchlist"),
        ("/settings/provider/3/sync", "POST", "watchlist"),
        ("/settings/provider/3/edit", "GET", "open"),
        ("/settings", "GET", "open"),
        ("/api/search/manual", "POST", "core"),
        ("/api/search/manual/releases", "POST", "core"),
        ("/api/torbox/add", "POST", "core"),
        ("/api/items/5/push/auto", "POST", "watchlist"),
        ("/items/5", "DELETE", "watchlist"),
        ("/api/presets", "POST", "open"),
        ("/api/presets/3/default", "POST", "open"),
        ("/api/blacklist/clear", "POST", "open"),
        ("/", "GET", "core"),
        ("/static/app.css", "GET", "open"),
        ("/health", "GET", "open"),
        ("/settingsfoo", "GET", "core"),
    ],
)
def test_classify_path(path, method, expected):
    assert classify_path(path, method) == expected


def test_every_route_is_classified():
    unreviewed = []
    for route in app.routes:
        if not isinstance(route, (Route, Mount)):
            continue
        path = re.sub(r"\{[^}]+\}", "1", route.path)
        if not is_reviewed(path):
            unreviewed.append(route.path)
    assert not unreviewed, f"Unclassified routes (add to route_policy): {unreviewed}"
