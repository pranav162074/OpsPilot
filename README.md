# OpsPilot

An AI on-call assistant: retrieval over runbooks, incident write-ups and code, plus an agent that investigates production incidents using live metrics and logs.

Work in progress.

## Running locally

Requirements: Docker Desktop, Python 3.13, and [uv](https://docs.astral.sh/uv/).

```bash
# Postgres (with pgvector) and Redis
docker compose up -d

cd backend
cp .env.example .env          # Windows: copy .env.example .env
uv sync
uv run alembic upgrade head
uv run uvicorn opspilot.main:app --reload
```

- `GET /health/live`: the process is running.
- `GET /health/ready`: Postgres (with pgvector) and Redis both answer. Returns `503` with per-dependency status otherwise.
- `GET /docs`: interactive API documentation.

## Workspaces and API keys

Every resource belongs to a workspace, and every request authenticates with an API key for one workspace: `Authorization: Bearer ops_...`.

```bash
# Create a workspace. Needs ADMIN_TOKEN from .env; returns the first admin key, shown once.
curl -X POST localhost:8000/v1/workspaces -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d '{"name": "Acme"}'

curl localhost:8000/v1/me -H "Authorization: Bearer ops_..."
```

| Endpoint | Role needed |
|---|---|
| `POST /v1/workspaces` | admin token |
| `GET /v1/me` | viewer |
| `POST /v1/api-keys`, `GET /v1/api-keys`, `DELETE /v1/api-keys/{id}` | admin |

- Roles: `viewer` < `responder` < `admin`. A key can do everything the roles below it can.
- Keys are stored only as SHA-256 hashes. The full key is returned once, when it's created.
- Unknown, revoked and expired keys all get the same `401`. Another workspace's resources return `404`, the same as ones that don't exist.
- The last active admin key of a workspace can't be revoked (`409`).
- Each key is limited to `API_RATE_LIMIT_PER_MINUTE` requests (default 120), and each IP to `WORKSPACE_CREATE_LIMIT_PER_HOUR` workspace creations (default 10). Responses carry `X-RateLimit-*` headers; a `429` also has `Retry-After`. If Redis is down, requests are allowed rather than blocked.
- Errors always look like `{"error": {"code": "...", "message": "...", "details": [...]}}`.

## Checks

From `backend/`, the same checks CI runs on every pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run alembic check
uv run pytest
```

Tests use their own database (`opspilot_test`, created automatically) and Redis database 15, so they never touch your development data.
