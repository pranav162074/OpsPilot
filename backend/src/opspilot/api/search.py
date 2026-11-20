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
from opspilot.search.service import SearchMode, load_results, search_chunks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["search"])


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[str, Field(min_length=1, max_length=1000)]
    limit: Annotated[int, Field(ge=1, le=50)] = 10
    mode: SearchMode = SearchMode.HYBRID
    source_types: list[SourceType] | None = None


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


class SearchResponse(BaseModel):
    query: str
    mode: SearchMode
    # True when hybrid search fell back to keywords only because embeddings were unavailable
    degraded: bool
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

    fused = await search_chunks(
        session,
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
        )
        for item, chunk, document in await load_results(session, fused[: body.limit])
    ]
    return SearchResponse(query=body.query, mode=body.mode, degraded=degraded, results=results)
