# Project Structure — NZBoxer

**Last Updated:** 2026-08-02

---

## Directory Tree

```
NZBoxer/
│
├── AGENT_INSTRUCTIONS.md          # Master anchor — read first every session
├── README.md                      # (TODO) Human-facing project overview
├── usage.md                       # How to install, run, test (symlinked from docs)
├── requirements.txt               # Python dependencies (pinned)
├── .gitignore
│
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI app, routers, APScheduler lifespan
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── database.py            # SQLAlchemy async engine, session factory
│   │   └── models.py              # ORM models: MediaItem, Season, DownloadHistory
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── simkl.py               # Simkl API client (watchlist sync)
│   │   ├── tmdb.py                # TMDB API client (release dates, metadata)
│   │   ├── treasure_maps.py       # Newznab/Treasure Maps API client
│   │   └── torbox.py              # TorBox API client (cache check, NZB upload)
│   │
│   └── core/
│       ├── __init__.py
│       ├── parser.py              # guessit-based NZB title parser
│       └── automation.py          # Orchestration: search, score, upgrade, send
│
├── templates/
│   ├── base.html                  # Base layout (TailwindCSS CDN, HTMX)
│   ├── dashboard.html             # Watchlist overview with status badges
│   ├── item_detail.html           # Per-item detail + season toggles
│   └── search_results.html        # Manual search results (HTMX partial)
│
├── static/
│   └── favicon.ico                # (TODO)
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                # Shared fixtures (in-memory DB, mock HTTP)
│   ├── test_parser.py             # Unit tests for parser.py
│   ├── test_scorer.py             # Unit tests for scorer.py
│   ├── test_automation.py         # Integration tests for automation.py
│   └── test_api_routes.py         # FastAPI route tests (TestClient)
│
└── docs/
    └── agent_memory/
        ├── active_state.md        # Current tasks and next steps
        ├── project_structure.md   # THIS FILE
        ├── architecture_decisions.md
        └── usage.md
```

---

## Module Dependency Graph

```
main.py
  ├── db/database.py → db/models.py
  ├── services/simkl.py
  ├── services/tmdb.py
  ├── services/treasure_maps.py
  ├── services/torbox.py
  └── core/automation.py
        ├── core/parser.py (→ guessit)
        ├── services/treasure_maps.py
        ├── services/torbox.py
        └── db/models.py
```

---

## Key External Dependencies

| Package        | Role                          | Version Target |
|----------------|-------------------------------|----------------|
| fastapi        | Web framework                 | >=0.111        |
| uvicorn        | ASGI server                   | >=0.30         |
| sqlalchemy     | ORM (async)                   | >=2.0          |
| aiosqlite      | Async SQLite driver           | >=0.20         |
| httpx          | Async HTTP client             | >=0.27         |
| apscheduler    | Task scheduling               | >=3.10         |
| guessit        | Media metadata parsing        | >=3.8          |
| jinja2         | HTML templating               | >=3.1          |
| python-multipart | Form data support           | >=0.0.9        |
| pytest         | Testing framework             | >=8.0          |
| pytest-asyncio | Async test support            | >=0.23         |
| httpx          | TestClient support            | >=0.27         |
| ruff           | Linter + formatter            | >=0.4          |
| mypy           | Static type checker           | >=1.10         |
