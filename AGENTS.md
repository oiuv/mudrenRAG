# Repository Guidelines

## Project Structure & Module Organization

- `app/`: FastAPI routes, Dify request/response models, metadata filters, configuration, and server entry point. Retrieval combines FAISS vectors and BM25 through RRF, then fetches forum content and reranks candidates.
- `scripts/`: incremental MySQL synchronization, Windows startup orchestration, and interactive Dify retrieval testing.
- `tests/`: pytest suites and shared helpers.
- `docs/operations.md`: synchronization schedules and troubleshooting; `README.md` covers setup and architecture.
- `data/`: ignored runtime snapshots and diagnostic output. There are no frontend assets.

## Build, Test, and Development Commands

Run commands from the repository root using the project virtual environment:

- `python -m venv .venv`: create the environment; activate it before subsequent commands.
- `python -m pip install -r requirements-dev.txt`: install runtime dependencies and pytest.
- `python scripts/sync_data.py`: incrementally synchronize visible forum posts; requires configured MySQL and embedding access.
- `python -m app.server`: start the API using `.env` values `HOST` and `PORT`, defaulting to `0.0.0.0:8008`.
- `.\start_server.bat --no-pause`: prepare dependencies, synchronize, and start on Windows.
- `python scripts/query_knowledge.py --top-k 3 --score 0`: interactively test a running API with real upstream calls.
- `python -m pytest -q`: run automated tests.
- `docker build -t mudren-rag .`: build the service image.

## Coding Style & Naming Conventions

Use Python 3.10+ syntax, four-space indentation, snake_case functions/modules, PascalCase classes, and UPPER_CASE constants. Add type hints where they clarify interfaces. Follow existing module boundaries and formatting; no dedicated formatter or linter is configured. Run `git diff --check` before submitting.

Keep configuration comments and operational instructions in Chinese for the mud.ren community. Preserve UTF-8 files and CRLF line endings for Windows batch scripts.

## Testing Guidelines

Use `tests/test_*.py` files and `test_*` functions. Mock MySQL and external HTTP/model calls; use temporary directories for snapshots. Automated tests exercise real local FAISS/BM25 behavior without paid model requests. Add focused regression coverage for changed retrieval, synchronization, configuration, or failure behavior. No numeric coverage threshold is configured.

## Commit & Pull Request Guidelines

Follow recent history: concise, imperative subjects such as `feat: add configurable listener` or `fix: preserve snapshot on sync failure`. PRs should describe the problem, resulting behavior, validation commands/results, and configuration or migration impact. Link relevant issues and update affected documentation.

## Security & Configuration

Never commit `.env`, credentials, or generated `data/` artifacts. Preserve existing user configuration and redact secrets from diagnostics. Keep TLS verification enabled. This service calls embedding and reranking models; Dify generates answers.
