# Payments API runbook

Owner: payments-platform team. Pager: #payments-oncall.

## High 5xx error rate

Alert `PaymentsApi5xxHigh` fires when more than 2% of requests fail for five minutes.

1. Open the Payments API dashboard and check whether the spike lines up with a deploy.
2. If a deploy went out in the last hour, roll it back first and investigate after:
   `kubectl rollout undo deployment/payments-api -n payments`.
3. If there was no deploy, check the downstream dependencies panel (database, fraud service,
   card network gateway) for the one whose latency rose first.

## Database connection pool exhaustion

Symptom: requests hang for 30 seconds and then fail with `ERR_POOL_TIMEOUT` in the logs.
Every pod holds a pool of 20 connections, and PgBouncer allows 400 in total.

- Look for long-running transactions in `pg_stat_activity`; a stuck migration or report query
  can hold connections for minutes.
- Do not raise the pool size during the incident: more connections make the database slower.
- If PgBouncer shows clients waiting, scale the API down to shed load, then cancel the
  long queries with `pg_cancel_backend`.

## Card network gateway timeouts

If the gateway times out, payments move to `pending_confirmation`. Do not retry them by hand:
the reconciliation job confirms or voids them within 15 minutes.
