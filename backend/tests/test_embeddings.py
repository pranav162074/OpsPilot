import json
import math

import httpx
import pytest
from redis.asyncio import Redis

from opspilot.config import Settings
from opspilot.embeddings import EmbeddingError, EmbeddingUnavailable, HashingEmbedder
from opspilot.embeddings.cache import QueryEmbeddingCache
from opspilot.embeddings.gemini import GeminiEmbedder


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_hashing_embedder_is_deterministic_unit_length_and_lexical() -> None:
    embedder = HashingEmbedder()
    [a, b, c] = await embedder.embed_documents(
        [
            "restart the payments api pods",
            "restart payments api pods now",
            "kafka consumer lag is rising",
        ]
    )

    assert len(a) == 768
    assert math.isclose(math.sqrt(sum(v * v for v in a)), 1.0)
    assert await embedder.embed_query("restart the payments api pods") == a
    assert cosine(a, b) > 0.5 > cosine(a, c)


def gemini_with(handler: httpx.MockTransport) -> GeminiEmbedder:
    return GeminiEmbedder("test-key", client=httpx.AsyncClient(transport=handler))


def fake_vectors(count: int, dims: int = 768) -> dict[str, object]:
    return {"embeddings": [{"values": [3.0, 4.0] + [0.0] * (dims - 2)} for _ in range(count)]}


async def test_gemini_request_shape_batching_and_normalization() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json=fake_vectors(len(body["requests"])))

    embedder = gemini_with(httpx.MockTransport(handler))
    vectors = await embedder.embed_documents([f"chunk {i}" for i in range(150)])
    query = await embedder.embed_query("why is the api slow")

    assert len(vectors) == 150
    # 150 documents need two batches of at most 100, plus one call for the query
    assert [len(json.loads(r.content)["requests"]) for r in seen] == [100, 50, 1]
    first = seen[0]
    assert str(first.url).endswith("/models/gemini-embedding-001:batchEmbedContents")
    assert first.headers["x-goog-api-key"] == "test-key"
    request = json.loads(first.content)["requests"][0]
    assert request["taskType"] == "RETRIEVAL_DOCUMENT"
    assert request["outputDimensionality"] == 768
    assert json.loads(seen[2].content)["requests"][0]["taskType"] == "RETRIEVAL_QUERY"
    # 3-4-5 triangle: normalized to unit length
    assert query[:2] == [0.6, 0.8]


@pytest.mark.parametrize(
    ("response", "error", "retry_after"),
    [
        (httpx.Response(429, headers={"retry-after": "7"}), EmbeddingUnavailable, 7.0),
        (httpx.Response(503), EmbeddingUnavailable, None),
        (
            httpx.Response(400, json={"error": {"message": "API key not valid"}}),
            EmbeddingError,
            None,
        ),
        (httpx.Response(200, json=fake_vectors(1, dims=3072)), EmbeddingError, None),
        (httpx.Response(200, json={"embeddings": []}), EmbeddingError, None),
    ],
)
async def test_gemini_errors_are_classified(
    response: httpx.Response, error: type[Exception], retry_after: float | None
) -> None:
    embedder = gemini_with(httpx.MockTransport(lambda _: response))

    with pytest.raises(error) as caught:
        await embedder.embed_query("q")

    if isinstance(caught.value, EmbeddingUnavailable):
        assert caught.value.retry_after == retry_after


async def test_gemini_network_errors_are_temporary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(EmbeddingUnavailable):
        await gemini_with(httpx.MockTransport(handler)).embed_query("q")


class CountingEmbedder(HashingEmbedder):
    def __init__(self) -> None:
        self.calls = 0

    async def embed_query(self, text: str) -> list[float]:
        self.calls += 1
        return await super().embed_query(text)


async def test_query_embeddings_are_cached(settings: Settings) -> None:
    redis = Redis.from_url(settings.redis_url)
    await redis.flushdb()
    cache = QueryEmbeddingCache(redis, ttl_seconds=60)
    embedder = CountingEmbedder()

    first = await cache.embed_query(embedder, "Why is the API slow?")
    second = await cache.embed_query(embedder, "  why is the api slow?  ")
    await redis.aclose()

    assert embedder.calls == 1
    # float32 storage: equal to about 7 significant digits
    assert second == pytest.approx(first, abs=1e-6)


async def test_cache_falls_through_when_redis_is_down() -> None:
    redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    embedder = CountingEmbedder()

    vector = await QueryEmbeddingCache(redis, 60).embed_query(embedder, "q")
    await redis.aclose()

    assert len(vector) == 768
    assert embedder.calls == 1
