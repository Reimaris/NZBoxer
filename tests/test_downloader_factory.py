"""Downloader factory and SABnzbd category mapping (m57-01)."""

import json
import sys
import types

import pytest

from app.db.models import MediaType, Provider
from app.services.downloaders import (
    DOWNLOADER_DISPLAY_NAMES,
    category_for_media,
    get_active_downloader_client,
    get_downloader_client,
    get_downloader_client_for_type,
)

_STUBS = {
    "torbox": ("app.services.downloaders.torbox", "TorBoxDownloader"),
    "sabnzbd": ("app.services.downloaders.sabnzbd", "SabnzbdDownloader"),
    "premiumize": ("app.services.downloaders.premiumize", "PremiumizeDownloader"),
}


def _provider(type_: str, config: dict | str | None = None, **kw) -> Provider:
    cfg = json.dumps(config) if isinstance(config, dict) else (config or "{}")
    return Provider(
        name=type_,
        type=type_,
        category="downloader",
        api_key="k",
        config_json=cfg,
        **kw,
    )


@pytest.fixture(autouse=True)
def stub_clients(monkeypatch):
    """Replace client modules so the factory is testable before they land."""
    for ptype, (mod_name, cls_name) in _STUBS.items():
        cls = type(cls_name, (), {"__init__": lambda self, p: setattr(self, "p", p)})
        module = types.ModuleType(mod_name)
        setattr(module, cls_name, cls)
        monkeypatch.setitem(sys.modules, mod_name, module)


@pytest.mark.parametrize("ptype", list(_STUBS))
def test_factory_returns_client_per_type(ptype):
    p = _provider(ptype)
    client = get_downloader_client(p)
    assert type(client).__name__ == _STUBS[ptype][1]
    assert client.p is p


def test_factory_unknown_type_raises():
    with pytest.raises(ValueError, match="Unsupported downloader type: nzbget"):
        get_downloader_client(_provider("nzbget"))


def test_display_names():
    assert DOWNLOADER_DISPLAY_NAMES == {
        "torbox": "TorBox",
        "sabnzbd": "SABnzbd",
        "premiumize": "Premiumize",
    }


@pytest.mark.anyio
async def test_active_and_for_type_clients(db_session):
    assert await get_active_downloader_client(db_session) is None
    assert await get_downloader_client_for_type(db_session, "sabnzbd") is None
    db_session.add_all(
        [
            _provider("torbox", is_active=True),
            _provider("sabnzbd", is_active=False),
        ]
    )
    await db_session.flush()
    active = await get_active_downloader_client(db_session)
    assert type(active).__name__ == "TorBoxDownloader"
    inactive = await get_downloader_client_for_type(db_session, "sabnzbd")
    assert type(inactive).__name__ == "SabnzbdDownloader"


_CUSTOM = {"category_movie": "m", "category_series": "s", "category_anime": "a"}


@pytest.mark.parametrize(
    ("ptype", "config", "media", "anime", "expected"),
    [
        ("sabnzbd", _CUSTOM, MediaType.MOVIE, False, "m"),
        ("sabnzbd", _CUSTOM, MediaType.SHOW, False, "s"),
        ("sabnzbd", _CUSTOM, "show", False, "s"),
        ("sabnzbd", _CUSTOM, MediaType.ANIME, False, "a"),
        ("sabnzbd", _CUSTOM, MediaType.SHOW, True, "a"),
        ("sabnzbd", _CUSTOM, MediaType.MOVIE, True, "a"),
        ("sabnzbd", {}, MediaType.MOVIE, False, "movies"),
        ("sabnzbd", {}, MediaType.SHOW, False, "tv"),
        ("sabnzbd", {}, MediaType.ANIME, False, "anime"),
        ("sabnzbd", {"category_movie": ""}, MediaType.MOVIE, False, None),
        ("sabnzbd", {"category_movie": 5}, MediaType.MOVIE, False, "movies"),
        ("sabnzbd", "not json", MediaType.SHOW, False, "tv"),
        ("torbox", _CUSTOM, MediaType.MOVIE, False, None),
        ("premiumize", _CUSTOM, MediaType.ANIME, True, None),
    ],
)
def test_category_for_media(ptype, config, media, anime, expected):
    assert category_for_media(_provider(ptype, config), media, anime) == expected
