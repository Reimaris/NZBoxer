# Active State — NZBoxer

**Last Updated:** 2026-08-03  
**Current Phase:** Maintenance & Feature Extensions Refactored

---

## ✅ Completed

- [x] `AGENT_INSTRUCTIONS.md` created & updated with Rate Limiting, Logging, and UI layout rules.
- [x] Central Rate Limiters & HTTP 429 backoffs implemented (`RateLimiter` async lock class):
  - Newznab: 0.6s politeness cooldown
  - TMDB: 0.1s rate limit
  - Simkl: 1.0s query cooldown
  - TorBox: 1.0s send spacing, 10.0s status polling interval
- [x] Daily Grab Tracking (`DailyGrabCounter` DB model):
  - Hard limit of 400 NZB grabs/day enforced in `automation.py`.
- [x] Daily Rotating File Logging & Process Block Separators:
  - `TimedRotatingFileHandler` with `YYYY-MM-DD` filenames.
  - `log_process_start` and `log_process_end` block headers added to automation & self-healing runs.
- [x] Dashboard UI & Stats Refactoring:
  - History card removed.
  - Movies, Series, and Anime cards expanded with metric breakdowns (Total, Wanted, Completed, Ignored).
  - Anime Series & Anime Movies consolidated into a unified "Anime" category badge and single tab.
- [x] UI Symmetry & Accordion Layout:
  - Provider Modal (`templates/modals/provider.html`): Symmetrical 2-column grid layout for form inputs.
  - Item Detail View (`templates/item_detail.html`): Horizontal Season Accordion UI displaying collapsible episode lists.
- [x] Test Suite & Verification:
  - Added `tests/test_rate_limits.py`.
  - 14/14 tests passing on `pytest`.
  - Zero errors on `ruff check .` and `mypy app`.
- [x] ID-Mapping & Anime Fallbacks:
  - Added `mal_id` and `anilist_id` to `MediaItem` DB model.
  - Parsed `mal` and `anilist` IDs from Simkl API during watchlist sync.
  - Added fallback search logic in `automation.py` (`_process_movie`, `_process_season`) and `treasure_maps.py` to search by IMDB, TMDB, and Title.
  - Anime primarily uses indexer category 5070 and Anime absolute episode title search fallback.
- [x] Git Push Rule enforced in instructions.

---

## 🔄 In Progress

- Wait for DB recreation and test passing to push changes to Git.

---

## 📋 Next Steps (Ordered)

1. Monitor system in production / user feedback.

---

## 🐛 Known Bugs / Blockers

None.

---

## 🏷️ Current Git Branch

`main`
