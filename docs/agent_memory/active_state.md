# Active State — NZBoxer

**Last Updated:** 2026-08-03  
**Current Phase:** Phase 3 — Testing & Quality Assurance

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
- [x] `app/db/database.py` — SQLAlchemy engine, session factory, table creation
- [x] `app/main.py` — FastAPI application, router registration, APScheduler wiring
- [x] Scaffold all service files (`simkl.py`, `tmdb.py`, `treasure_maps.py`, `torbox.py`)
- [x] Implement `app/core/parser.py` (guessit integration)
- [x] Implement `app/core/scorer.py` (config.yaml-driven scoring)
- [x] Implement `app/core/automation.py` (search/download/upgrade logic)
- [x] Build templates and HTMX routes

---

## 🔄 In Progress

- [ ] Write unit tests for parser and scorer
- [ ] Write integration tests for API clients (with mocks)

---

## 📋 Next Steps (Ordered)

1. Write `tests/test_parser.py`
2. Write `tests/test_scorer.py`
3. Write `tests/conftest.py` with mock fixtures
4. Write `tests/test_automation.py` (integration tests)
5. Request sandbox network bypass or local linter run for `ruff` / `mypy` / `pytest`.

---

## 🐛 Known Bugs / Blockers

- Sandbox has no network access (`Temporärer Fehler bei der Namensauflösung`). Cannot install `requirements.txt` or run `pytest` / `ruff` / `mypy` automatically without a network bypass.

---

## 🏷️ Current Git Branch

`main`
