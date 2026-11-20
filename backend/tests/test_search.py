import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update

from opspilot.config import Settings
from opspilot.embeddings import EmbeddingError, EmbeddingUnavailable, HashingEmbedder
from opspilot.evals.retrieval import evaluate
from opspilot.ingestion.service import Outcome, ingest_document, mark_for_reindex
from opspilot.models import Chunk, Document
from opspilot.search.service import reciprocal_rank_fusion
from tests.conftest import auth, upload

POOL = """# Payments API

## Connection pool exhaustion

Requests hang and fail with ERR_POOL_TIMEOUT. Look for long transactions in pg_stat_activity.
"""
FAILOVER = """# Database failover

## Promote a replica

When the primary database dies, promote the replica with the least replication lag.
"""
KAFKA = """# Kafka

## Consumer lag

Messages pile up when consumers are slow. Check lag per partition.
"""


def a(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def test_rrf_rewards_agreement_between_retrievers() -> None:
    fused = reciprocal_rank_fusion({"vector": [a(1), a(2), a(3)], "keyword": [a(3), a(4)]}, k=60)

    # a(3) is only third by vector, but both retrievers found it, so it wins
    # a(2) and a(4) are both second in one list: equal scores, so the lower id comes first
    assert [f.chunk_id for f in fused] == [a(3), a(1), a(2), a(4)]
    assert fused[0].score == pytest.approx(1 / 63 + 1 / 61)
    assert fused[0].ranks == {"vector": 3, "keyword": 1}
    assert fused[3].ranks == {"vector": None, "keyword": 2}


def test_rrf_breaks_ties_by_id_and_handles_empty_lists() -> None:
    fused = reciprocal_rank_fusion({"vector": [a(2)], "keyword": [a(1)]}, k=60)

    assert [f.chunk_id for f in fused] == [a(1), a(2)]
    assert reciprocal_rank_fusion({"vector": [], "keyword": []}, k=60) == []


@pytest.fixture
async def indexed(
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
    ingest: Callable[..., Awaitable[Outcome]],
) -> dict[str, Any]:
    _, key = await create_workspace()
    ids = {}
    for name, content, source_type in [
        ("pool.md", POOL, "runbook"),
        ("failover.md", FAILOVER, "runbook"),
        ("kafka.md", KAFKA, "postmortem"),
    ]:
        res = await upload(client, key, content, name, source_type=source_type)
        ids[name] = res.json()["id"]
        assert await ingest(ids[name]) == Outcome.READY
    return {"key": key, "ids": ids}


async def search(client: AsyncClient, key: str, **body: Any) -> Any:
    res = await client.post("/v1/search", json=body, headers=auth(key))
    assert res.status_code == 200, res.text
    return res.json()


async def test_chunks_are_embedded_with_the_model_name(
    app: FastAPI, indexed: dict[str, Any]
) -> None:
    async with app.state.sessionmaker() as session:
        chunks = list(await session.scalars(select(Chunk)))

    assert chunks
    assert all(c.embedding is not None and len(c.embedding) == 768 for c in chunks)
    assert {c.embedding_model for c in chunks} == {"hashing-v1"}


async def test_hybrid_search_finds_the_passage(
    client: AsyncClient, indexed: dict[str, Any]
) -> None:
    body = await search(client, indexed["key"], query="primary database died, promote replica")

    top = body["results"][0]
    assert body["mode"] == "hybrid"
    assert body["degraded"] is False
    assert top["document_id"] == indexed["ids"]["failover.md"]
    assert top["heading"] == "Database failover > Promote a replica"
    assert top["ranks"]["vector"] == 1
    assert FAILOVER[top["char_start"] : top["char_end"]] == top["content"]


async def test_keyword_mode_matches_exact_identifiers(
    client: AsyncClient, indexed: dict[str, Any]
) -> None:
    body = await search(client, indexed["key"], query="ERR_POOL_TIMEOUT", mode="keyword")

    assert [r["document_id"] for r in body["results"]] == [indexed["ids"]["pool.md"]]
    assert body["results"][0]["ranks"] == {"vector": None, "keyword": 1}


async def test_keyword_search_stems_words(client: AsyncClient, indexed: dict[str, Any]) -> None:
    # "promoting" matches "promote", "dies" matches "dies"/"died"
    body = await search(client, indexed["key"], query="promoting replicas", mode="keyword")

    assert body["results"][0]["document_id"] == indexed["ids"]["failover.md"]


async def test_filters_by_source_type_and_limits(
    client: AsyncClient, indexed: dict[str, Any]
) -> None:
    body = await search(
        client, indexed["key"], query="consumer lag database", source_types=["postmortem"]
    )
    limited = await search(client, indexed["key"], query="database", limit=1)

    assert {r["source_type"] for r in body["results"]} == {"postmortem"}
    assert len(limited["results"]) == 1


async def test_never_returns_another_workspaces_documents(
    client: AsyncClient, create_workspace: Callable[..., Any], indexed: dict[str, Any]
) -> None:
    _, other = await create_workspace("Globex")

    for mode in ["hybrid", "vector", "keyword"]:
        body = await search(client, other, query="ERR_POOL_TIMEOUT promote replica", mode=mode)
        assert body["results"] == []


async def test_ignores_vectors_from_another_model(
    app: FastAPI, client: AsyncClient, indexed: dict[str, Any]
) -> None:
    async with app.state.sessionmaker() as session:
        await session.execute(update(Chunk).values(embedding_model="old-model"))
        await session.commit()

    body = await search(client, indexed["key"], query="promote replica", mode="vector")

    assert body["results"] == []


class BrokenEmbedder(HashingEmbedder):
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def embed_query(self, text: str) -> list[float]:
        raise self.error

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise self.error


async def test_hybrid_falls_back_to_keywords_when_embeddings_are_down(
    app: FastAPI, client: AsyncClient, indexed: dict[str, Any]
) -> None:
    app.state.embedder = BrokenEmbedder(EmbeddingUnavailable("provider down"))

    hybrid = await search(client, indexed["key"], query="ERR_POOL_TIMEOUT")
    vector = await client.post(
        "/v1/search", json={"query": "x", "mode": "vector"}, headers=auth(indexed["key"])
    )

    assert hybrid["degraded"] is True
    assert hybrid["results"][0]["document_id"] == indexed["ids"]["pool.md"]
    assert vector.status_code == 503
    assert vector.json()["error"]["code"] == "search_unavailable"


async def test_rejects_bad_requests(client: AsyncClient, indexed: dict[str, Any]) -> None:
    for body in [{"query": ""}, {"query": "x", "limit": 51}, {"query": "x", "mode": "fuzzy"}]:
        res = await client.post("/v1/search", json=body, headers=auth(indexed["key"]))
        assert res.status_code == 422


async def test_a_permanent_embedding_error_fails_the_document(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()
    doc = (await upload(client, key, POOL)).json()

    outcome = await ingest_document(
        app.state.sessionmaker,
        settings,
        uuid.UUID(doc["id"]),
        BrokenEmbedder(EmbeddingError("API key not valid")),
    )

    assert outcome == Outcome.FAILED
    fetched = (await client.get(f"/v1/documents/{doc['id']}", headers=auth(key))).json()
    assert fetched["status"] == "failed"
    assert fetched["error"] == "Embedding failed: API key not valid"


async def test_a_temporary_embedding_error_hands_the_document_back(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()
    doc = (await upload(client, key, POOL)).json()

    with pytest.raises(EmbeddingUnavailable):
        await ingest_document(
            app.state.sessionmaker,
            settings,
            uuid.UUID(doc["id"]),
            BrokenEmbedder(EmbeddingUnavailable("rate limited")),
        )

    fetched = (await client.get(f"/v1/documents/{doc['id']}", headers=auth(key))).json()
    assert fetched["status"] == "pending"


async def test_reindex_picks_documents_embedded_by_another_model(
    app: FastAPI, indexed: dict[str, Any]
) -> None:
    async with app.state.sessionmaker() as session:
        await session.execute(
            update(Chunk)
            .where(Chunk.document_id == uuid.UUID(indexed["ids"]["kafka.md"]))
            .values(embedding_model="old-model")
        )
        await session.commit()
        stale = await mark_for_reindex(session, "hashing-v1")
        everything = await mark_for_reindex(session, "hashing-v1", everything=True)
        statuses = set(await session.scalars(select(Document.status)))

    assert stale == [uuid.UUID(indexed["ids"]["kafka.md"])]
    # The first call already set kafka.md back to pending, so --all finds the other two
    assert len(everything) == 2
    assert statuses == {"pending"}


async def test_retrieval_quality_does_not_regress(app: FastAPI, settings: Settings) -> None:
    """The eval corpus with the offline hashing embedder. Thresholds sit just under the
    current scores, so a change that makes retrieval worse fails here."""
    report = await evaluate(app.state.sessionmaker, settings, HashingEmbedder())

    assert report["keyword"]["keyword"].hit_at_1 == 1.0
    assert report["hybrid"]["keyword"].hit_at_1 == 1.0
    assert report["hybrid"]["all"].mrr >= 0.85
    # Hybrid is at least as good as either retriever alone
    assert report["hybrid"]["all"].mrr >= report["vector"]["all"].mrr
    assert report["hybrid"]["all"].mrr >= report["keyword"]["all"].mrr
