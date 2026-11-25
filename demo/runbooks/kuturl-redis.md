# Kuturl runbook: Redis problems

Kuturl uses Redis for the redirect cache, rate limits, click-limit counters, the click event
stream (consumed by the click worker) and pub/sub for the Bloom filter. Postgres stays the
source of truth.

## Symptoms

- Alert `KuturlRedisDown`: `redis_up{job="kuturl-redis"}` is 0.
- `kuturl_cache_errors_total` rises, and Kuturl logs warnings such as
  `Could not record a click` with Redis connection errors.
- Redirects still work but get slower, because every redirect is a cache miss that goes to
  Postgres (`http_request_duration_seconds` for route `/:code` goes up).
- Creating API keys, password guesses and click-limited links fail on purpose: those limits
  refuse to run without Redis (they fail closed).
- Click statistics stop updating: new clicks can't be added to the stream.

## Checks

1. Confirm Redis is the problem: `redis_up` and `up{job="kuturl-redis"}` in Prometheus.
2. Look at Kuturl's logs for `redis` errors in the last 15 minutes.
3. Check recent changes: a deploy that changed `REDIS_URL` or `REDIS_KEY_PREFIX` looks the same.

## Fix

1. Start Redis again (in the demo: `docker compose --profile demo start kuturl-redis`).
2. Kuturl reconnects by itself within a few seconds; no restart is needed.
3. The cache refills on demand. Clicks made while Redis was down are lost; tell whoever
   relies on exact click counts.

## Don't

- Don't restart Kuturl while Redis is down: it will just keep failing its Redis calls.
- Don't switch rate limiting off (`RATE_LIMIT_ENABLED=false`) to "fix" failed key creation.
