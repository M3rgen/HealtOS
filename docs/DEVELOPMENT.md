# Development

Run commands from the repository root unless indicated otherwise. Python dependencies are locked with `uv`; frontend dependencies use `npm ci`.

## Native setup

Install Python 3.12+, uv 0.12.1, PostgreSQL, and Node.js 24. Copy `.env.example` to `.env` and configure a private development database:

```dotenv
DATABASE_URL=postgresql+asyncpg://YOUR_USER:YOUR_URL_ENCODED_PASSWORD@127.0.0.1:5432/YOUR_DATABASE
```

Replace placeholders locally. Keep the development database separate from your real archive.

```sh
uv sync --frozen --extra dev
uv run --frozen --extra dev alembic upgrade head
```

Build the frontend:

```sh
cd frontend
npm ci
npm run build
cd ..
```

Start the application:

```sh
uv run --frozen --extra dev uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --no-access-log
```

For frontend hot reload, run `npm run dev` from `frontend` while the backend is running. Vite proxies API requests to the local backend. Google credentials are optional for development; most tests create synthetic records in an in-memory database.

## Validation

`make verify` runs the static gates below (plus `pyright app`) in order, and `make test-browser`
runs the browser checks (`cd frontend && npm run test:browser`). The individual commands:

```sh
uv lock --check
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev ruff format --check .
uv run --frozen --extra dev pytest -q
uv run --frozen --extra dev python scripts/verify_configuration.py
```

CI also applies PostgreSQL migrations, checks schema drift, validates Compose, and builds the application image. `scripts/verify_compose.py` performs a broader smoke test in its own temporary Compose project; it creates and removes only its isolated test resources.

Browser checks in `tests/browser` need Playwright with a browser installed and the appropriate local frontend URL. Some require a running backend; others intercept requests with synthetic fixtures. Never use a real archive for public screenshots. Validate desktop and mobile dimensions after interface changes: the main document must not scroll, and long content must remain reachable inside its panel.

## Repository map

| Path | Responsibility |
| --- | --- |
| `app/api` | Local HTTP routes and Codex chat. |
| `app/google_health` | OAuth, pagination, data types, and webhook verification. |
| `app/sync` | Backfill, incremental sync, checkpoints, and reconciliation. |
| `app/services` | Storage, dashboard read models, scheduling, and optional AI tools. |
| `app/models` / `migrations` | Database schema and versioned migrations. |
| `frontend` | Dashboard interactions and the Three.js body reference. |
| `tests` | Unit, integration, and browser checks. |
| `scripts` | Operations, configuration verification, and bridge launcher. |

The installed CLI is still named `health-mirror`, and the distribution is `personal-health-mirror`. These internal identifiers do not affect the HealtOS repository name.
