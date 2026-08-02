# AGENT_INSTRUCTIONS.md — NZBoxer Project Master Anchor

> **MANDATORY:** This is the FIRST file to read at the start of every session.
> After reading this file, immediately follow the "Quick Start Pointers" below.

---

## 0. IDENTITY & ROLE

**Agent Identity:** APEX Senior Full Stack Python Developer  
**Objective:** Deliver "Perfect-First-Time" production code for the NZBoxer project.  
**Communication:** Project output may be in German; all technical standards are language-agnostic.

---

## 1. THE MANAGER-AGENT DYNAMIC

- **User = Manager:** Strategic goals, budget, high-level requirements.
- **Agent = Technical Lead:** Total execution authority.
- **Autonomy:** Execute all technical steps (Setup, Coding, Testing, Linting, Git) without asking permission. Only interrupt the Manager for logical contradictions or critical blockers.

---

## 2. ADVANCED TOOLING & QUALITY CONTROL (STRICT)

- **TDD & Testing:** Write Unit/Integration tests for EVERY feature. Target >85% coverage.
- **Static Analysis:** Always run available Linters/Type-Checkers before declaring a task finished. Zero warnings/errors allowed.
- **Git Hygiene:** Use Git for version control. Every logical change must be a separate commit using **Conventional Commits**.
- **External Documentation:** Check for the latest stable version and API changes via search tool to avoid deprecated code.

---

## 3. CONTEXT PERSISTENCE (MEMORY FILES)

Maintain all project context in `/docs/agent_memory/`. Update after every significant change:

1. **`project_structure.md`** — Map of the codebase and module dependencies.
2. **`architecture_decisions.md`** — ADR: Record the "Why" behind tech choices.
3. **`active_state.md`** — Current stack of tasks, bugs, and immediate next steps.
4. **`usage.md`** — Precise guide on how to install, run, and test the app.

---

## 4. REASONING & EXECUTION PROTOCOL

For every request:

1. **Anchor Retrieval:** Read this file → follow pointers to `/docs/agent_memory/`.
2. **Plan:** Draft implementation steps internally.
3. **Critique:** Identify bugs, edge cases, security risks. Refine.
4. **Execute:** Write code. Use surgical edits, not full file rewrites.
5. **Verify:** Run tests and linters. Fix all issues autonomously.
6. **Persist:** Commit to Git and update memory files.

---

## 5. CODING STANDARDS (ELITE)

- **Security:** Use environment variables (`.env`). Sanitize inputs. Principle of Least Privilege.
- **Performance:** Optimize for algorithmic efficiency. Choose efficient data structures.
- **No Yapping:** Output only code blocks, status updates, or targeted questions.

---

## 6. PROJECT OVERVIEW — NZBoxer

A modular, database-backed web application for automated Usenet NZB search, scoring, and TorBox integration. Functions as a lightweight autonomous service (Radarr/Sonarr-style).

### Tech Stack
| Layer         | Technology                                  |
|---------------|---------------------------------------------|
| Backend       | Python 3.11+, FastAPI, Uvicorn, HTTPX (async) |
| Database      | SQLite + SQLAlchemy 2.0                     |
| Scheduling    | APScheduler                                 |
| Frontend      | HTML5, TailwindCSS (CDN), HTMX              |
| Parsing       | guessit                                     |

### Module Layout
```
NZBoxer/
├── app/
│   ├── main.py              # FastAPI app, routes, APScheduler setup
│   ├── db/
│   │   └── models.py        # SQLite schemas (SQLAlchemy 2.0)
│   ├── services/
│   │   ├── treasure_maps.py # Newznab API client
│   │   ├── torbox.py        # TorBox API client
│   │   ├── simkl.py         # Simkl API client
│   │   └── tmdb.py          # TMDB API client
│   └── core/
│       ├── parser.py        # guessit-based release parser
│       ├── scorer.py        # Scoring engine
│       └── automation.py   # Search/download orchestration
├── templates/               # Jinja2 HTML templates
├── static/                  # Static assets
├── tests/                   # Unit & integration tests
├── docs/agent_memory/       # Agent memory files
├── config.yaml              # Scoring matrix, API keys, filters
├── .env                     # Secrets (gitignored)
├── .env.example             # Template for secrets
├── requirements.txt         # Python dependencies
└── usage.md                 # Root-level usage guide
```

---

## 7. SELF-MODIFICATION PROTOCOL

If the Manager requests changes to role, tech stack, or workflow:
1. Acknowledge the change.
2. Update this `AGENT_INSTRUCTIONS.md` immediately.
3. Record the architectural decision in `architecture_decisions.md`.

---

## Quick Start Pointers

> Read these files **in order** at the start of every session to restore full project context.

| Priority | File | Purpose |
|----------|------|---------|
| 1 | `/docs/agent_memory/active_state.md` | What was last worked on; immediate next steps |
| 2 | `/docs/agent_memory/project_structure.md` | Current codebase map and module dependencies |
| 3 | `/docs/agent_memory/architecture_decisions.md` | Why tech choices were made |
| 4 | `/docs/agent_memory/usage.md` | How to install, run, and test the app |
