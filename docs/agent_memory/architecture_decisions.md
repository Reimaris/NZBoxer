# Architecture Decision Records — NZBoxer

**Last Updated:** 2026-08-02

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

## ADR-005: config.yaml for Scoring Matrix and User Configuration

**Status:** Accepted  
**Date:** 2026-08-02

### Context
The scoring rules (codec weights, resolution weights, group whitelist/blacklist, cutoff scores) must be user-editable without code changes.

### Decision
Store all scoring parameters and automation settings in `config.yaml`. Load at startup into a Pydantic settings model. Watch for changes or reload on demand.

### Consequences
- **Pros:** Human-readable, Git-diffable, no DB round-trip for config.
- **Cons:** Requires app restart (or explicit reload endpoint) to apply changes. Acceptable for this use case.

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
