"""Hybrid search: vector similarity and keyword matching, fused by Reciprocal Rank Fusion.

Each retriever is good at what the other misses. Embeddings match meaning ("the database is
out of connections" finds a passage about "pool exhaustion"); keywords match exact tokens that
embeddings blur (an error code like ERR_POOL_TIMEOUT, a hostname, a flag). RRF combines the two
ranked lists using only ranks, so the retrievers' incomparable scores never need calibrating:
score = sum over lists of 1 / (k + rank).
"""

import enum
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.models import Chunk, Document, SourceType
from opspilot.search.rerank import Reranker

logger = logging.getLogger(__name__)


class SearchMode(enum.StrEnum):
    HYBRID = "hybrid"
    VECTOR = "vector"
    KEYWORD = "keyword"


@dataclass
class Fused:
    chunk_id: uuid.UUID
    score: float
    # 1-based rank in each retriever's list, or None if that retriever didn't find it
    ranks: dict[str, int | None] = field(default_factory=dict)
    # The cross-encoder's score, when the results were re-ranked
    rerank_score: float | None = None


def reciprocal_rank_fusion(rankings: Mapping[str, Sequence[uuid.UUID]], k: int) -> list[Fused]:
    fused: dict[uuid.UUID, Fused] = {}
    for name, ids in rankings.items():
        for rank, chunk_id in enumerate(ids, start=1):
            entry = fused.setdefault(
                chunk_id, Fused(chunk_id, 0.0, {other: None for other in rankings})
            )
            entry.score += 1.0 / (k + rank)
            entry.ranks[name] = rank
    # Ties (same score) are broken by id, so results are stable between identical requests
    return sorted(fused.values(), key=lambda f: (-f.score, str(f.chunk_id)))


def _scope(workspace_id: uuid.UUID, source_types: Sequence[SourceType] | None) -> list[object]:
    conditions: list[object] = [Chunk.workspace_id == workspace_id]
    if source_types:
        conditions.append(Document.source_type.in_([s.value for s in source_types]))
    return conditions


async def vector_candidates(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    query_vector: list[float],
    model: str,
    source_types: Sequence[SourceType] | None,
    limit: int,
) -> list[uuid.UUID]:
    # The HNSW index finds nearest neighbours across every workspace, then the WHERE clause
    # filters. For a small workspace in a big table that could leave too few rows; an
    # iterative scan (pgvector 0.8+) keeps searching until `limit` rows pass the filter.
    await session.execute(text("SET LOCAL hnsw.ef_search = 100"))
    await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    distance = Chunk.embedding.cosine_distance(query_vector)
    rows = (
        await session.execute(
            select(Chunk.id, distance.label("distance"))
            .join(Document, Document.id == Chunk.document_id)
            # Vectors from different models live in different spaces and can't be compared
            .where(Chunk.embedding_model == model, *_scope(workspace_id, source_types))  # type: ignore[arg-type]
            .order_by(distance)
            .limit(limit)
        )
    ).all()
    # relaxed_order may return neighbours slightly out of order; put them back in order
    return [row.id for row in sorted(rows, key=lambda r: r.distance)]


async def keyword_candidates(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    query: str,
    source_types: Sequence[SourceType] | None,
    limit: int,
) -> list[uuid.UUID]:
    # websearch_to_tsquery understands what people type: "quoted phrases", OR, -exclusions,
    # and never fails on stray punctuation
    tsquery = func.websearch_to_tsquery("english", query)
    rank = func.ts_rank_cd(Chunk.search_vector, tsquery)
    rows = await session.scalars(
        select(Chunk.id)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.search_vector.op("@@")(tsquery), *_scope(workspace_id, source_types))  # type: ignore[arg-type]
        .order_by(rank.desc(), Chunk.id)
        .limit(limit)
    )
    return list(rows)


async def load_results(
    session: AsyncSession, fused: list[Fused]
) -> list[tuple[Fused, Chunk, Document]]:
    if not fused:
        return []
    rows = (
        await session.execute(
            select(Chunk, Document)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.id.in_([f.chunk_id for f in fused]))
        )
    ).all()
    by_id = {chunk.id: (chunk, document) for chunk, document in rows}
    # A chunk can vanish between the searches and this load (its document re-indexed)
    return [(f, *by_id[f.chunk_id]) for f in fused if f.chunk_id in by_id]


async def search_chunks(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    query: str,
    mode: SearchMode,
    query_vector: list[float] | None,
    model: str,
    source_types: Sequence[SourceType] | None,
    candidates: int,
    rrf_k: int,
) -> list[Fused]:
    """Runs the retrievers the mode asks for and fuses their rankings.

    A hybrid search without a query vector (embeddings unavailable) runs on keywords alone.
    """
    rankings: dict[str, list[uuid.UUID]] = {}
    if mode in (SearchMode.HYBRID, SearchMode.VECTOR) and query_vector is not None:
        rankings["vector"] = await vector_candidates(
            session, workspace_id, query_vector, model, source_types, candidates
        )
    if mode in (SearchMode.HYBRID, SearchMode.KEYWORD):
        rankings["keyword"] = await keyword_candidates(
            session, workspace_id, query, source_types, candidates
        )
    return reciprocal_rank_fusion(rankings, rrf_k)


@dataclass
class Retrieval:
    results: list[tuple[Fused, Chunk, Document]]
    reranked: bool
    # Re-ranking was asked for but failed: these are in fusion order
    rerank_failed: bool = False


async def retrieve(
    session: AsyncSession,
    *,
    reranker: Reranker | None,
    rerank_candidates: int,
    limit: int,
    **search: Any,
) -> Retrieval:
    """Search, then (with a re-ranker) re-order the top candidates by the cross-encoder.

    Everything that searches (the API, the agent's tool, the evaluation) goes through here,
    so they all rank the same way. A re-ranker that fails costs ordering, not results."""
    fused = await search_chunks(session, **search)
    if reranker is None or not fused:
        return Retrieval(await load_results(session, fused[:limit]), reranked=False)
    candidates = await load_results(session, fused[: max(limit, rerank_candidates)])
    query: str = search["query"]
    try:
        scores = await reranker.score(
            query, [f"{chunk.heading}\n{chunk.content}" for _, chunk, _ in candidates]
        )
    except Exception:
        logger.exception("Re-ranking failed; keeping the fusion order")
        return Retrieval(candidates[:limit], reranked=False, rerank_failed=True)
    for (item, _, _), score in zip(candidates, scores, strict=True):
        item.rerank_score = score
    # Stable: equal scores keep their fusion order
    ordered = sorted(candidates, key=lambda r: -(r[0].rerank_score or 0.0))
    return Retrieval(ordered[:limit], reranked=True)
