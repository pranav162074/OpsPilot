# The demo: a real service for OpsPilot to investigate

OpsPilot is only as good as what it can see. This folder runs **Kuturl** (a URL shortener,
[github.com/pranav162074/Kuturl](https://github.com/pranav162074/Kuturl)) next to OpsPilot,
with the telemetry a real team would have. A simulator then breaks it in known ways, so every
investigation can be checked against a cause we know.

```
 traffic ──► Kuturl ──► Postgres, Redis
               │ /metrics            │ exporters
               ▼                     ▼
            Prometheus ── alert rules ──► Alertmanager ──► OpsPilot /v1/alerts ──► incident
               ▲                                                     │ (high/critical)
 container logs ──► Alloy ──► Loki                                  ▼
               ▲        ▲                                   investigation agent
               └────────┴──── query_metrics / search_logs / recent_changes ◄┘
```

Everything is free and runs locally in Docker. Pinned versions: Prometheus 3.16,
Alertmanager 0.35, Loki 3.7, Alloy 1.22, Postgres 17, Redis 7.

## Run it

You need Docker Desktop, and OpsPilot's API and worker running (see the main README). On
Windows, run the `.sh` scripts in **Git Bash**.

```bash
# 1. Start Kuturl and its telemetry (the first build takes a few minutes)
docker compose --profile demo up -d --build

# 2. Connect your OpsPilot workspace (an admin API key of your workspace)
OPSPILOT_API_KEY=ops_... bash demo/setup.sh

# 3. Break something
bash demo/simulate.sh list
bash demo/simulate.sh redis-down
```

| Address | What |
|---|---|
| http://localhost:3001 | Kuturl |
| http://localhost:9090/alerts | Prometheus: alert rules and their state |
| http://localhost:9093 | Alertmanager |
| http://localhost:3100 | Loki (queried by OpsPilot) |

`setup.sh` does the following:

- Connects Prometheus, Loki and Kuturl's GitHub repository to the workspace.
- Creates a **responder** key for Alertmanager. It writes the key to `demo/alertmanager/secrets/`, which is git-ignored.
- Uploads the four Kuturl runbooks in `runbooks/`.
- Records Kuturl's current deploy.

## Scenarios

| Scenario | What breaks | Alerts that fire | Where the evidence is |
|---|---|---|---|
| `redis-down` | Kuturl's Redis is stopped | `KuturlRedisDown` (high) | `redis_up`, cache error metrics, "Could not record a click" logs |
| `db-lock` | A transaction holds an exclusive lock on `links` | `KuturlDatabaseLocked`, `KuturlHighLatency`, `KuturlHighErrorRate` | `pg_locks_count`, latency by route |
| `traffic-flood` | 200 concurrent connections hit one short link | `KuturlTrafficSpike` | request rate by route |
| `bad-deploy` | A "new version" ships with a wrong database password and crash-loops | `KuturlDown` (critical) | `up`, `password authentication failed` in logs, the deploy in recent changes |

**What a run does:**

1. Holds the fault for 180 seconds (or the number you pass, e.g. `simulate.sh db-lock 120`).
2. Restores everything.
3. Appends the true root cause to `demo/simulations.jsonl`. These records are the ground truth for measuring the agent's accuracy (next branch).

**What OpsPilot does in the meantime:**

1. An alert creates an incident.
2. High and critical incidents get an investigation automatically.
3. The agent checks the metrics, reads the logs, looks at recent deploys and searches the runbooks.
4. Its answer cites each of those.

When the fault clears, Alertmanager sends `resolved` and the incident closes itself.

`bash demo/simulate.sh restore` puts everything back at any time. `docker compose --profile demo down`
stops the demo (add `-v` to also delete its data).

## Verified

Before this branch was merged, the pipeline was run end to end:

- **Scraping:** Kuturl from this pinned commit, scraped by Prometheus with its metrics token.
- **Redis outage:** stopping Kuturl's Redis fired `KuturlRedisDown`, and Alertmanager posted it to OpsPilot as a high-severity incident.
- **Recovery:** restarting Redis resolved the incident automatically.
- **Database lock:** locking the `links` table raised `pg_locks_count{mode="accessexclusivelock"}` to 1, and the latency and error-rate alerts fired.
- **Queries:** OpsPilot's metric, log and change queries returned the live data.
