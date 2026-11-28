# OpsPilot

An AI on-call assistant: retrieval over runbooks, incident write-ups and code, plus an agent that investigates production incidents using live metrics and logs.

Work in progress: see [ROADMAP.md](ROADMAP.md) for every planned branch and where it stands.

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

### Code, chunked by function

Source code (`.py`, `.ts`/`.tsx`, `.js`, `.go`, `.java`, `.kt`, `.cs`, `.rs`, `.php`, `.rb`, `.sh`) can be uploaded or synced. It's split where a programmer would split it, one chunk per function, method or class, each headed with its name (`GatewayClient.charge`), which is also what keyword search matches ([`ingestion/code.py`](backend/src/opspilot/ingestion/code.py)):

- **Python** is parsed with `ast`: exact boundaries, decorators and the comments above a definition included. A class too big for one chunk becomes its header (docstring, attributes) plus a chunk per method.
- **Brace languages** are split at declarations found by per-language patterns, each extended to its matching closing brace, skipping braces inside strings and comments. Go methods are named `Receiver.Method`.
- **Ruby** blocks end at the `end` matching their indentation.
- Imports and module-level code stay searchable as `(module)` chunks; code that doesn't parse falls back to windows of whole lines. Every chunk is still an exact slice of the file.

### Sources: syncing a GitHub repository

Instead of uploading runbooks one by one, connect the repository they live in (admin):

```bash
curl -X POST localhost:8000/v1/sources -H "Authorization: Bearer ops_..." -H "Content-Type: application/json" \
  -d '{"repository": "acme/platform", "paths": ["docs/runbooks", "src/payments"], "include_code": true, "token": "github_pat_..."}'
```

GitHub is asked about the repository first, so a typo or a missing token fails now (`422`), not in a background job. Leave out `ref` to follow the default branch. The token is optional for public repositories; it's stored encrypted (`INTEGRATIONS_KEY`), like telemetry tokens.

**Syncs are incremental** ([`sources/sync.py`](backend/src/opspilot/sources/sync.py)). One request reads the branch's latest commit, and the sync stops there if the documents already match it. Otherwise one request lists the whole tree, and each file's blob sha is compared with the one stored on its document: only new and changed files are downloaded (8 at a time) and re-indexed, and documents of deleted files are removed. A thousand-file repository where one file changed costs four requests and one re-index.

**Which files:** documentation (`.md`, `.markdown`, `.txt`, `.rst`) under the configured paths, plus code with `include_code`. Dependency and build directories (`node_modules`, `vendor`, `dist`, ...) and dot-directories are skipped, as are files over `SOURCE_MAX_FILE_BYTES`, binary files and anything that isn't UTF-8. Invisible Unicode is stripped, as for uploads. Two files with the same content are two documents (the duplicate check applies to uploads only).

**When:** GitHub calls the source's webhook on every push. The URL and secret come back once, when the source is created (repository → Settings → Webhooks, content type `application/json`, the push event). The webhook is verified by its `X-Hub-Signature-256` HMAC, so it needs no API key, and pushes to other branches are ignored. A beat job also syncs any source not synced for `SOURCE_SYNC_INTERVAL_MINUTES`, the catch-up for webhooks that never arrived. `POST /v1/sources/{id}/sync` (responder) syncs now, and `?force=true` compares every file even at an unchanged commit.

**When GitHub fails:** a 5xx, a timeout or a used-up rate limit is retried with backoff (30 s doubling, or as long as GitHub's `retry-after` says). A revoked token or a deleted repository won't fix itself: the source is marked `failed` with the reason, and the job goes straight to the dead-letter queue. A sync that died mid-way is taken over after 15 minutes. `GET /v1/sources/{id}` shows the status, last commit, last error, document counts and what the last sync did (`added`, `updated`, `deleted`, `unchanged`, `skipped`).

### Original files in object storage

The text OpsPilot indexes lives in Postgres. With `STORAGE_BACKEND=s3` or `local`, the files it came from are kept too, byte for byte, so they can be downloaded (`GET /v1/documents/{id}/original`) and processed again by a better parser or a new chunker, without asking anyone to upload them again ([`storage/`](backend/src/opspilot/storage/__init__.py)).

- Keys are `workspaces/<workspace>/documents/<document>/<sha256>`: a changed file never overwrites the previous version's object.
- The original is written **before** the database row, so a failed insert leaves a harmless orphan, never a document pointing at nothing. Uploads ask S3 to verify a SHA-256 checksum.
- S3 means anything that speaks its API: AWS, Cloudflare R2, or locally RustFS (`docker compose --profile storage up -d objects`). MinIO, the usual choice, stopped publishing community images in October 2025. Tests use `moto`, so nothing reaches a network.
- Downloads are always served as attachments, with `nosniff` and a sandboxing CSP, so an uploaded HTML file can't run in OpsPilot's origin.

### The dead-letter queue

Retries handle what time fixes (a rate limit, a restart). What's left after them, or fails in a way retrying can't fix, lands in the **dead-letter queue** with the job's arguments and error ([`dead_letters.py`](backend/src/opspilot/dead_letters.py)), instead of in a log line nobody reads:

- ingestion that ran out of attempts, or that the embedding service kept rejecting;
- source syncs that failed for good, or ran out of retries (a Celery base task records any final failure);
- original files whose deletion failed after their document was deleted.

There's one open dead letter per job: the same job failing again updates it and counts the failure, so a broken source synced every 30 minutes is one row, not fifty.

| Endpoint (admin) | |
|---|---|
| `GET /v1/dead-letters?status=dead` | what's waiting (also `retried`, `discarded`) |
| `POST /v1/dead-letters/{id}/retry` | sends it again: a document gets a fresh set of attempts, a sync is queued, a leftover file is deleted on the spot |
| `POST /v1/dead-letters/{id}/discard` | not worth retrying |

Both are conditional updates, so two admins clicking at once send a job once. A retry that fails again comes back to the queue.

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
| **hybrid + re-ranker** | **1.00** | **1.00** | **1.00** |

With `gemini-embedding-001` (768 dimensions):

| mode | hit@1 | MRR | paraphrased questions, hit@1 |
|---|---|---|---|
| keyword | 0.50 | 0.50 | 0.08 |
| vector | 0.95 | 0.98 | 1.00 |
| hybrid | 1.00 | 1.00 | 1.00 |

Keyword search nails exact identifiers and misses almost every paraphrase. Hybrid keeps keyword search's perfect score on identifiers and adds vector search's recall on paraphrases. The re-ranker then fixes the order: with even the crude offline embedder, the right document is first for every question.

**Re-ranking.** Hybrid search scores the query and each passage separately, so it can't tell a passage that *answers* "the database ran out of connections" from one that merely shares its words. A cross-encoder reads the query and the passage together, which is far more precise, and far too slow for a whole corpus. So search finds candidates cheaply, and the cross-encoder re-orders the top 20 (`RERANK_CANDIDATES`). The model is `ms-marco-MiniLM-L-6-v2` (80 MB, Apache-2.0), run locally on the CPU through ONNX: free, no API, a few milliseconds per query, loaded once per process and shared.

```bash
uv sync --extra rerank      # adds fastembed (ONNX Runtime); the model downloads on first use
RERANKER=cross-encoder      # in .env
```

Search, the agent's `search_knowledge_base` tool and the evaluation all rank through the same function. Results carry `rerank_score`; `"rerank": false` in a search request skips it; a re-ranker that fails leaves results in fusion order with `"degraded": true`.

**The quality gate.** `--check` compares the scores with [`evals/thresholds.json`](backend/evals/thresholds.json) (per embedding provider) and exits 1 if any is lower:

```bash
uv run python -m opspilot.evals.retrieval --check
```

CI runs it in its own job, with the offline embedder (deterministic, no API key) and the re-ranker (its model cached between runs). A change that makes search worse fails the build, the same as a failing test.

**Answer evaluation.** Retrieval being right doesn't make answers right, so a second evaluation runs the whole agent on 8 incident questions ([`evals/answer_cases.json`](backend/evals/answer_cases.json)) and scores each answer:

| metric | meaning |
|---|---|
| grounded | cites the document that answers the question |
| citation precision | share of its citations from that document |
| coverage | share of the key facts (a command, a setting) it includes |
| faithfulness | share of its claims that its cited sources support, judged by a second model call that sees only the answer and those sources |

plus invalid citations, tokens, cost and latency. Unsupported claims are printed one by one, which is what makes it useful when changing a prompt.

```bash
uv run python -m opspilot.evals.answers                     # react strategy
uv run python -m opspilot.evals.answers --strategy plan --rerank --check --json results.json
```

It needs `GEMINI_API_KEY` (the free tier covers it) and isn't in CI: model answers vary from run to run and cost quota. Its parts (the judge, the scoring) are tested with a scripted model.

## Investigations (the agent)

`POST /v1/investigations` (responder) asks the agent a question about an incident. It searches the workspace's documents, answers with citations, and has the answer checked against its sources before returning it.

```bash
curl -X POST localhost:8000/v1/investigations -H "Authorization: Bearer ops_..." \
  -H "Content-Type: application/json" \
  -d '{"question": "Payments are failing with ERR_POOL_TIMEOUT. What should I do?"}'
```

Add `"background": true` to get `202` at once and have the worker run it; poll `GET /v1/investigations/{id}` until `status` isn't `running`.

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

### Two strategies: `react` and `plan`

`react` (above) decides one tool call at a time: right for a quick question. Incidents are investigated with `plan`, which works the way an engineer would ([`agent/planner.py`](backend/src/opspilot/agent/planner.py)):

```
plan --> gather --> hypothesize --> verify --> answer --> review --> done
(what to   (every planned   (up to 3 causes,   (one check per      (cites the         +--unsupported--> answer
 look at)   tool at once)    with evidence)     cause, at once)     evidence)
```

1. **Plan:** the model lists everything worth knowing up front (the runbook for this symptom, the metrics and logs that confirm it, recent deploys). Unknown tools are dropped; an unusable plan still gets a runbook search.
2. **Gather:** all planned tool calls run concurrently, each with its own time limit (`AGENT_TOOL_TIMEOUT_SECONDS`). A tool that fails or times out is reported as evidence ("Loki didn't answer"), not raised.
3. **Hypothesize:** possible causes, most likely first, each citing its evidence and naming one tool call that would confirm or rule it out.
4. **Verify:** those checks run concurrently, then the model rules on each cause: `supported`, `refuted` or `inconclusive`. They're stored on the investigation (`hypotheses`), supported first.
5. **Answer, then review:** the answer names the likely cause, what was ruled out and the next steps, citing evidence; the reviewer checks it against the cited sources as in `react`.

Every stage asks for JSON and the parsing is defensive: models wrap JSON in prose and code fences, invent tools and misspell verdicts, and none of that breaks a run. Choose per request with `"strategy": "react" | "plan"`, or set the defaults with `AGENT_STRATEGY` (default `react`) and `INCIDENT_AGENT_STRATEGY` (default `plan`).

### Token budget

`AGENT_TOKEN_BUDGET` (default 60,000 input + output tokens) caps what one run may spend. It's checked before every model call: past it, `plan` skips the remaining gathering and reasoning and answers with the evidence it has, and `react` stops calling tools. Either way the answer is told to say what couldn't be checked, the review is skipped, and the investigation is marked `budget_exhausted: true`.

### Resumable runs

A `plan` run stores its state after every stage: the stage reached, the evidence, the hypotheses, every passage it may cite, and the trace so far. That write is also its heartbeat. Every two minutes the scheduler (`celery beat`) looks for running investigations that have been silent for 3× the timeout:

- with attempts left (`AGENT_MAX_ATTEMPTS`, default 2), the claim is released and the task queued again. The next worker carries on from the last finished stage, so a crash during the answer costs one model call, not six, and the answer can still cite what the first run found. The trace and token counts cover both runs, and `attempts` shows it was resumed;
- the rest are marked failed. A task lost before any worker picked it up is resumed the same way.

Both changes are conditional UPDATEs, so two schedulers can't release or fail the same run twice, and the claim itself stays the single UPDATE that makes a duplicate delivery a no-op.

### Actions need a person's approval

The agent never changes anything. When its answer involves a change (roll back, restart, fail over), it lists it in a separate block, which OpsPilot stores as a **proposed action**: title, the exact command from the runbook, risk, rationale, and citations (only to evidence it actually saw).

| Endpoint | Who |
|---|---|
| `GET /v1/actions?status=proposed` (the approval queue), `GET /v1/investigations/{id}/actions` | viewer |
| `POST /v1/actions/{id}/approve`, `/reject` (with an optional `note`), `/retry` | responder |

Approval is one conditional UPDATE (five people clicking at once: one approval, four `409`s), recorded with who approved it and why, and on the incident's timeline. It's committed *before* anything is sent. Then:

- with no actions webhook, the action is `approved`, for the engineer to carry out;
- with one (`PUT /v1/integrations/actions` with the URL and a signing secret as the token), it's POSTed there, and becomes `executed` or `failed` (retry with `/retry`). The request is signed like Stripe's and GitHub's webhooks:

```
X-OpsPilot-Timestamp: 1764147600
X-OpsPilot-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>.<raw body>" with the secret>
```

The timestamp is signed too, so a receiver can refuse replays. The webhook URL gets the same SSRF checks as telemetry URLs, and `POST /v1/integrations/actions/check` sends a signed `{"type": "ping"}`. Over MCP, assistants can list pending actions but not approve them.

### Postmortem drafts

| Endpoint | Who |
|---|---|
| `POST /v1/incidents/{id}/postmortem` (draft it), `PUT` (save edits) | responder |
| `GET /v1/incidents/{id}/postmortem` | viewer |
| `POST /v1/incidents/{id}/postmortem/publish` | responder, resolved incidents only |

The draft is written from the incident, its timeline, the investigation's answer, hypotheses and actions, in fixed sections (summary, impact, timeline, root cause, what went well and wrong, action items). Anything the material doesn't support is marked `TODO:` for a person to fill in, and citations of anything the investigation didn't cite are removed. Publishing turns the `[c:<id>]` markers into footnotes naming the source and stores it as a `postmortem` document, indexed like an upload, so the next investigation of a similar incident finds it.

### Streaming

`POST /v1/investigations/stream` takes the same body and answers with Server-Sent Events while the agent works:

```
event: investigation
data: {"id":"..."}

event: step
data: {"type":"tool","name":"search_knowledge_base","args":{"query":"ERR_POOL_TIMEOUT"},"ok":true,...}

: keep-alive

event: done
data: {"id":"...","status":"completed","answer":"...","citations":[...],...}
```

A `step` per model call and tool call as it finishes, a comment every `SSE_HEARTBEAT_SECONDS` so proxies keep the connection open, then `done` (the whole investigation) or `error`. The run is a task owned by the server, not by the connection: if the client disconnects it still finishes and is saved. On shutdown, streamed runs still going are stopped where they are, and the scheduler resumes them in a worker from their checkpoint.

### Prompt injection

Runbooks, logs and commit messages are written by people, and some of those people may be attackers: a log line can contain whatever a request put in it. So text that reaches the agent is treated as hostile, in layers ([`security/injection.py`](backend/src/opspilot/security/injection.py)):

1. **Invisible text is removed** at upload: Unicode tag characters (which can spell out a whole instruction the model reads but a reviewer can't see), bidi overrides and zero-width characters.
2. **Suspicious passages are flagged** when indexed: text that addresses an AI rather than an engineer ("ignore previous instructions", "you are now", "do not tell the user", fake role markers, image links that would carry data away). Look-alike characters are folded first, so full-width "Ｉｇｎｏｒｅ" is caught too. Flags show in search results (`flags`), and investigations count them (`flagged_sources`). Nothing is blocked: a runbook *about* prompt injection is indexed, just flagged.
3. **Untrusted text is fenced** (spotlighting): every tool result reaches the model between `⟦untrusted⟧` markers that the system prompt says never to take instructions from, with a WARNING before flagged ones. The marker characters are replaced in the content first, so a document can't close the fence early.
4. **What the agent proposes is checked against what it read.** A proposed action's command must appear in a source it cites (otherwise the agent made it up, or was told to), and none of its sources may be flagged. Failing either raises it to high risk, whatever the agent said, and approving it needs `"confirm_unverified": true`, with the reason shown.

Detection is a heuristic and will miss things; layers 3 and 4 are what matter when it does. The tests use a poisoned runbook that tries to get a `curl | sh` approved.

### Cost tracking and budgets

Every investigation stores its cost (`usage.cost_usd`) at the configured list prices (`LLM_INPUT_USD_PER_MTOK`, `LLM_OUTPUT_USD_PER_MTOK`; the defaults are gemini-3.8-flash's standard rate). On the free tier nothing is billed, but the figure still shows what the traffic would cost.

- `GET /v1/usage?days=30` (viewer): per day, investigations, model calls, tokens and cost; totals; month-to-date spend; how many runs saw flagged sources.
- `PUT /v1/usage/budget` (admin) with `{"monthly_budget_usd": "20.00"}` caps the month's spend. Past it, new investigations and postmortem drafts get `402 budget_exceeded`, and alerts still open incidents but skip the automatic investigation (`investigation_skipped` on the timeline). A run already going is never cut off. `null` removes the cap.

## Incidents and alerts

Point a monitoring tool's webhook at `POST /v1/alerts` with a responder API key as the bearer token. It accepts a simple format, or Prometheus Alertmanager's webhook format as is:

```bash
curl -X POST localhost:8000/v1/alerts -H "Authorization: Bearer ops_..." \
  -H "Content-Type: application/json" \
  -d '{"title": "Payments API error rate above 5%", "severity": "critical",
       "description": "ERR_POOL_TIMEOUT in payments-api logs",
       "labels": {"service": "payments-api", "env": "prod"}}'
```

- **Deduplication:** every alert has a fingerprint, from the tool or computed from its title and labels (in any order). While an incident with that fingerprint is unresolved, repeats only increase its `alert_count`. It's a single `INSERT ... ON CONFLICT` against a partial unique index (`WHERE status <> 'resolved'`), so even 20 identical alerts arriving at the same instant open exactly one incident.
- **Resolution:** a `resolved` alert closes its incident. If the problem comes back, that's a new incident.
- **Automatic investigations:** a new incident at or above `AUTO_INVESTIGATE_SEVERITY` (default `high`) gets an investigation, queued for the worker only after the incident is committed. When the engineer opens the incident, the agent's answer and citations are often already there.
- **Runs once:** a worker claims an investigation with a single `UPDATE ... WHERE started_at IS NULL`, so a task delivered twice runs (and is paid for) once. Investigations whose worker died are marked failed by the scheduler.
- **Timeline:** opened, acknowledged, investigation started/completed/failed, resolved, in order.

| Endpoint | Role |
|---|---|
| `POST /v1/alerts` | responder |
| `GET /v1/incidents` (`?status=open`), `GET /v1/incidents/{id}` (timeline and investigation) | viewer |
| `POST /v1/incidents/{id}/acknowledge`, `/resolve`, `/investigate` | responder |

## Telemetry: metrics, logs and changes

Runbooks say what *should* be checked; telemetry shows what *is* happening. A workspace connects its own Prometheus, Loki and GitHub repository, and CI reports deploys. The agent then checks the evidence before it answers, and cites it.

| Endpoint | What it does | Role |
|---|---|---|
| `PUT /v1/integrations/{prometheus\|loki\|github}` | Connect a source: URL, optional bearer token, repository for GitHub | admin |
| `POST /v1/integrations/{kind}/check` | Try the saved connection | admin |
| `GET /v1/integrations`, `DELETE .../{kind}` | List (tokens never returned), disconnect | viewer / admin |
| `POST /v1/deploys`, `GET /v1/deploys` | CI records each release; list them | responder / viewer |
| `POST /v1/telemetry/metrics` | PromQL over the last 1-360 minutes, summarised per series | viewer |
| `GET /v1/telemetry/metric-names?contains=` | Metric names, to write correct queries | viewer |
| `POST /v1/telemetry/logs` | A service's newest log lines, optionally containing some text | viewer |
| `GET /v1/telemetry/changes?hours=` | Deploys and GitHub commits merged, newest first | viewer |

**The agent's new tools:**

- `query_metrics`, `list_metric_names`, `search_logs` and `recent_changes` are offered only when the matching integration is connected, so the model never calls a tool that can't work. `recent_changes` is always available, since deploys are stored in OpsPilot.
- Every telemetry result gets a citation id, just like a document passage. The answer cites the metric or log lines behind each claim, and the reviewer checks the draft against that same evidence. A citation now carries a `kind` (`document`, `metrics`, `logs` or `changes`).
- Results are bounded before they reach the model:
  - at most 10 series per query, each reduced to last, min, max, average and 8 samples, with the series that moved most first;
  - at most 100 log lines, each cut to 400 characters;
  - windows capped at 6 hours.
- Failures (Prometheus down, a bad query, GitHub's rate limit) come back to the model as text it can reason about, not as crashes. If GitHub can't be read, deploys are still listed, with a warning.

**Security:**

- **Tokens encrypted at rest:** integration tokens are encrypted with a Fernet key (`INTEGRATIONS_KEY`) that lives only in the server's environment, so a database dump doesn't leak a team's Grafana or GitHub tokens. Tokens are never returned by the API.
- **SSRF protection:** integration URLs are chosen by workspace admins, and the server makes requests to them. In production every request's host is resolved and must be a public address. That blocks `localhost`, private networks and the cloud metadata endpoint `169.254.169.254`, and the check is repeated on every request, not only when the URL is saved (DNS rebinding). Redirects are not followed. Private addresses are allowed in development, for a local Prometheus.
- **No query injection:** the model can't write arbitrary LogQL. It names a service (validated) and a text filter (escaped), and OpsPilot builds the query. PromQL is read-only by nature, and is bounded in length and time range.
- **Untrusted logs:** log lines are untrusted input. The agent's instructions tell it to treat tool output as data, never as instructions.

**The demo ([demo/](demo/README.md)):** Kuturl, a real URL shortener, runs next to OpsPilot in Docker with Prometheus, Alertmanager, Loki and Alloy, all free and local. `bash demo/simulate.sh redis-down` (or `db-lock`, `traffic-flood`, `bad-deploy`) breaks it in a known way. Prometheus alerts, Alertmanager creates an OpsPilot incident, and the agent investigates with metrics, logs, deploys and runbooks. Each run's true root cause is recorded, which is the ground truth for evaluating the agent next. The full alert pipeline was verified end to end before merging.

## Using OpsPilot from Claude, Cursor and other AI assistants (MCP)

OpsPilot is also an [MCP](https://modelcontextprotocol.io) server. An engineer can ask their assistant "what's firing, and what does our runbook say?" and the assistant calls OpsPilot itself.

| Tool | What it does | Role |
|---|---|---|
| `search_docs` | Hybrid search over runbooks, postmortems and architecture notes | viewer |
| `list_incidents`, `get_incident` | What's firing, with labels, timeline and the investigation's answer | viewer |
| `get_investigation` | An investigation's answer and citations (optionally the full trace) | viewer |
| `query_metrics`, `list_metric_names`, `search_logs`, `recent_changes` | The workspace's telemetry (see above) | viewer |
| `investigate` | Runs the agent and returns a cited, reviewed answer | responder |
| `investigate_incident` | Starts a fresh background investigation of an incident | responder |
| `acknowledge_incident`, `resolve_incident` | Changes the incident's state | responder |

Plus a `triage_incident` prompt: incident summary, then runbook search, then cited next steps.

**Connecting.** Use any OpsPilot API key; its role decides what the assistant may do (a viewer key makes a read-only assistant).

```bash
# Claude Code, or any client that speaks Streamable HTTP: the API serves MCP at /mcp
claude mcp add --transport http opspilot http://localhost:8000/mcp \
  --header "Authorization: Bearer ops_..."
```

Clients that launch a local command (Claude Desktop, Cursor) use the stdio server, which talks to a running OpsPilot:

```json
{
  "mcpServers": {
    "opspilot": {
      "command": "uv",
      "args": ["--directory", "/path/to/OpsPilot/backend", "run", "opspilot-mcp"],
      "env": { "OPSPILOT_URL": "http://localhost:8000", "OPSPILOT_API_KEY": "ops_..." }
    }
  }
}
```

**Design:**

- **A client of the REST API, not a second way in.** Every tool call goes through the same endpoints as any HTTP client, in-process inside the API with no network hop. So API-key auth, roles, per-key rate limits, validation and workspace isolation apply unchanged, and there is no duplicated logic to drift. A revoked key stops working on the very next MCP request.
- **Keys checked at the door.** `/mcp` rejects requests without a valid key with a plain `401` before any MCP processing; that check doesn't spend the key's rate limit.
- **Stateless Streamable HTTP.** Every MCP request stands alone, so any API instance behind a load balancer can answer it.
- **Long calls stay alive.** `investigate` can take a minute or more, so it sends progress notifications every few seconds and clients don't time it out.
- **Safety hints for the model.** Tools carry hints: read-only tools are marked read-only, and the tools that change shared state say in their descriptions to act only when the user asks. Errors come back as tool errors the model can read ("This API key's role isn't allowed to do that", "Rate limited; try again in 12 seconds"), not as crashes.
- **Switching it off.** `MCP_ENABLED=false` turns off the `/mcp` endpoint.

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
