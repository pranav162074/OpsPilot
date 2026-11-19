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

# In two more terminals, the ingestion worker and its scheduler
uv run celery -A opspilot.worker worker --loglevel=info --pool=solo
uv run celery -A opspilot.worker beat --loglevel=info
```

(`--pool=solo` is needed on Windows; on Linux and macOS it can be left out.)

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

## Documents

Runbooks, postmortems and architecture notes are uploaded as `.md` or `.txt` files and split into chunks for retrieval.

```bash
curl -X POST localhost:8000/v1/documents -H "Authorization: Bearer ops_..." \
  -F file=@payments-runbook.md -F source_type=runbook
```

| Endpoint | Role needed |
|---|---|
| `POST /v1/documents` (upload), `POST /v1/documents/{id}/reingest`, `DELETE /v1/documents/{id}` | responder |
| `GET /v1/documents`, `GET /v1/documents/{id}`, `GET /v1/documents/{id}/chunks` | viewer |

How ingestion works:

1. The upload is checked (type, size, UTF-8), stored with status `pending`, and queued on Celery. The request returns `202` straight away.
2. A worker claims the document (`pending` → `processing`) with a single `UPDATE`, so only one worker handles it even if the task is delivered twice.
3. Markdown is split along its headings, then by paragraph, into chunks of at most `CHUNK_MAX_TOKENS` with `CHUNK_OVERLAP_TOKENS` of overlap. Code blocks are kept whole. Each chunk is an exact slice of the original text and records its position, so answers can cite the passage.
4. Old chunks are replaced and the document is marked `ready` in one transaction. Each claim increments `attempts`, and a worker saves its result only if `attempts` hasn't moved on, so a stalled worker can't overwrite a newer one.

What happens when things go wrong:

- **Database or network blip:** the document is handed back and the task retries with backoff (5s, 10s, 20s...).
- **Worker crashes mid-task:** tasks are acknowledged only when they finish, so Redis hands the task to another worker; a document stuck in `processing` for `INGESTION_STALE_SECONDS` can be taken over.
- **Task lost (queue down during upload, Redis restarted):** every minute, the beat scheduler re-sends documents still `pending` after `INGESTION_REQUEUE_AFTER_SECONDS`.
- **Repeated failures:** after `INGESTION_MAX_ATTEMPTS` the document is marked `failed` with the reason in `error`.

Other details:

- The same content uploaded twice to a workspace returns `409` with the existing document's id. Line endings and byte-order marks don't count as differences.
- The list is newest first with cursor pagination (`?limit=20&cursor=...`), and filters by `status` and `source_type`.
- Worker logs carry the request ID of the upload that queued the task.

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
