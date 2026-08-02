# NZBoxer — Usage Guide

**Last Updated:** 2026-08-02

---

## Prerequisites

- Python 3.11+
- `pip` or `uv`
- Git

---

## 1. Installation

```bash
# Clone the repository
git clone <repo-url>
cd NZBoxer

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

---

## 2. Configuration

### 2a. Environment Variables (`.env`)

Copy the template and fill in your API keys:

```bash
cp .env.example .env
```

Edit `.env`:

```env
SIMKL_CLIENT_ID=your_simkl_client_id
SIMKL_ACCESS_TOKEN=your_simkl_access_token
TMDB_API_KEY=your_tmdb_api_v3_key
TREASURE_MAPS_URL=https://your.indexer.url
TREASURE_MAPS_API_KEY=your_newznab_api_key
TORBOX_API_KEY=your_torbox_api_key
DATABASE_URL=sqlite+aiosqlite:///./nzboxer.db
```

### 2b. Scoring & Automation (`config.yaml`)

Review and adjust `config.yaml` for:
- Scoring weights (resolution, codec, audio, source)
- Release group whitelist/blacklist
- Cutoff score (stop searching after reaching this score)
- Upgrade threshold (minimum score delta to trigger upgrade download)
- Automation interval (minutes)

---

## 3. Running the Application

```bash
# Development server with auto-reload
uvicorn app.main:app --reload --port 8000
```

Open your browser at: **http://localhost:8000**

---

## 4. Running Tests

```bash
# Run all tests with coverage report
pytest --cov=app --cov-report=term-missing

# Run only unit tests
pytest tests/test_parser.py tests/test_scorer.py -v

# Run only integration tests
pytest tests/test_automation.py tests/test_api_routes.py -v
```

---

## 5. Linting & Type Checking

```bash
# Lint and format with ruff
ruff check app/ tests/
ruff format app/ tests/

# Static type checking
mypy app/
```

---

## 6. Database

The SQLite database file is created automatically at startup at the path defined in `DATABASE_URL` (default: `./nzboxer.db`).

To reset the database:
```bash
rm nzboxer.db
# Restart the app — tables are recreated automatically
```

---

## 7. Git Workflow

Commits follow **Conventional Commits**:
```
feat: add season monitoring toggle UI
fix: handle missing guessit audio field
chore: update dependencies
docs: update usage guide
```

---

## 8. Key Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard — watchlist with status badges |
| GET | `/items/{id}` | Item detail — season toggles |
| POST | `/items/{id}/seasons/{s}/toggle` | Toggle season monitoring |
| POST | `/sync` | Trigger manual Simkl sync |
| GET | `/search?item_id={id}` | Manual NZB search (HTMX partial) |
| POST | `/download` | Send NZB to TorBox |
| GET | `/api/status` | Scheduler + app health |
