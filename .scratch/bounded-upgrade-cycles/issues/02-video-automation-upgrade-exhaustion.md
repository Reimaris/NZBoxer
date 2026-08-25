# 02: Target Cutoff Standardization & Video Automation Upgrade Attempt Exhaustion

**What to build:**
Standardize target cutoff score defaults to 8000 across all services. In the background video automation cycle, items in `DOWNLOADED` status increment their `upgrade_attempts_count` whenever a search cycle runs without discovering an upgraded release that meets or exceeds `current_score + upgrade_threshold`. Once `upgrade_attempts_count >= max_upgrade_attempts`, the item transitions to `COMPLETED`, permanently halting automatic cyclic queries to preserve indexer quotas. If a qualifying upgrade is found and grabbed, `upgrade_attempts_count` resets to 0. Manual "Search Now" clicks on `COMPLETED` items execute one-off searches without reverting the item's completed status unless a new release is grabbed.

**Blocked by:** 01: Data Model Migration & Configurable Max Upgrade Attempts Setting

**Status:** done

- [x] Target cutoff score fallback default is unified to 8000 across `app/core/self_healing.py` and `app/core/automation.py`
- [x] Automated search cycles increment `upgrade_attempts_count` on `DOWNLOADED` Movies, Seasons, and Episodes when no qualifying upgrade candidate is found
- [x] Movies, Seasons, and Episodes automatically transition from `DOWNLOADED` to `COMPLETED` when `upgrade_attempts_count >= max_upgrade_attempts`
- [x] Grabbing a new release or upgrade resets `upgrade_attempts_count` to 0 and updates `best_score`
- [x] Triggering manual search on a `COMPLETED` item runs a one-time search and does not alter its completed state unless a new upgrade is grabbed
- [x] Comprehensive unit and integration tests verify attempt incrementation, limit enforcement, transition to `COMPLETED`, and reset behavior
