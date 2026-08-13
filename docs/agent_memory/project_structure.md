# Project Structure — NZBoxer

**Last Updated:** 2026-08-03

---

## Directory Tree

```
NZBoxer/
│
├── AGENT_INSTRUCTIONS.md          # Master anchor — read first every session
├── README.md                      # Human-facing project overview
├── usage.md                       # How to install, run, test (symlinked from docs)
├── pyproject.toml                 # Tool configuration (mypy, pytest, ruff)
├── requirements.txt               # Python dependencies (pinned)
├── .gitignore
│
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI app, routers, APScheduler lifespan, settings endpoints
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── database.py            # SQLAlchemy async engine, session factory
│   │   ├── models.py              # ORM models: MediaItem, Season, Episode, DownloadHistory, DailyGrabCounter
│   │   └── grab_tracker.py        # Daily NZB grab tracking (limit: 400/day)
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── simkl.py               # Simkl API client (1.0s rate limited)
│   │   ├── tmdb.py                # TMDB API client (0.1s rate limited)
│   │   ├── treasure_maps.py       # Newznab/Treasure Maps API client (0.6s rate limited)
│   │   ├── torbox.py              # TorBox API client (1.0s send, 10.0s poll rate limited)
│   │   └── telegram.py            # Telegram notifications
│   │
│   └── core/
│       ├── __init__.py
│       ├── rate_limiter.py        # Thread/task-safe async RateLimiter class
│       ├── logging_config.py      # Daily rotating file logger & log_process_start/end block formatters
│       ├── parser.py              # guessit-based NZB title parser
│       ├── scorer.py              # Release scoring engine
│       ├── default_scoring.py     # Default scoring profile configuration
│       ├── self_healing.py        # Self-healing engine for broken downloads
│       └── automation.py          # Orchestration: search, score, upgrade, send
│
├── templates/
│   ├── base.html                  # Base layout (TailwindCSS CDN, HTMX, AlpineJS)
│   ├── dashboard.html             # Watchlist overview (3 stats cards, unified Anime tab)
│   ├── item_detail.html           # Item detail view (horizontal Season Accordion UI)
│   └── modals/
│       ├── provider.html          # Provider modal (symmetrical 2-column grid layout)
│       └── notification.html      # Notification modal
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                # Shared fixtures (in-memory DB, mock HTTP)
│   ├── test_parser.py             # Unit tests for parser.py
│   ├── test_scorer.py             # Unit tests for scorer.py
│   ├── test_rate_limits.py        # Unit tests for RateLimiter and DailyGrabCounter
│   ├── test_automation.py         # Integration tests for automation.py
│   └── test_settings.py           # FastAPI settings endpoint tests
│
└── docs/
    └── agent_memory/
        ├── active_state.md        # Current tasks and state
        ├── project_structure.md   # THIS FILE
        ├── architecture_decisions.md
        └── usage.md
```

---

## Module Dependency Graph

```
main.py
  ├── db/database.py → db/models.py, db/grab_tracker.py
  ├── core/rate_limiter.py
  ├── core/logging_config.py
  ├── services/simkl.py
  ├── services/tmdb.py
  ├── services/treasure_maps.py
  ├── services/torbox.py
  ├── core/self_healing.py
  └── core/automation.py
        ├── core/parser.py (→ guessit)
        ├── core/scorer.py
        ├── db/grab_tracker.py
        ├── services/treasure_maps.py
        ├── services/torbox.py
        └── db/models.py
```
