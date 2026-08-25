# 04: Dashboard Attempt Progress Badges & Cutoff-Unmet Sub-Labels

**What to build:**
Provide clear dashboard and drilldown feedback regarding upgrade attempt progression and completion reasons. Items in `DOWNLOADED` status display an active progress badge (e.g. `Upgrade: X/10`) indicating how many attempts have run. Items in `COMPLETED` status that finished due to reaching the attempt limit without meeting the target cutoff score (or highest format tier) display an informative sub-label beneath their status badge (e.g. `Cutoff nicht erreicht (10/10 Versuche)`).

**Blocked by:** 
- 02: Target Cutoff Standardization & Video Automation Upgrade Attempt Exhaustion
- 03: Print Media Upgrade Attempt Exhaustion

**Status:** done

- [x] Video Dashboard cards in `templates/dashboard.html` render `Upgrade X/Y` badge when status is `DOWNLOADED`
- [x] Video Dashboard cards render a `Cutoff nicht erreicht (X/Y Versuche)` sub-label beneath the `COMPLETED` badge when `best_score < target_score`
- [x] Season and Episode rows in `templates/item_detail.html` render upgrade attempt badges and cutoff-unmet sub-labels
- [x] Print Dashboard cards in `templates/print_dashboard.html` for Books render matching progress badges and cutoff-unmet sub-labels
- [x] Manga Volume drawer in `templates/partials/manga_volumes.html` renders upgrade badges and cutoff-unmet sub-labels for volumes
- [x] UI correctly reflects real-time attempt counts and handles localized text cleanly
