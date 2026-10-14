# Contributing to NZBoxer

Thanks for your interest! Bug reports, feature requests and pull requests are welcome.

## Before you start

- **Bugs, ideas, questions:** open an issue using the templates, or use the
  feedback buttons in the app's Settings page, which attach privacy-safe diagnostics.
- **Larger changes:** open an issue first so the approach can be agreed before you invest time.
- **Security problems:** see [SECURITY.md](SECURITY.md). Do not file them publicly.

## Development setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload
```

## Before opening a pull request

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy app/
.venv/bin/pytest -q tests/
```

- Add or update tests for behavior changes.
- Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, ...).
- Keep code, UI labels and commit messages in English.
- By contributing you agree your work is licensed under the project's AGPL-3.0 license.
