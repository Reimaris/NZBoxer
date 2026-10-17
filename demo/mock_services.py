"""Offline stand-ins for the services NZBoxer talks to (stdlib only).

Serves, on one port:
  /newznab/api          Newznab JSON search with fake releases
  /tmdb/3/...           the TMDB endpoints NZBoxer uses
  /torbox/...           empty TorBox lists (the demo never sends a real NZB)
  /poster/<n>.svg       generated placeholder posters
Nothing here touches the network; all data comes from demo/catalog.py.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from catalog import all_titles  # noqa: E402

TITLES = all_titles()
PALETTE = ["#d40060", "#2563eb", "#059669", "#d97706", "#7c3aed", "#0891b2", "#be123c"]

# (resolution, source, codec, audio, group, size_gb, passworded)
RELEASES = [
    ("2160p", "WEB-DL", "HEVC", "DDP5.1 Atmos", "NOVA", 14.2, False),
    ("2160p", "BluRay.REMUX", "HEVC", "TrueHD 7.1", "FORGE", 58.0, False),
    ("1080p", "WEB-DL", "H264", "DDP5.1", "NOVA", 4.6, False),
    ("1080p", "BluRay", "x264", "DTS-HD MA 5.1", "FORGE", 11.8, False),
    ("1080p", "WEBRip", "x265", "AAC 2.0", "TIDE", 2.1, False),
    ("720p", "HDTV", "x264", "AAC 2.0", "TIDE", 1.3, False),
    ("1080p", "WEB-DL", "H264", "AC3 5.1", "LUMEN", 4.4, True),
    ("1080p", "WEB-DL", "H264.GERMAN", "DD5.1", "NOVA", 4.5, False),
]


# Per-episode sizes (GB), index-aligned with RELEASES; a season pack is 8 episodes.
EPISODE_GB = [8.0, 16.0, 3.2, 6.0, 1.8, 1.4, 3.1, 3.2]


def poster_svg(key: str) -> bytes:
    h = int(hashlib.sha1(key.encode()).hexdigest(), 16)
    c1, c2 = PALETTE[h % len(PALETTE)], PALETTE[(h // 7) % len(PALETTE)]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 300">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{c1}"/>'
        f'<stop offset="1" stop-color="{c2}"/></linearGradient></defs>'
        '<rect width="200" height="300" fill="url(#g)"/>'
        '<circle cx="100" cy="120" r="46" fill="none" stroke="#fff" stroke-opacity=".55" stroke-width="6"/>'
        '<rect x="30" y="215" width="140" height="10" rx="5" fill="#fff" fill-opacity=".6"/>'
        '<rect x="55" y="238" width="90" height="8" rx="4" fill="#fff" fill-opacity=".4"/></svg>'
    ).encode()


def _dots(title: str) -> str:
    return (
        "".join(c if c.isalnum() else "." for c in title).replace("..", ".").strip(".")
    )


def _release_item(name: str, idx: int, size_gb: float, passworded: bool) -> dict:
    guid = hashlib.md5(name.encode()).hexdigest()
    return {
        "title": name,
        "guid": f"https://indexer.demo.invalid/details/{guid}",
        "link": f"http://127.0.0.1:9100/newznab/api?t=get&id={guid}",
        "pubDate": (datetime.now(timezone.utc) - timedelta(hours=3 * idx + 2)).strftime(
            "%a, %d %b %Y %H:%M:%S +0000"
        ),
        "enclosure": {
            "@attributes": {
                "url": f"http://127.0.0.1:9100/newznab/api?t=get&id={guid}",
                "length": str(int(size_gb * 1024**3)),
                "type": "application/x-nzb",
            }
        },
        "attr": [
            {"@attributes": {"name": "size", "value": str(int(size_gb * 1024**3))}},
            {"@attributes": {"name": "guid", "value": guid}},
            {"@attributes": {"name": "password", "value": "1" if passworded else "0"}},
        ],
    }


def search_results(q: dict[str, list[str]]) -> dict:
    tmdb = int((q.get("tmdbid") or ["0"])[0] or 0)
    meta = TITLES.get(tmdb)
    query = (q.get("q") or [""])[0].lower()
    if meta is None and query:
        meta = next((m for m in TITLES.values() if query in m["title"].lower()), None)
    if meta is None:
        return {"channel": {"item": []}}
    season = (q.get("season") or [""])[0]
    ep = (q.get("ep") or [""])[0]
    base = _dots(meta["title"])
    items = []
    for i, (res, src, codec, audio, grp, gb, pw) in enumerate(RELEASES):
        if meta["kind"] == "movie":
            name = f"{base}.{meta['year']}.{res}.{src}.{audio.replace(' ', '.')}.{codec}-{grp}"
        else:
            tag = f"S{int(season):02d}" if season else "S01"
            gb = EPISODE_GB[i]
            if ep:
                tag += f"E{int(ep):02d}"
            else:
                gb = round(gb * 8, 1)
            name = f"{base}.{tag}.{res}.{src}.{audio.replace(' ', '.')}.{codec}-{grp}"
        items.append(_release_item(name, i, gb, pw))
    return {"channel": {"item": items}}


def _tmdb_entry(tmdb: int, meta: dict) -> dict:
    date = f"{meta['year']}-06-15"
    base = {
        "id": tmdb,
        "overview": f"{meta['title']} is a fictional demo title.",
        "poster_path": f"/{tmdb}.svg",
        "popularity": 50.0,
        "vote_average": 7.4,
        "original_language": "en",
    }
    if meta["kind"] == "movie":
        base.update(
            title=meta["title"], original_title=meta["title"], release_date=date
        )
    else:
        base.update(
            name=meta["title"], original_name=meta["title"], first_air_date=date
        )
    return base


def tmdb_response(path: str, q: dict[str, list[str]]) -> tuple[int, dict]:
    parts = [p for p in path.split("/") if p]
    # parts: ['tmdb', '3', ...]
    rest = parts[2:]
    if rest[:2] in (["search", "movie"], ["search", "tv"]):
        want = "movie" if rest[1] == "movie" else "show"
        needle = (q.get("query") or [""])[0].lower()
        res = [
            _tmdb_entry(t, m)
            for t, m in TITLES.items()
            if m["kind"] == want and needle in m["title"].lower()
        ]
        return 200, {"results": res}
    if rest and rest[0] in ("movie", "tv") and len(rest) >= 2 and rest[1].isdigit():
        tmdb = int(rest[1])
        meta = TITLES.get(tmdb)
        if meta is None:
            return 404, {"status_message": "not found"}
        if len(rest) == 3 and rest[2] == "release_dates":
            return 200, {
                "results": [
                    {
                        "iso_3166_1": "US",
                        "release_dates": [
                            {
                                "type": 4,
                                "release_date": f"{meta['year']}-06-15T00:00:00.000Z",
                            }
                        ],
                    }
                ]
            }
        if len(rest) == 4 and rest[2] == "season":
            n = int(rest[3])
            return 200, {
                "season_number": n,
                "air_date": f"{meta['year'] + n - 1}-01-10",
                "episodes": [
                    {
                        "episode_number": e,
                        "air_date": f"{meta['year'] + n - 1}-01-{9 + e:02d}",
                    }
                    for e in range(1, 9)
                ],
            }
        d = _tmdb_entry(tmdb, meta)
        d["alternative_titles"] = {"titles": [], "results": []}
        d["translations"] = {"translations": []}
        if meta["kind"] != "movie":
            d["number_of_seasons"] = meta["seasons"]
            d["seasons"] = [
                {
                    "season_number": n,
                    "episode_count": 8,
                    "air_date": f"{meta['year'] + n - 1}-01-10",
                }
                for n in range(1, meta["seasons"] + 1)
            ]
            d["status"] = "Returning Series"
        return 200, d
    if rest[:1] == ["find"]:
        return 200, {"movie_results": [], "tv_results": []}
    return 404, {"status_message": "unsupported"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a: object) -> None:  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/poster/"):
            self._send(200, poster_svg(Path(u.path).stem), "image/svg+xml")
        elif u.path == "/newznab/api":
            if (q.get("t") or [""])[0] == "get":
                self._send(
                    200,
                    b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">'
                    b'<file poster="demo" date="1" subject="demo [1/1] &quot;a.rar&quot; yEnc (1/1)">'
                    b"<groups><group>alt.binaries.demo</group></groups>"
                    b'<segments><segment bytes="1" number="1">demo@demo</segment></segments></file></nzb>',
                    "application/x-nzb",
                )
            else:
                self._send(
                    200, json.dumps(search_results(q)).encode(), "application/json"
                )
        elif u.path.startswith("/torbox/"):
            self._send(
                200,
                json.dumps({"success": True, "detail": "demo", "data": []}).encode(),
                "application/json",
            )
        elif u.path.startswith("/tmdb/"):
            code, data = tmdb_response(u.path, q)
            self._send(code, json.dumps(data).encode(), "application/json")
        else:
            self._send(404, b"{}", "application/json")


def start(host: str = "0.0.0.0", port: int = 9100) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    start()
    threading.Event().wait()
