# Usage Guide

This file provides comprehensive instructions on how to use NZBoxer.

## Installation

1. **Clone the repository:**
   ```bash
   git clone <repository_url>
   cd NZBoxer
   ```

2. **Set up Virtual Environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

## Configuration

1. **Environment Variables:**
   Copy `.env.example` to `.env` and provide your credentials.
   ```bash
   cp .env.example .env
   ```
   Required keys include:
   - `SIMKL_CLIENT_ID`
   - `TMDB_API_KEY`
   - `TREASURE_MAPS_URL`
   - `TREASURE_MAPS_API_KEY`
   - `TORBOX_API_KEY`

2. **Scoring Rules:**
   Edit `config.yaml` to adjust the scoring engine (resolution points, codecs, release groups, and target/upgrade thresholds).

## Running the Application

The application is run via `uvicorn`. The APScheduler is integrated into the FastAPI lifespan and will automatically trigger the automation cycles.

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- **Dashboard:** Navigate to `http://localhost:8000/` to see the current status of all movies and shows.
- **Background Tasks:** Simkl syncing happens every hour, and automation (searching/upgrading) happens every 15 minutes.

## Testing & Quality Assurance

Ensure you have run `pip install -r requirements.txt` which includes test dependencies (`pytest`, `mypy`, `ruff`).

- **Run all tests:** `pytest -v`
- **Run linter and formatter:** `ruff check app/ tests/` (add `--fix` to auto-fix issues)
- **Run static type checking:** `mypy app/`
