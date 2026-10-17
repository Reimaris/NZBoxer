"""Start the mock services, point NZBoxer's TMDB client at them and serve the app.

Used as the container command by demo/compose.demo.yaml. The app code is untouched:
only the TMDB/TorBox URL constants and the AniList search calls are patched in this process.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mock_services  # noqa: E402

PUBLIC = os.environ.get("DEMO_PUBLIC_URL", "http://localhost:9100")

mock_services.start(port=9100)

from app.services import anilist, media_search, tmdb  # noqa: E402

tmdb.TMDB_BASE_URL = "http://127.0.0.1:9100/tmdb/3"
tmdb.TMDB_POSTER_BASE = f"{PUBLIC}/poster"
media_search._TMDB_POSTER_BASE = f"{PUBLIC}/poster"


async def _no_anilist_search(*_a: object, **_k: object) -> list[dict]:
    return []


async def _no_anilist_media(*_a: object, **_k: object) -> None:
    return None


# Ad-hoc search must not surface real AniList entries (or call the real API).
anilist.search_anime_media = _no_anilist_search  # type: ignore[assignment]
anilist.get_anime_media_by_id = _no_anilist_media  # type: ignore[assignment]

from app.services.downloaders import torbox  # noqa: E402

torbox.TORBOX_BASE_URL = "http://127.0.0.1:9100/torbox/v1"
torbox.DEFAULT_API_URL = "http://127.0.0.1:9100/torbox/v1/api"

import uvicorn  # noqa: E402

import app.main as app_main  # noqa: E402

# Show "demo mode" instead of the version badge: swap the text in the base template
# at load time so the real template stays untouched.
_loader = app_main.templates.env.loader
assert _loader is not None
_orig_get_source = _loader.get_source


def _demo_get_source(environment, template):  # type: ignore[no-untyped-def]
    source, filename, uptodate = _orig_get_source(environment, template)
    return source.replace("v{{ app_version }}", "demo mode"), filename, uptodate


_loader.get_source = _demo_get_source  # type: ignore[method-assign]

uvicorn.run(app_main.app, host="0.0.0.0", port=8000)
