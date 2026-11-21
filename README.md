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

## Search

`POST /v1/search` (viewer) finds the passages in a workspace's documents that best answer a question:

```bash
curl -X POST localhost:8000/v1/search -H "Authorization: Bearer ops_..." \
  -H "Content-Type: application/json" \
  -d '{"query": "database ran out of connections", "limit": 5, "mode": "hybrid"}'
```

Each result has the chunk's text, its heading path, the document it came from, its exact position in that document (`char_start`, `char_end`) for citations, and the rank each retriever gave it.

**How it works**

- **Embeddings:** at ingestion, every chunk (heading path plus text) is embedded and stored in a pgvector column with an HNSW index. Each chunk records the model that embedded it, and search only compares vectors from the current model.
- **Vector search** finds passages with similar meaning: "pods killed for using too much memory" finds the section on `OOMKilled` without sharing its words. Queries and documents are embedded with different task types, and query embeddings are cached in Redis.
- **Keyword search** uses Postgres full-text search: a generated `tsvector` column with a GIN index, English stemming, and `websearch_to_tsquery`, so quotes, `OR` and `-word` work. It catches exact identifiers that embeddings blur, like `ERR_POOL_TIMEOUT` or `db-primary.internal`.
- **Hybrid** (the default) runs both and merges them with **Reciprocal Rank Fusion**: each result scores `1/(60 + rank)` per list it appears in. RRF uses ranks only, so the two retrievers' scores never need calibrating against each other.
- **Multi-tenant vector search:** an HNSW index searches the whole table and filters by workspace afterwards, which can leave a small workspace with too few results. pgvector 0.8's iterative scan keeps searching until enough rows pass the filter.
- **Degrades instead of failing:** if the embedding API is down, hybrid search answers from keywords alone with `"degraded": true`. Vector-only search returns `503`.

**Embedding providers** (`EMBEDDING_PROVIDER`):

- `hashing` (default): local, no API key. It captures word overlap, not meaning, and is meant for tests, CI and offline work.
- `gemini`: Google's `gemini-embedding-001` at 768 dimensions, normalized. Needs `GEMINI_API_KEY`.

After switching provider or model, re-index so every chunk is embedded by the new model:

```bash
uv run python -m opspilot.cli reindex        # only documents embedded by another model
uv run python -m opspilot.cli reindex --all  # everything
```

**Retrieval evaluation**

`backend/evals` holds six runbooks and a postmortem, plus 22 questions, each with the document that answers it. Half quote an exact identifier; half are paraphrases. The evaluation scores each mode by hit@1, hit@3 and MRR:

```bash
uv run python -m opspilot.evals.retrieval
```

With the offline `hashing` embedder:

| mode | hit@1 | MRR | paraphrased questions, hit@1 |
|---|---|---|---|
| keyword | 0.50 | 0.50 | 0.08 |
| vector | 0.73 | 0.83 | 0.58 |
| hybrid | 0.77 | 0.86 | 0.58 |

With `gemini-embedding-001` (768 dimensions):

| mode | hit@1 | MRR | paraphrased questions, hit@1 |
|---|---|---|---|
| keyword | 0.50 | 0.50 | 0.08 |
| vector | 0.95 | 0.98 | 1.00 |
| hybrid | 1.00 | 1.00 | 1.00 |

Keyword search nails exact identifiers and misses almost every paraphrase. Hybrid keeps keyword search's perfect score on identifiers and adds vector search's recall on paraphrases. A test runs this evaluation on every CI build, so a change that makes retrieval worse fails the build.

## Investigations (the agent)

`POST /v1/investigations` (responder) asks the agent a question about an incident. It searches the workspace's documents, answers with citations, and has the answer checked against its sources before returning it.

```bash
curl -X POST localhost:8000/v1/investigations -H "Authorization: Bearer ops_..." \
  -H "Content-Type: application/json" \
  -d '{"question": "Payments are failing with ERR_POOL_TIMEOUT. What should I do?"}'
```

The response holds the answer, its citations (document, heading, chunk), token usage, latency, and the full trace of every model call and tool call. `GET /v1/investigations` and `GET /v1/investigations/{id}` (viewer) read them back.

**How it works:** a LangGraph state machine.

```
agent --tool calls--> tools --> agent          ReAct: reason, act, observe, repeat
  | step limit --> force_answer --> review
  +-- answer --> review --supported--> done
                   +--unsupported--> agent     one revision, with the reviewer's notes
```

- **Tools:** `search_knowledge_base` (the hybrid search) and `read_section` (a passage with its neighbours). They're built per request and bound to the caller's workspace, so the model can't reach another workspace's documents whatever it asks for.
- **Citations are checked, not trusted:** the answer must cite passages as `[c:<chunk id>]`. Any citation to a passage the tools never returned (a made-up id) is removed and counted in `invalid_citations`.
- **Self-review:** a second model call compares the draft with the passages it cites. If it finds unsupported statements, the agent gets one chance to revise.
- **Bounded:** at most `AGENT_MAX_STEPS` tool rounds (then it must answer with what it has), an overall timeout, and a per-workspace hourly limit, since model calls cost money.
- **Prompt injection:** documents are written by people, so the system prompt tells the model to treat tool results as reference material and ignore instructions inside them.
- **Observable:** every run stores its model calls, tool calls, tokens and latency per step, and logs a summary line.

**Tests** run the real graph with a scripted chat model that plays back prepared replies (tool calls, drafts, reviewer verdicts). They cover the ReAct loop, revision, the step limit, unknown tools, made-up citations, workspace isolation, timeouts and failures, with no API calls. Tests always use the offline embedder and no real model, whatever `.env` says.

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
