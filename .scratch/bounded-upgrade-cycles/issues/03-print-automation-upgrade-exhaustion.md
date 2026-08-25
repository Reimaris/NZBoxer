# 03: Print Media Upgrade Attempt Exhaustion

**What to build:**
Extend upgrade attempt tracking and automated search capping to print media (Books and Manga Volumes). When a book or manga volume is in `DOWNLOADED` status with a format below the top tier (e.g. PDF when EPUB/CBZ is preferred), automated background cycles increment `upgrade_attempts_count` when no higher-ranked format release is found. Once `upgrade_attempts_count >= max_upgrade_attempts`, the book or manga volume transitions to `COMPLETED`, halting automated background queries. Grabbing a format upgrade resets the counter to 0.

**Blocked by:** 01: Data Model Migration & Configurable Max Upgrade Attempts Setting

**Status:** done

- [x] Background print automation cycle in `app/core/automation.py` increments `upgrade_attempts_count` for `DOWNLOADED` `BookItem` records when no format upgrade is discovered
- [x] Background print automation cycle increments `upgrade_attempts_count` for `DOWNLOADED` `MangaVolume` records when no format upgrade is discovered
- [x] `BookItem` and `MangaVolume` records automatically transition from `DOWNLOADED` to `COMPLETED` once `upgrade_attempts_count >= max_upgrade_attempts`
- [x] Successfully grabbing a format upgrade resets `upgrade_attempts_count` to 0
- [x] Unit and integration tests verify attempt tracking and exhaustion transitions for books and manga volumes
