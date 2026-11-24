# OpsPilot roadmap

The plan OpsPilot is built against: one branch at a time, each with tests, a pull request, green CI and a merge. Early branches combined several planned ones, so the table maps each planned branch to the pull request that delivered it, and lists what is still missing.

✅ done · 🟡 partly done · 🔨 in progress · ⬜ not started

## Phase 1: Foundation

| # | Planned branch | Status | Delivered in | Still missing |
|---|---|---|---|---|
| 1 | `chore/project-setup` | ✅ | #1 | MinIO in Compose (comes with S3 storage) |
| 2 | `feat/workspaces-auth` | ✅ | #2 `feat/workspaces-auth` | |

## Phase 2: The RAG core (resume checkpoint 1)

| # | Planned branch | Status | Delivered in | Still missing |
|---|---|---|---|---|
| 3 | `feat/ingestion-pipeline` | 🟡 | #3 `feat/document-ingestion`: upload, Celery jobs with retries, status API, content-hash dedupe | GitHub source connector, raw files in S3 (MinIO locally), dead-letter queue |
| 4 | `feat/chunking-embeddings` | 🟡 | #3, #4: heading-aware chunking, batched embeddings, HNSW index, re-indexing only stale documents | Code chunked by function |
| 5 | `feat/hybrid-retrieval` | 🟡 | #4 `feat/hybrid-search`: full-text + vector search, RRF, metadata filters, query-embedding cache | Cross-encoder re-ranker |
| 6 | `feat/retrieval-evals` | 🟡 | #4, #5: question set, hit@k and MRR, comparison table in the README | Quality gate in CI |
| 7 | `feat/grounded-answers` | 🟡 | #5: answers citing passages, citations verified, reviewer pass, token counts | Streaming over SSE, prompt-injection defences, cost tracking, faithfulness evaluation |

## Phase 3: The incident agent (resume checkpoint 2)

| # | Planned branch | Status | Delivered in | Still missing |
|---|---|---|---|---|
| 8 | `feat/telemetry-tools` | ⬜ | | Prometheus, log-search and commits/deploys tools; Kuturl in Compose; the Bash incident simulator |
| 9 | `feat/investigation-agent` | 🟡 | #5 `feat/investigation-agent`: LangGraph agent, step limit, timeout, saved traces, background runs | plan → parallel evidence → hypotheses → verify flow, token budget, resumable runs, human approval |
| 10 | `feat/incident-lifecycle` | 🟡 | #6 `feat/incidents`: open → acknowledged → resolved, Alertmanager webhook, automatic investigations, timeline | Draft postmortems |
| 11 | `feat/mcp-server` | 🔨 | `feat/mcp-server` | |

## Phase 4: Production-grade (resume checkpoint 3)

| # | Planned branch | Status |
|---|---|---|
| 12 | `feat/observability`: OpenTelemetry traces, Prometheus metrics (latency, tokens, cost, retrieval hit rate), Grafana dashboards in code | ⬜ |
| 13 | `feat/web-ui`: Next.js dashboard (streamed chat, incident view, citations) with Playwright tests | ⬜ |
| 14 | `perf/load-testing`: JMeter tests for ingestion and querying, optimisations with before/after numbers | ⬜ |
| 15 | `feat/kubernetes`: multi-stage images, manifests, worker autoscaling, kind smoke test in CI | ⬜ |
| 16 | `feat/aws-terraform`: VPC, compute, S3, ECR, IAM, secrets; deploys via GitHub OIDC; teardown script | ⬜ |
| 17 | `docs/results`: architecture diagram, results tables, demo video, final resume bullets | ⬜ |

## Next, in order

1. **#8 `feat/telemetry-tools`.** The agent needs real metrics, logs and deploys to investigate, and Kuturl plus the incident simulator give staged incidents with known causes to measure it against.
2. **#9 `feat/agent-v2`.** The rest of the planned agent: the plan → evidence → hypotheses → verify flow, token budget, resumable runs, human approval before actions, and draft postmortems (#10's gap).
3. **`feat/answer-quality`.** The rest of #5–#7: re-ranker, CI evaluation gate, SSE streaming, prompt-injection defences, cost tracking, faithfulness evaluation.
4. **`feat/sources-storage`.** The rest of #3–#4: GitHub connector, S3/MinIO storage, dead-letter queue, code chunking.
5. Then #12 → #17 as planned.

## Ground rules

- Every branch ships with tests; CI runs against real Postgres, pgvector and Redis.
- LLM calls are mocked in CI. Evaluations against the real model run by hand, and their reports are committed.
- No number goes in the README or on the resume until it has been measured.
- AWS resources exist only for a demo, then are destroyed.
