# Postmortem: site-wide outage on 14 March 2026

Duration: 47 minutes. Impact: every page on the website and the mobile app's API returned TLS
errors; roughly 120,000 users could not log in or pay.

## What happened

The TLS certificate on the CDN edge expired at 09:12 UTC. cert-manager had tried to renew it
for nine days, but each attempt failed: the DNS-01 challenge could not create its TXT record
because the DNS provider API token had been rotated and the new token was never added to the
cluster secret.

## Why we didn't notice

Renewal failures were logged but not alerted on, and the certificate expiry alert was set to
fire one day before expiry on a dashboard nobody watched over the weekend.

## Resolution

We issued a certificate by hand, uploaded it to the CDN at 09:59, and service recovered.

## Action items

- Alert on cert-manager renewal failures immediately, and on certificates expiring within 14 days.
- Store the DNS provider token in the secrets manager, with rotation that updates the cluster.
- Add the CDN certificate to the weekly on-call checklist.
