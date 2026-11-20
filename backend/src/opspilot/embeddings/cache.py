import hashlib
import logging
import struct

from redis.asyncio import Redis
from redis.exceptions import RedisError

from opspilot.embeddings.base import Embedder

logger = logging.getLogger(__name__)


class QueryEmbeddingCache:
    """Caches query embeddings in Redis: the same question asked twice costs one API call.

    Keyed by model and text, so changing the model never serves a stale vector. Stored as packed
    float32 (3 KB for 768 numbers, against ~15 KB as JSON). Redis problems just skip the cache.
    """

    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def embed_query(self, embedder: Embedder, text: str) -> list[float]:
        digest = hashlib.sha256(text.strip().lower().encode()).hexdigest()
        key = f"qemb:{embedder.model}:{embedder.dimensions}:{digest}"
        try:
            cached = await self._redis.get(key)
            if isinstance(cached, bytes):
                return list(struct.unpack(f"<{embedder.dimensions}f", cached))
        except (RedisError, struct.error):
            logger.warning("Query embedding cache unavailable", exc_info=True)

        vector = await embedder.embed_query(text)
        try:
            await self._redis.set(key, struct.pack(f"<{len(vector)}f", *vector), ex=self._ttl)
        except RedisError:
            logger.warning("Could not cache query embedding", exc_info=True)
        return vector
