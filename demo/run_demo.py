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

uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
