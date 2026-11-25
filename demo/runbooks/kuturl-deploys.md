# Kuturl runbook: problems right after a deploy

## Symptoms

- Alert `KuturlDown`: Prometheus can't scrape Kuturl (`up{job="kuturl"} == 0`).
- The container restarts over and over (a crash loop).
- A deploy of Kuturl in the last few minutes.

## Checks

1. Recent changes: which deploy went out just before the alert, and what did it change?
2. Kuturl's logs from the last minutes. At startup Kuturl validates its configuration and
   runs migrations, so a bad value fails loudly:
   - `Invalid environment configuration` names the missing or malformed variable.
   - `password authentication failed for user "kuturl"` means `DATABASE_URL` has the wrong
     credentials.
   - `ECONNREFUSED` means the database or Redis host is wrong or down.

## Fix

1. Roll back to the previous configuration or version (in the demo:
   `bash demo/simulate.sh restore`).
2. Fix the configuration, then deploy again and watch `up{job="kuturl"}` return to 1.
3. Record the rollback as a deploy, so the timeline shows it.
