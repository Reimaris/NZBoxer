<p align="center">
  <img src="app/static/icon.svg" width="128" height="128" alt="NZBoxer Logo" />
</p>

<h1 align="center">NZBoxer</h1>

<p align="center">
  <strong>Self-hosted watchlist and on-demand Usenet push companion for Simkl, Newznab indexers, and TorBox.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-v3.0.0-pink" alt="Version" />
  <img src="https://img.shields.io/badge/docker-ready-blue" alt="Docker Ready" />
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License" />
</p>

---

## What is NZBoxer?

**NZBoxer** bridges your **Simkl** watchlist, **TMDB** & **AniList** metadata, **Newznab** Usenet indexers, and **TorBox** into a single lightweight web application.

Unlike traditional automation stacks that poll indexers 24/7 and download everything in the background before you are ready to watch, NZBoxer is built around an **on-demand workflow**:
- It keeps your **Movies**, **TV Series**, and **Anime** synchronized with Simkl and tracks exact digital release dates, episode air dates, and chronological anime watch orders.
- It performs **zero background indexer polling** while idle—preserving your indexer API limits and TorBox storage until you actually want to push a movie, season, or episode.
- When you decide to push a title, NZBoxer searches your configured indexers, filters out fakes, scores releases against your quality and language rules, sends the NZB to TorBox, monitors the transfer to completion, and automatically replaces dead downloads if a release fails.

---

## How It Works

### 1. Watchlist Sync & Metadata Enrichment
- **Simkl Integration:** Connects to your Simkl account (via OAuth2 PIN linking) and syncs your *Plan to Watch* and *Watching* lists across Movies, TV Shows, and Anime.
- **Release Date Tracking (TMDB):** Enriches Movies and Series with digital/physical release dates and season/episode air dates so you always know whether a title is actually available or still upcoming.
- **Chronological Anime Franchises (AniList):** Automatically groups standalone anime sequel entries and canon franchise movies under a single parent entry in chronological watch order, while keeping your Simkl watch progress aligned.
- **Automatic Cleanup:** When you mark an item as *Completed* or drop it on Simkl, NZBoxer automatically removes it from your active watchlist views on the next sync while preserving your permanent push history.

### 2. On-Demand Search, Scoring & Push
- **One-Click Auto-Push:** Push an entire movie, one or more seasons, or specific episodes with a single click. For series and anime, you can choose whether to allow season packs, prefer season packs over individual episodes (with automatic episode fallback if no pack is available), and optionally auto-advance to the next season when the current one finishes.
- **Manual Release Picker:** Prefer to choose the exact release yourself? Run an interactive search directly from the item modal or the standalone Manual Search view to inspect scored candidates, language tags, file sizes, and rejection reasons before grabbing.
- **Customizable Scoring & Presets:** Configure global scoring weights for resolution (`2160p`, `1080p`, `720p`), sources (`BluRay`, `WEB-DL`, `Remux`), HDR (`Dolby Vision`, `HDR10+`, `HDR10`), video/audio codecs (`HEVC`, `AV1`, `Atmos`, `TrueHD`, `DTS-HD`), primary/fallback languages, and preferred/unwanted keywords—or save reusable **Search Presets** for different types of media.

### 3. Live Transfer Monitoring, Auto-Recovery & Notifications
- **Auto-Waking Transfer Poller:** As soon as a push is dispatched to TorBox, NZBoxer wakes its transfer monitor to track live download progress, speed, and ETA. Once all active transfers finish, the poller goes back to sleep automatically.
- **Automatic Dead-Release Replacement:** If a pushed NZB fails on TorBox (e.g., missing articles, repair failure, or password protection), NZBoxer blacklists the broken release, deletes the failed transfer from TorBox, and automatically pushes the next highest-scoring candidate.
- **Push History & TorBox Management:** Every push is recorded in a permanent history ledger showing its current TorBox status (*Ready on TorBox*, *Replaced*, *Deleted*, *Failed*, or *Canceled*), with built-in controls to delete files directly from TorBox or re-push a title at any time.
- **Consolidated Notifications:** Sends rich status notifications via **Discord Webhooks** and/or **Telegram**, automatically grouping multi-episode pushes and completions for the same show into a single clean summary message.

---

## Prerequisites

- **Docker & Docker Compose** (recommended) or **Python 3.12+**
- Accounts / API Keys for:
  - [Simkl](https://simkl.com/) (Client ID + interactive OAuth2 PIN login in the UI)
  - [TMDB](https://www.themoviedb.org/) (API Key for movie & TV metadata)
  - At least one **Newznab-compatible Usenet Indexer**
  - [TorBox](https://torbox.app/) (API Key for Usenet cloud downloads)
- *(Optional)* **Discord Webhook URL** or **Telegram Bot Token & Chat ID** for push and completion notifications (AniList anime metadata requires no API key).

---

## Getting Started

### 1. Installation with Docker (Recommended)

The easiest way to run NZBoxer is with the pre-built Docker image from the GitHub Container Registry (`ghcr.io/reimaris/nzboxer:latest`).

> [!IMPORTANT]
> Because the container runs as an unprivileged non-root user (`UID 1000`), create the `config` directory on your host **before** starting the container so it is owned by your user rather than `root`.

```bash
git clone https://github.com/Reimaris/NZBoxer.git
cd NZBoxer
mkdir config
docker compose up -d
```

### 2. Initial Configuration

All configuration—including Providers (Simkl, TMDB, Newznab Indexers, TorBox), Search Presets, Video Scoring rules, and Discord/Telegram Notifications—is managed directly in the Web UI. No `.env` file is required.

1. Open the dashboard at [http://localhost:8000/](http://localhost:8000/).
2. Navigate to **Settings** (top right).
3. In the **Providers** tab, configure your **TMDB**, **TorBox**, and **Newznab Indexer** credentials, and connect your **Simkl** account using the interactive PIN authentication flow.
4. Click **Sync Watchlist** in the top navigation bar to import your watchlist.

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
- **Release Parsing:** GuessIt + custom 3-layer fake release detection & weighted scoring engine
- **Frontend:** Jinja2, HTMX, Alpine.js, TailwindCSS
