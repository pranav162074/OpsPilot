# Redis memory pressure

## Symptoms

- Writes fail with `OOM command not allowed when used memory > 'maxmemory'`.
- The evicted_keys counter climbs and cache hit rate drops sharply.
- Session lookups miss, so users get logged out at random.

## Diagnose

Find what is using the memory: `redis-cli --bigkeys` lists the largest key of each type, and
`MEMORY USAGE <key>` gives the exact size of one key. A single unbounded list or hash that
grows forever is the usual cause.

Check the eviction policy with `CONFIG GET maxmemory-policy`. The cache cluster must use
`allkeys-lru`; the queue cluster must use `noeviction`, because evicting queue data loses jobs.

## Mitigate

1. If one key is huge and disposable, delete it with `UNLINK` (non-blocking), not `DEL`.
2. Raise `maxmemory` only if the host has free RAM; leave 25% headroom for fork during
   background saves.
3. Add a TTL to keys written without one.
