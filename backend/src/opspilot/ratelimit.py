import logging
import math
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# Counts requests in fixed windows. INCR and the expiry happen in one atomic script, so two
# requests arriving together can never both see "first request" and skip setting the expiry.
_FIXED_WINDOW = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('PTTL', KEYS[1])
return {count, ttl}
"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._script = redis.register_script(_FIXED_WINDOW)

    async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        try:
            count, ttl_ms = await self._script(
                keys=[f"ratelimit:{key}"], args=[window_seconds * 1000]
            )
        except RedisError:
            # Fail open: a Redis outage should slow nobody down. The cost is that limits are
            # not enforced while Redis is down, which is logged so it doesn't go unnoticed.
            logger.warning("Rate limiter unavailable, allowing request", exc_info=True)
            return RateLimitResult(True, limit, limit, window_seconds)

        count, ttl_ms = int(count), int(ttl_ms)
        return RateLimitResult(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            reset_seconds=max(1, math.ceil(ttl_ms / 1000)),
        )
