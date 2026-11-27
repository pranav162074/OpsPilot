import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from opspilot.api.deps import SessionDep, SettingsDep, Viewer
from opspilot.api.errors import ApiError
from opspilot.embeddings import Embedder, EmbeddingError, EmbeddingUnavailable
from opspilot.embeddings.cache import QueryEmbeddingCache
from opspilot.models import SourceType
from opspilot.search.service import SearchMode, retrieve

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["search"])


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[str, Field(min_length=1, max_length=1000)]
    limit: Annotated[int, Field(ge=1, le=50)] = 10
    mode: SearchMode = SearchMode.HYBRID
    source_types: list[SourceType] | None = None
    # Re-order the top candidates with the cross-encoder, if the server has one
    rerank: bool = True


class Ranks(BaseModel):
    vector: int | None = None
    keyword: int | None = None


class SearchResult(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    source_type: SourceType
    heading: str
    content: str
    char_start: int
    char_end: int
    score: float
    ranks: Ranks
    # The cross-encoder's relevance score, when the results were re-ranked
    rerank_score: float | None
    # Prompt-injection heuristics the passage trips (see the README); [] if none
    flags: list[str]


class SearchResponse(BaseModel):
    query: str
    mode: SearchMode
    # True when hybrid search fell back to keywords only because embeddings were unavailable,
    # or re-ranking failed and the results are in fusion order
    degraded: bool
    reranked: bool
    results: list[SearchResult]


@router.post("/search")
async def search(
    body: SearchRequest,
    request: Request,
    principal: Viewer,
    session: SessionDep,
    settings: SettingsDep,
) -> SearchResponse:
    """Finds the passages in this workspace's documents that best match the query."""
    embedder: Embedder = request.app.state.embedder
    cache: QueryEmbeddingCache = request.app.state.query_cache
    query_vector: list[float] | None = None
    degraded = False

    if body.mode in (SearchMode.HYBRID, SearchMode.VECTOR):
        try:
            query_vector = await cache.embed_query(embedder, body.query)
        except (EmbeddingUnavailable, EmbeddingError) as exc:
            if body.mode == SearchMode.VECTOR:
                raise ApiError(
                    503, "search_unavailable", "Semantic search is unavailable right now"
                ) from exc
            # Keyword results beat an error: answer with those and say so
            logger.warning("Embedding the query failed; falling back to keyword search: %s", exc)
            degraded = True

    found = await retrieve(
        session,
        reranker=request.app.state.reranker if body.rerank else None,
        rerank_candidates=settings.rerank_candidates,
        limit=body.limit,
        workspace_id=principal.workspace.id,
        query=body.query,
        mode=body.mode,
        query_vector=query_vector,
        model=embedder.model,
        source_types=body.source_types,
        candidates=settings.search_candidates,
        rrf_k=settings.rrf_k,
    )
    results = [
        SearchResult(
            chunk_id=chunk.id,
            document_id=document.id,
            document_title=document.title,
            source_type=SourceType(document.source_type),
            heading=chunk.heading,
            content=chunk.content,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            score=round(item.score, 6),
            ranks=Ranks(vector=item.ranks.get("vector"), keyword=item.ranks.get("keyword")),
            rerank_score=None if item.rerank_score is None else round(item.rerank_score, 4),
            flags=list(chunk.flags or []),
        )
        for item, chunk, document in found.results
    ]
    return SearchResponse(
        query=body.query,
        mode=body.mode,
        degraded=degraded or found.rerank_failed,
        reranked=found.reranked,
        results=results,
    )
