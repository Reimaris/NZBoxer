# Demo instance for README screenshots

A fully fictional, offline NZBoxer instance, so screenshots never show a real library.
Nothing here talks to a real service: TMDB, the indexer and TorBox are served by
`mock_services.py`, and every watchlist provider syncs manually only.

## Start

```bash
.venv/bin/python demo/seed_demo_data.py          # (re)build demo/config/nzboxer.db
docker compose -f demo/compose.demo.yaml up -d --build
```

- App: <http://localhost:8002/> (dry-run is on, so pushes are simulated)
- Mock services and posters: <http://localhost:9100/> (the browser loads posters from here)

Stop with `docker compose -f demo/compose.demo.yaml down`. Re-seeding replaces the database file, so restart the container afterwards (`docker restart nzboxer-demo`).

## Screenshot checklist

Run down this list top to bottom. Desktop shots use responsive mode at 1440x900, mobile
shots use the Pixel 9 preset. Save each file to `screenshots/` under the name shown.

1. [ ] Seed and start the demo (see [Start](#start)); confirm the top-left badge reads *demo mode* and the yellow dry-run banner shows.
2. [ ] Browser responsive mode (F12, Ctrl+Shift+M): set the size to **1440x900**.
3. [ ] `dashboard.png`: dashboard, *Movies*/*Series* tab with every category card visible (hold counts, status legend).
4. [ ] `push-modal.png`: open *The Glass Orchard* → push modal, click *Search* so scored releases are listed.
5. [ ] `anime-franchise.png`: Anime tab → *Skybound Tide* → push modal (seasons plus the movie entry).
6. [ ] `active-pushes.png`: *Active Pushes* card (two simulated transfers, 62 %, dry-run banner visible).
7. [ ] `providers.png`: Settings → Providers (Simkl, AniList, Jellyseerr, TMDB, TorBox, SABnzbd, indexer).
8. [ ] `add-provider.png`: Settings → Providers → *Add Provider* dialog, watchlist category visible (capability pills and Experimental pills on the Jellyseerr/Overseerr cards).
9. [ ] `capabilities-matrix.png`: Settings → *Capabilities Matrix*.
10. [ ] `ad-hoc-search.png`: *Manual Search* card, query `neon`, then *Search* on a result so releases show.
11. [ ] Switch responsive mode to the **Pixel 9** preset (this is the only size change; desktop shots are done).
12. [ ] `mobile-dashboard.png`: dashboard.
13. [ ] `mobile-push-modal.png`: push modal.
14. [ ] Review each image for anything personal (bookmarks, tabs, usernames, real hostnames); crop to the page, not the browser chrome.
15. [ ] Strip metadata and compress, e.g. `python3 -c "from PIL import Image; import sys; [Image.open(f).save(f, optimize=True) for f in sys.argv[1:]]" screenshots/*.png` (re-saving with Pillow drops EXIF/text chunks).
16. [ ] If new images were added (`capabilities-matrix.png`, `add-provider.png`), reference them in the main README gallery.
17. [ ] Commit (`docs: refresh screenshots`), then `docker compose -f demo/compose.demo.yaml down`.

## What it contains

`catalog.py` defines the library (all titles invented):

| Screenshot | Use |
|---|---|
| Dashboard | Movies and series in every state, an anime franchise with movie and seasons, an on-hold title and a series awaiting its next season |
| Push modal | Any "ready" title, e.g. *The Glass Orchard* or *The Lantern Keeper* (scored mock releases) |
| Anime franchise | *Skybound Tide* (seasons plus a movie entry) |
| Active pushes | *A Quiet Harvest* and *Paper Lanterns* S2 (simulated, 62 %) |
| Ad-hoc search | Search `neon` (titles that are not on any watchlist) |
| Providers / Capabilities Matrix | Simkl, AniList, Jellyseerr, TMDB, TorBox, SABnzbd, one indexer |

To change the data, edit `catalog.py` and re-run the seed script.
