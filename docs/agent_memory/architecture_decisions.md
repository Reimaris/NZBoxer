# Architecture Decision Records — NZBoxer

**Last Updated:** 2026-08-03

---

## ADR-001: SQLite + SQLAlchemy 2.0 Async as Database Layer

**Status:** Accepted  
**Date:** 2026-08-02

### Context
The application is a lightweight, single-user autonomous service. A full PostgreSQL deployment would be operationally heavy for this use case.

### Decision
Use SQLite via `aiosqlite` with `SQLAlchemy 2.0` in async mode. The 2.0 API uses `AsyncSession` and `AsyncEngine`, providing full async/await compatibility with FastAPI's async request handlers and the APScheduler background jobs.

### Consequences
- **Pros:** Zero-dependency deployment (no DB server), fast for single-user workloads, file-based (easy backup/migration).
- **Cons:** Not suitable for multi-writer concurrent scenarios (acceptable for this use case). WAL mode enabled to improve concurrent reads.

---

## ADR-002: APScheduler (3.x) for Background Automation

**Status:** Accepted  
**Date:** 2026-08-02

### Context
The service needs periodic background jobs (e.g., every 30 min) to search Usenet indexers and upgrade downloads.

### Decision
Use `APScheduler >= 3.10` with the `AsyncIOScheduler`. Start/stop the scheduler via FastAPI's `lifespan` context manager to ensure clean startup and shutdown.

### Consequences
- **Pros:** Mature, well-documented, supports cron and interval triggers, integrates cleanly with asyncio.
- **Cons:** APScheduler 4.x has a redesigned API; pinning to 3.x for stability while 4.x matures.

---

## ADR-003: HTMX + TailwindCSS (CDN) for Frontend

**Status:** Accepted  
**Date:** 2026-08-02

### Context
The frontend should be dynamic (partial page updates, toggles, search results) without the operational overhead of a JS framework build pipeline.

### Decision
Use HTMX for declarative AJAX interactions (hx-get, hx-post, hx-swap) with Jinja2 templates on the FastAPI backend. TailwindCSS loaded via CDN for rapid styling with no build step.

### Consequences
- **Pros:** No Node.js, no bundler, no JS framework state management. Backend renders HTML partials directly. Extremely low operational complexity.
- **Cons:** CDN TailwindCSS is ~110KB (acceptable). No tree-shaking in CDN mode (acceptable for internal tool).

---

## ADR-004: guessit for Release Name Parsing

**Status:** Accepted  
**Date:** 2026-08-02

### Context
NZB release names follow complex, informal naming conventions (e.g., `Movie.Name.2023.2160p.UHD.BluRay.x265.DTS-HD.MA.7.1-GROUP`). Manual regex would be brittle.

### Decision
Use the `guessit` library, the industry standard for media metadata extraction from filenames. It returns structured dicts with `title`, `year`, `screen_size`, `video_codec`, `audio_codec`, `release_group`, etc.

### Consequences
- **Pros:** Battle-tested against thousands of real-world release name patterns.
- **Cons:** Occasionally mis-identifies ambiguous tokens; scorer must handle `None` values gracefully.

---

## ADR-005: DB-backed Config & Scoring System

**Status:** Accepted  
**Date:** 2026-08-02

### Context
The scoring rules (codec weights, resolution weights, group whitelist/blacklist, cutoff scores) must be user-editable without code changes.

### Decision
Store global system settings and scoring profiles in the `SystemSettings` table in SQLite, loaded into memory at startup and updated via the settings endpoints.

### Consequences
- **Pros:** Dynamic settings adjustments directly via UI without application restart.
- **Cons:** Requires reloading cache upon update.

---

## ADR-006: Separation of Simkl Watchlist into Local SQLite Cache

**Status:** Accepted  
**Date:** 2026-08-02

### Context
Querying the Simkl API live on every dashboard page load would be slow and rate-limited.

### Decision
Sync the Simkl watchlist to `MediaItem` rows in SQLite during startup and via scheduled job. The frontend reads only from local DB. A manual "Sync Now" button triggers an on-demand sync.

### Consequences
- **Pros:** Dashboard loads instantly. No rate-limit risk. Enables status tracking independent of Simkl.
- **Cons:** Watchlist can be stale by up to one scheduler interval. Acceptable tradeoff.

---

## ADR-007: Central Async Rate Limiting & Daily NZB Grab Limit

**Status:** Accepted  
**Date:** 2026-08-03

### Context
External APIs enforce distinct rate limits (Newznab politeness limits, TMDB rate limits, Simkl query limits, TorBox polling limits). Treasure Maps imposes a strict 400 NZB downloads/day limit.

### Decision
Implement a central `RateLimiter` class using `asyncio.Lock` to guarantee minimum delays between HTTP requests per client service. Track daily NZB downloads in a `DailyGrabCounter` DB model (`YYYY-MM-DD` key). Check `can_grab_today(session, limit=400)` before sending any download request.

### Consequences
- **Pros:** Prevents API bans, respects provider terms, prevents quota overflow.
- **Cons:** Requires small delay overhead per request cycle (0.1s - 1.0s).

---

## ADR-008: Daily Rotating File Logging & Process Block Formatters

**Status:** Accepted  
**Date:** 2026-08-03

### Context
Logs must be easily inspectable per day and visually distinguishable during automated background runs.

### Decision
Configure `TimedRotatingFileHandler` writing to `logs/nzboxer_YYYY-MM-DD.log`. Implement `log_process_start` and `log_process_end` block formatters with 80-character line separators for automation and self-healing cycles.

### Consequences
- **Pros:** Clean log inspection, automatic log rotation, clear visual boundaries between background runs.
