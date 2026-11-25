# Kuturl runbook: traffic spikes

## Symptoms

- Alert `KuturlTrafficSpike`: more than 150 requests a second.
- Latency rising on the redirect route `/:code` while error rates stay low.
- Most of the traffic on one or a few short codes (a link shared widely, or a bot).

## Checks

1. Total and per-route rate: `sum by (route) (rate(http_request_duration_seconds_count{job="kuturl"}[1m]))`.
2. Is it real users or one client? Kuturl's access logs show the client IP and user agent.
3. Is the cache doing its job? Redirect hits should be served from Redis:
   `sum by (cache) (rate(kuturl_redirects_total[1m]))`.

## Fix

1. A popular link: nothing to fix if latency is acceptable; Kuturl is built for read spikes.
2. Abuse from a few IPs: block them at the proxy or load balancer in front of Kuturl.
   Redirects are deliberately not rate limited inside Kuturl, so the block has to happen there.
3. Latency too high: run more Kuturl instances; they share Redis and Postgres safely.
