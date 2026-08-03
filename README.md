# NZBoxer

NZBoxer is a lightweight, autonomous service that connects Simkl, TMDB, Newznab indexers, and TorBox to completely automate the search, scoring, and downloading of your favorite movies and TV shows.

## Features

- **Automated Sync**: Seamlessly syncs your "Plan to Watch" lists from Simkl for Movies and Shows.
- **Smart Orchestration**: Leverages TMDB for exact digital release dates to know exactly when to start searching.
- **Configurable Scoring Engine**: Uses a highly customizable `config.yaml` matrix to evaluate NZB releases based on resolution, codecs, release groups, and bitrate.
- **Upgrade Logic**: Continuously monitors for better releases and upgrades existing downloads if they exceed your configured threshold.
- **Modern UI**: A lightweight, fast HTMX & TailwindCSS dashboard for monitoring status and managing season tracking.
- **Zero Dependencies (Almost)**: Uses SQLite with WAL-mode for high concurrency, meaning no heavy database setups (like PostgreSQL) are required.

## Prerequisites

- Python 3.10+
- Accounts / API Keys for:
  - [Simkl](https://simkl.com/)
  - [TMDB](https://www.themoviedb.org/)
  - Newznab-compatible Indexer (e.g., Treasure Maps)
  - [TorBox](https://torbox.app/)

## Quick Start

### 1. Installation

Clone the repository and install the dependencies in a virtual environment:

```bash
git clone https://github.com/yourusername/NZBoxer.git
cd NZBoxer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configuration

Copy the example environment file and fill in your API keys:

```bash
cp .env.example .env
nano .env
```

Review the scoring logic and thresholds in `config.yaml`. The defaults are optimized for high-quality HEVC/x265 releases:

```bash
nano config.yaml
```

### 3. Running the Service

#### Option A: Production Deployment (Recommended, via GHCR)

The easiest and most robust way to run NZBoxer is using the pre-built Docker image from the GitHub Container Registry.

1. **Log into GHCR:** (You need a GitHub Personal Access Token with `read:packages` permission)
   ```bash
   echo $PAT | docker login ghcr.io -u Reimaris --password-stdin
   ```

2. **Set up environment:** (Ensure `.env` and `config.yaml` are created as described above)

3. **Start the application:**
   ```bash
   docker compose -f docker-compose.prod.yml up -d
   ```

#### Option B: Local Docker Build (Development)

If you want to modify the code and build locally:

```bash
docker compose up -d --build
```

#### Option C: Local Python Environment

Start the FastAPI application natively. The background scheduler will automatically start syncing and searching.

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 4. Access the Dashboard

Access the dashboard at: [http://localhost:8000/](http://localhost:8000/)

## Development & Testing

To run the test suite, MyPy type checker, and Ruff linter:

```bash
# Run tests
pytest -v

# Run type checking
mypy app/

# Run linter
ruff check app/ tests/
```

## Architecture

NZBoxer is built with:
- **FastAPI** for the web server and API
- **SQLAlchemy 2.0 (async)** for database operations
- **APScheduler** for background automation tasks
- **HTMX + Jinja2 + TailwindCSS** for the frontend
- **GuessIt** for parsing release names

See the `/docs/agent_memory/` directory for detailed architecture decisions and the project structure.
