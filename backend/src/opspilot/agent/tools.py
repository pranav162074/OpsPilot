"""The agent's tools. Built per request and bound to one workspace, so the model can only
ever see that workspace's documents, whatever it asks for."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opspilot.config import Settings
from opspilot.embeddings import Embedder, EmbeddingError, EmbeddingUnavailable
from opspilot.embeddings.cache import QueryEmbeddingCache
from opspilot.models import Chunk, Document, SourceType
from opspilot.search.service import SearchMode, load_results, search_chunks

# Long passages are cut, so a handful of results doesn't fill the model's context
MAX_PASSAGE_CHARS = 1500


@dataclass
class Source:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    heading: str
    content: str


@dataclass
class Retrieved:
    """Every passage the tools showed the model in this run: the only ones it may cite."""

    sources: dict[uuid.UUID, Source] = field(default_factory=dict)

    def add(self, chunk: Chunk, document: Document) -> None:
        self.sources[chunk.id] = Source(
            chunk.id, document.id, document.title, chunk.heading, chunk.content
        )


def _format(chunk: Chunk, document: Document) -> str:
    content = chunk.content
    if len(content) > MAX_PASSAGE_CHARS:
        content = content[:MAX_PASSAGE_CHARS] + " [...]"
    location = f"{document.title} > {chunk.heading}" if chunk.heading else document.title
    return f"[c:{chunk.id}] {location}\n{content}"


class SearchArgs(BaseModel):
    query: str = Field(description="What to look for, in plain words or exact error codes")
    source_types: list[SourceType] | None = Field(
        default=None, description="Optionally limit to runbook, postmortem, architecture or other"
    )


class ReadArgs(BaseModel):
    chunk_id: str = Field(description="A chunk id from a search result, without the 'c:' prefix")


def build_tools(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: Embedder,
    query_cache: QueryEmbeddingCache,
    workspace_id: uuid.UUID,
    retrieved: Retrieved,
    default_source_types: Sequence[SourceType] | None = None,
) -> list[BaseTool]:
    async def search_knowledge_base(
        query: str, source_types: list[SourceType] | None = None
    ) -> str:
        try:
            vector = await query_cache.embed_query(embedder, query)
        except (EmbeddingUnavailable, EmbeddingError):
            vector = None  # keyword search still works
        async with sessionmaker() as session:
            fused = await search_chunks(
                session,
                workspace_id=workspace_id,
                query=query,
                mode=SearchMode.HYBRID,
                query_vector=vector,
                model=embedder.model,
                source_types=source_types or default_source_types,
                candidates=settings.search_candidates,
                rrf_k=settings.rrf_k,
            )
            results = await load_results(session, fused[:5])
        if not results:
            return "No matching passages. Try different words."
        for _, chunk, document in results:
            retrieved.add(chunk, document)
        return "\n\n".join(_format(chunk, document) for _, chunk, document in results)

    async def read_section(chunk_id: str) -> str:
        try:
            wanted = uuid.UUID(chunk_id.removeprefix("c:"))
        except ValueError:
            return "That is not a chunk id."
        async with sessionmaker() as session:
            found = (
                await session.execute(
                    select(Chunk, Document)
                    .join(Document, Document.id == Chunk.document_id)
                    # Scoped to this workspace: ids from elsewhere simply don't exist
                    .where(Chunk.id == wanted, Chunk.workspace_id == workspace_id)
                )
            ).first()
            if found is None:
                return "No such passage."
            chunk, document = found
            rows = (
                await session.execute(
                    select(Chunk)
                    .where(
                        Chunk.document_id == chunk.document_id,
                        Chunk.ordinal.between(chunk.ordinal - 1, chunk.ordinal + 1),
                    )
                    .order_by(Chunk.ordinal)
                )
            ).scalars()
            neighbours = list(rows)
        for neighbour in neighbours:
            retrieved.add(neighbour, document)
        return "\n\n".join(_format(n, document) for n in neighbours)

    return [
        StructuredTool.from_function(
            coroutine=search_knowledge_base,
            name="search_knowledge_base",
            description="Search this workspace's runbooks, postmortems and architecture notes.",
            args_schema=SearchArgs,
        ),
        StructuredTool.from_function(
            coroutine=read_section,
            name="read_section",
            description="Read a passage from a search result together with the text around it.",
            args_schema=ReadArgs,
        ),
    ]
