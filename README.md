<p align="center">
  <img src="app/static/icon.svg" width="128" height="128" alt="NZBoxer Logo" />
</p>

<h1 align="center">NZBoxer</h1>

<p align="center">
  <strong>Self-hosted watchlist and on-demand Usenet push companion for Simkl, AniList, Plex and MyAnimeList watchlists, Newznab indexers, and TorBox, SABnzbd or Premiumize.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-v1.1.1-pink" alt="Version" />
  <img src="https://img.shields.io/badge/docker-ready-blue" alt="Docker Ready" />
  <img src="https://img.shields.io/badge/license-AGPLv3-green" alt="License" />
</p>

---

## What is NZBoxer?

**NZBoxer** bridges your watchlists (**Simkl**, **AniList**, **Plex**, **MyAnimeList**), **TMDB** & **AniList** metadata, **Newznab** Usenet indexers, and your downloader (**TorBox**, **SABnzbd** or **Premiumize**) into a single lightweight web application.

NZBoxer is built around an **on-demand workflow**:
- It keeps your **Movies**, **TV Series**, and **Anime** synchronized with your watchlist providers and tracks exact digital release dates, episode air dates, and chronological anime watch orders.
- It performs **zero background indexer polling** while idle—preserving your indexer API limits and downloader storage until you actually want to push a movie, season, or episode.
- When you decide to push a title, NZBoxer searches your configured indexers, executes rigorous **3-layer fake detection**, scores releases against your quality and language rules, sends the NZB to your downloader, monitors the transfer in real time, and automatically replaces dead or stalled downloads if a release fails.

---

## How It Works

### 1. Watchlist Sync & Metadata Enrichment
- **Simkl Integration:** Connects to your Simkl account (via OAuth 2.0 Device Flow) and syncs your *Plan to Watch* and *Watching* lists across Movies, TV Shows, and Anime.
- **Multiple Watchlist Providers:** Besides Simkl, NZBoxer can sync your anime/movie/TV lists from **AniList** (username, optional personal access token for private profiles), **Plex** (PIN login or manual auth token) and **MyAnimeList** (OAuth 2.0 PKCE). Providers can be combined; an item stays on your dashboard while at least one provider keeps it active. Periodic and page-load syncs apply to every connected watchlist, and after each sync NZBoxer runs one shared maintenance pass (anime sequel consolidation, TBA season discovery, duplicate merging and pruning of titles no list keeps active), regardless of provider. **MyAnimeList requires its redirect URI** — `<your NZBoxer base URL>/settings/provider/mal/auth/callback`, shown in the provider dialog — to be registered in your MAL API app.
- **Watch Progress Across Providers:** When the same title is on several watchlists, the **furthest watch progress wins** for Auto-Advance, and a less advanced provider never rolls it back. Simkl, AniList and MyAnimeList report watch progress; **Plex's watchlist carries none by design**, but progress from your other providers still applies. Turning off *Sync movies / series / anime* for a provider skips that category without completing or removing titles already imported.
- **First-Run Wizard:** Step 1 offers a **Watchlist** dropdown (None / Skip, Simkl, AniList, Plex, MyAnimeList) that shows only the selected provider's credential fields; Step 3 diagnostics verify the chosen provider. A **Downloader** dropdown (None / Skip, TorBox, SABnzbd, Premiumize) shows only the selected downloader's fields and makes it the single active downloader; Step 3 diagnostics test whichever downloader is active. An **Indexer** dropdown (None / Skip, TreasureMaps, DrunkenSlug, NZBGeek, NzbPlanet, AltHUB, NinjaCentral, Tabula Rasa, NZBFinder, NZB.su, DogNZB, or Custom Newznab) prefills the name and API URL and links to the indexer's API key page.
- **Release Date Tracking (TMDB):** Enriches Movies and Series with digital/physical release dates and season/episode air dates so you always know whether a title is actually available or still upcoming.
- **Chronological Anime Franchises (AniList):** Automatically groups standalone anime sequel entries and canon franchise movies under a single parent entry in chronological watch order, while keeping your watch progress aligned.
- **Future Episode & Season Detection:** Unaired episodes are tracked as *Future* with timezone-aware broadcast times and roll over to *Searching* automatically once they air. Upcoming TBA seasons are discovered on a paced schedule.
- **Paced Air Date Refresh:** Air dates of unreleased movies and series are re-checked every 24 hours, or every 3 hours for releases due within 14 days, and frozen for completed or ignored items to save API calls. Episode air times come from tiers: anime uses exact AniList airing times; TV uses Simkl's exact broadcast time when Simkl is connected, otherwise the TMDB calendar date plus a 24-hour buffer (so Plex/MAL-only TV episodes become eligible about a day after their date). A precise time is never downgraded to a date.
- **Automatic Cleanup:** When you mark an item as *Completed* or drop it on your watchlist, NZBoxer automatically removes it from your active watchlist views on the next sync while preserving your permanent push history.

### 2. On-Demand Search, Scoring & Push
- **One-Click Auto-Push:** Push an entire movie, one or more seasons, or specific episodes with a single click. Season pack acquisition rules (*Episodes Only*, *Score Decides*, *Prefer Season Pack*) are governed directly by your active Search Preset with automatic episode-level fallback.
- **Opt-In Air-Date Auto-Push:** Tick **Auto-Push New Episodes** in a series' push modal to push newly aired episodes automatically after a configurable air delay. A backlog guard stops old, long-aired episodes from being pushed in bulk, and the check runs hourly right after the watchlist sync.
- **Time-Bounded Episode Upgrades:** After the first push, an episode is eligible for better releases during a configurable upgrade window, up to a configurable maximum number of upgrades per episode. The old release stays on the downloader until the upgrade finishes. If the upgrade fails or is canceled, NZBoxer reverts to the original release. Upgraded pushes are marked with an `UPGRADE` badge, and upgrade notifications are rolled up per show.
- **History Autoclearing:** The local Push History Ledger removes finished entries older than a configurable retention period (default 30 days, or never) and can optionally drop entries whose title was pruned from your watchlist. Only the local ledger is affected; transfers on your downloader are never deleted.
- **Pre-Flight Inspection & Strict Episode Replacement:** NZB payloads are verified in memory before dispatch. If a single episode push fails, NZBoxer replaces strictly that episode without unwanted escalation to full season packs.
- **Manual Release Picker:** Prefer to choose the exact release yourself? Run an interactive search directly from the item modal or the standalone Manual Search view to inspect scored candidates, metallic quality feature pills, language tags, file sizes, and rejection reasons before grabbing.
- **Ad-Hoc Multi-Layer Search (Card 6):** Type a title, IMDb ID (`tt…`) or TMDb/AniList ID to browse matching movies, series and anime (TMDB + AniList) as expandable rows. Series and anime lazily load their seasons and episodes. Every row (title, season, episode) has its own **Search** button that opens an inline drawer of scored releases with per-release **Grab / Push** and **Auto-Push Best**. Pushed releases are tracked in the history ledger with an **Ad-Hoc** badge.
- **Built-In Scoring & Search Presets:** Combines a built-in release scoring matrix (evaluating resolution, source, HDR/Dolby Vision, video/audio codecs, Atmos, bitrate density, and primary/fallback language hierarchy) with reusable **Search Presets** and custom overrides.
- **Global Dry-Run Simulation Sandbox:** Safely test push rules, multi-season buffer logic, and auto-advance pipelines without sending anything to your downloader or mutating your library state, complete with amber simulation indicators and auto-expiring simulated history records.

### 3. Security & 3-Layer Fake Detection
- **Layer 1 (Search Metadata):** Rejects password-flagged posts and releases with unrealistic file sizes (e.g., movies < 300MB, 4K < 1.5GB) directly at the indexer query stage.
- **Layer 2 (Pre-Dispatch NZB Payload):** Inspects raw NZB XML segments before dispatching to your downloader, dropping password-protected archives, executable payloads (`.exe`, `.scr`, `.bat`), and sample-only files.
- **Layer 3 (Downloader Filelist & Unpack Grace Window):** Validates completed download filelists on your downloader, purging and auto-replacing fake size stubs (< 150MB movies, < 25MB episodes). For archive containers (`.rar`, `.7z`, `.zip`), NZBoxer grants a 10-minute unpack grace period (`Unpacking` badge) allowing the downloader's extraction to finish before fake verification.
- **Manual & Global Release Blacklisting:** Blacklist problematic releases directly from Active Pushes, Push History, or the Push Modal with remote downloader purging and one-click blacklist reset in Settings.

### 4. Live Transfer Monitoring, Auto-Recovery & Controls
- **Live Active Pushes & 5-Minute Retention:** Tracks live transfer progress, speed, and ETA via an auto-waking Alpine poller. Completed transfers remain visible for 5 minutes (`Ready on <downloader>`) for convenient review, dismissal, or remote deletion.
- **Granular & Global Transfer Abort:** Instantly cancel background push operations and purge active cloud downloads with a single-click global `[Abort All]` or per-item `[Abort Transfer]` controls.
- **Minute-Scale Stalled Download Watchdog:** Configurable stalled transfer timeout (`15m`, `30m`, `60m Default`, `120m`, `240m`) automatically triggers replacement searches if cloud transfers stall.
- **Mobile Push Modal:** On phones the push modal opens as an edge-to-edge sheet with a compact poster header and a docked action footer.
- **Push History Ledger & Batch Management:** Permanent record of all pushes with local system timezone timestamps, row-click multi-selection, and batch deletion controls that clean up local records without deleting downloader files.
- **Downloader Rate-Limit Queue:** Gracefully pauses pushes during downloader or indexer rate limits, resuming automatically in order with consolidated notification alerts.
- **Consolidated Notifications & Error Redaction:** Rich Discord Webhook and Telegram notifications with show-level batch rollups, optional ERROR-level log forwarding, and strict masking of API tokens and secrets.

---

## Prerequisites

- **Docker & Docker Compose** (recommended) or **Python 3.12+**
- Accounts / API Keys for:
  - At least one watchlist provider: [Simkl](https://simkl.com/) (Client ID + interactive OAuth 2.0 Device Flow login in the UI), [AniList](https://anilist.co/), [Plex](https://www.plex.tv/) or [MyAnimeList](https://myanimelist.net/)
  - [TMDB](https://www.themoviedb.org/) (API Key for movie & TV metadata)
  - At least one **Newznab-compatible Usenet Indexer**
  - A downloader: [TorBox](https://torbox.app/) (API Key for Usenet cloud downloads), [SABnzbd](https://sabnzbd.org/) (host, port, API key; it must be reachable from the NZBoxer container via its LAN IP or `host.docker.internal`, not `localhost`) or [Premiumize](https://www.premiumize.me/account) (API key). Only one downloader is active at a time.

**Required to run:** a TMDB API key, at least one indexer and at least one downloader. Until all three are configured in Settings, NZBoxer shows a **Setup required** screen (with a guided setup wizard) and background jobs stay idle. Without a connected watchlist provider the app runs in **Ad-Hoc Search only** mode: the dashboard shows just the Ad-Hoc Search card and watchlist sync is disabled.

> **Upgrade note:** Installs without a TMDB key will see a Setup required screen until one is added in Settings.

- *(Optional)* **Discord Webhook URL** or **Telegram Bot Token & Chat ID** for push and completion notifications (AniList anime metadata requires no API key).

---

## Getting Started

### 1. Installation with Docker (Recommended)

The easiest way to run NZBoxer is with the pre-built Docker image from the GitHub Container Registry (`ghcr.io/reimaris/nzboxer:latest`).

Create a `compose.yaml` file:

```yaml
services:
  nzboxer:
    image: ghcr.io/reimaris/nzboxer:latest
    container_name: nzboxer
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./config:/app/config
    environment:
      - TZ=UTC
```

Then start the container:

```bash
docker compose up -d
```

### 2. Initial Configuration

All configuration is managed directly in the Web UI with live asynchronous auto-saving—no manual "Save" button and no `.env` file required:

1. Open the dashboard at [http://localhost:8000/](http://localhost:8000/).
2. Navigate to **Settings** (top right) to explore the 6 collapsible configuration slices:
   - **Providers:** Connect your **TMDB**, a **downloader** (TorBox, SABnzbd or Premiumize), your **Newznab Indexers**, and one or more **watchlist providers** (e.g. log into Simkl via the OAuth 2.0 Device Flow).
   - **Search Presets:** Customize or create resolution, language, and quality profiles.
   - **Notifications:** Configure Discord webhooks, Telegram bots, and alert event toggles.
   - **System & Automation:** Toggle Debug Mode, Dry-Run Sandbox, adjust stalled timeouts, set the air-push delay, upgrade window and maximum upgrades per episode, configure history retention and autoclearing, and run **System Diagnostics** to verify connectivity and latency across all active services.
   - **Blacklist:** Review and manage blacklisted releases or clear the blacklist.
   - **Scoring Rules:** Fine-tune weighted scoring points for codecs, sources, and audio profiles.
3. Click **Sync Watchlist** in the top navigation bar to import your watchlist.

### Upgrading

On first start after upgrading to this version, NZBoxer migrates the database to provider-neutral column names. A backup of your database is written automatically to `config/nzboxer.db.pre-m59.bak` (once; an existing backup is never overwritten) before any change is made. The migration is one-way: **older builds cannot open an upgraded database**. To roll back, stop the container, restore the `.bak` file over `config/nzboxer.db`, and start the older image.

### Alternative: Running Locally with Python

If you prefer to run NZBoxer directly without Docker:

```bash
git clone https://github.com/Reimaris/NZBoxer.git
cd NZBoxer
mkdir -p config
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## Architecture & Tech Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2.0 (Async), APScheduler
- **Database:** SQLite in WAL mode (stored persistently in `./config/nzboxer.db`)
- **Release Verification & Scoring:** GuessIt + custom 3-layer fake detection pipeline, archive unpack grace monitor, and weighted release scoring engine
- **Frontend:** Jinja2, HTMX, Alpine.js, TailwindCSS (mobile-optimized card and deck layouts)
- **Feedback Helper:** *Report a Bug* / *Request a Feature* / *Ask a Question* buttons on the Settings page open a launcher with privacy-safe diagnostics (version, Python, OS, deployment, active provider types — no keys, URLs or names). Copy the diagnostics table and open the matching GitHub issue form; NZBoxer never contacts GitHub itself. Security issues go through GitHub Private Vulnerability Reporting.

---

## License

This project is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)** — see the [LICENSE](LICENSE) file for details.


