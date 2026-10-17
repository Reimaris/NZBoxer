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

Stop with `docker compose -f demo/compose.demo.yaml down`. Re-seeding replaces the database
file, so restart the container afterwards (`docker restart nzboxer-demo`).

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
