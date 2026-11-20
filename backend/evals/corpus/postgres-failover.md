# PostgreSQL primary failover

Use this when the primary database is down or unreachable and will not recover quickly.

## Confirm the primary is really gone

Check from two different hosts. A network partition can make a healthy primary look dead,
and promoting a replica while the old primary still accepts writes causes split brain.

## Pick the replica to promote

Choose the replica with the least replication lag:

    SELECT client_addr, replay_lag FROM pg_stat_replication;

If every replica is more than a minute behind, escalate to the database team before promoting:
transactions committed on the old primary after that point will be lost.

## Promote it

On the chosen replica run `pg_ctl promote -D /var/lib/postgresql/data`, or use
`SELECT pg_promote();` from psql.

## Point traffic at the new primary

Update the DNS record `db-primary.internal` to the new host. The TTL is 30 seconds, so
applications reconnect within a minute. Restart PgBouncer to drop cached connections.

## Afterwards

Rebuild the old primary as a replica with `pg_basebackup`; never bring it back as a primary.
