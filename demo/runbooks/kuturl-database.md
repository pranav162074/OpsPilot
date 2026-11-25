# Kuturl runbook: slow or failing database

Kuturl's Postgres holds links, API keys, idempotency records, clicks and hourly rollups.
Redirects for cached links don't touch it; API calls (`/api/v1/...`) and cache misses do.

## Symptoms

- Alert `KuturlHighLatency` (p95 over 500 ms), mostly on routes under `/api/v1/links`.
- Alert `KuturlDatabaseLocked`: `pg_locks_count{mode="accessexclusivelock"}` above 0.
- Requests time out while CPU stays low: they are waiting, not working.

## Checks

1. Which routes are slow? `histogram_quantile(0.95, sum by (le, route)
   (rate(http_request_duration_seconds_bucket{job="kuturl"}[1m])))`.
2. Are queries waiting on locks? `pg_locks_count` by `mode`, and in psql:
   `SELECT pid, state, wait_event_type, query_start, left(query, 80) FROM pg_stat_activity
   WHERE datname = 'kuturl' ORDER BY query_start;`
3. A long-running transaction holding an `ACCESS EXCLUSIVE` lock (a migration, a manual
   `LOCK TABLE`, a stuck `ALTER TABLE`) blocks every query on that table.
4. Check recent changes: did a deploy run a migration?

## Fix

1. Find the blocking session: `SELECT pg_blocking_pids(pid), pid, left(query, 80) FROM
   pg_stat_activity WHERE wait_event_type = 'Lock';`
2. If it is a stuck manual or maintenance transaction, end it:
   `SELECT pg_terminate_backend(<pid>);`
3. If it is a migration, let it finish if it is close, otherwise stop the deploy and roll back.

## Don't

- Don't raise the connection pool size: waiting queries would just pile up faster.
