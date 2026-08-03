# Active State — NZBoxer

**Last Updated:** 2026-08-03  
**Current Phase:** Phase 3 — Testing & Quality Assurance

---

## ✅ Completed

- [x] `AGENT_INSTRUCTIONS.md` created (Master Anchor)
- [x] `/docs/agent_memory/` bootstrapped with all base files
- [x] `app/db/models.py` created (MediaItem, Season, DownloadHistory schemas)
- [x] `requirements.txt` created
- [x] `app/__init__.py`, `app/db/__init__.py`, `app/services/__init__.py`, `app/core/__init__.py` stubs created
- [x] Initial Git repository initialized, first commit made
- [x] `app/db/database.py` — SQLAlchemy engine, session factory, table creation
- [x] `app/main.py` — FastAPI application, router registration, APScheduler wiring
- [x] Scaffold all service files (`simkl.py`, `tmdb.py`, `treasure_maps.py`, `torbox.py`)
- [x] Implement `app/core/parser.py` (guessit integration)
- [x] Implement `app/core/automation.py` (search/download/upgrade logic)
- [x] Build templates and HTMX routes
- [x] Phase 3: Testing & Quality Assurance
  - [x] `tests/conftest.py` with mock db/fixtures
  - [x] Unit tests for `parser.py` and `scorer.py`
  - [x] Integration tests for `automation.py`
  - [x] 100% pass on pytest
  - [x] zero errors in `mypy` and `ruff`
- [x] Phase 4: Finalizing & Documentation (Handoff)
  - [x] Write final instructions for the user (deployment docs)
  - [x] Summarize findings
  - [x] Hand off the project

**Next Action:** Project is completely finished and ready for handoff.

---

## 🔄 In Progress

---

## 📋 Next Steps (Ordered)

---

## 🐛 Known Bugs / Blockers

None.

---

## 🏷️ Current Git Branch

`main`
