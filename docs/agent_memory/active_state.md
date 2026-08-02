# Active State — NZBoxer

**Last Updated:** 2026-08-02  
**Current Phase:** Phase 1 — Bootstrap & Core Data Layer

---

## ✅ Completed

- [x] `AGENT_INSTRUCTIONS.md` created (Master Anchor)
- [x] `/docs/agent_memory/` bootstrapped with all base files
- [x] `config.yaml` created (scoring matrix, API keys placeholder, filters)
- [x] `app/db/models.py` created (MediaItem, Season, DownloadHistory schemas)
- [x] `requirements.txt` created
- [x] `.env.example` created
- [x] `app/__init__.py`, `app/db/__init__.py`, `app/services/__init__.py`, `app/core/__init__.py` stubs created
- [x] Initial Git repository initialized, first commit made

---

## 🔄 In Progress

- [ ] `app/main.py` — FastAPI application, router registration, APScheduler wiring
- [ ] `app/db/database.py` — SQLAlchemy engine, session factory, table creation
- [ ] `templates/base.html` — Base Jinja2 template (TailwindCSS CDN, HTMX)
- [ ] `templates/dashboard.html` — Watchlist overview with status badges

---

## 📋 Next Steps (Ordered)

1. Write `app/db/database.py` (async engine + session)
2. Write `app/main.py` (FastAPI app, lifespan, router stubs)
3. Scaffold all service files (`simkl.py`, `tmdb.py`, `treasure_maps.py`, `torbox.py`)
4. Implement `app/core/parser.py` (guessit integration)
5. Implement `app/core/scorer.py` (config.yaml-driven scoring)
6. Implement `app/core/automation.py` (search/download/upgrade logic)
7. Build templates and HTMX routes
8. Write unit tests for parser and scorer
9. Write integration tests for API clients (with mocks)
10. Run linters, fix all issues, commit

---

## 🐛 Known Bugs / Blockers

_None at this time._

---

## 🏷️ Current Git Branch

`main`
