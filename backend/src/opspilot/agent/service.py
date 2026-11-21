import asyncio
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opspilot.agent.graph import CITATION, Trace, build_graph, cited_ids, text_of
from opspilot.agent.tools import Retrieved, build_tools
from opspilot.config import Settings
from opspilot.embeddings import Embedder
from opspilot.embeddings.cache import QueryEmbeddingCache
from opspilot.models import SourceType

logger = logging.getLogger(__name__)


@dataclass
class Citation:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    heading: str


@dataclass
class InvestigationResult:
    answer: str
    citations: list[Citation]
    # Citations to passages the agent never retrieved (a hallucinated id), removed from the answer
    invalid_citations: int
    trace: Trace
    latency_ms: float
    revised: bool
    sources_considered: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)


async def run_investigation(
    *,
    llm: BaseChatModel,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: Embedder,
    query_cache: QueryEmbeddingCache,
    workspace_id: uuid.UUID,
    question: str,
    source_types: Sequence[SourceType] | None = None,
) -> InvestigationResult:
    started = time.perf_counter()
    retrieved = Retrieved()
    trace = Trace()
    tools = build_tools(
        sessionmaker=sessionmaker,
        settings=settings,
        embedder=embedder,
        query_cache=query_cache,
        workspace_id=workspace_id,
        retrieved=retrieved,
        default_source_types=source_types,
    )
    graph = build_graph(
        llm,
        tools,
        retrieved,
        trace,
        question=question,
        max_steps=settings.agent_max_steps,
        reflection=settings.agent_reflection,
    )
    async with asyncio.timeout(settings.agent_timeout_seconds):
        state = await graph.ainvoke(
            {"messages": [HumanMessage(question)], "steps": 0, "revisions": 0},
            # Each agent step is two graph steps (agent, tools), plus review and revision
            {"recursion_limit": settings.agent_max_steps * 4 + 10},
        )

    answer_message = next(
        m for m in reversed(state["messages"]) if isinstance(m, AIMessage) and not m.tool_calls
    )
    answer = text_of(answer_message).strip()

    # A citation is only trusted if the tools actually showed the model that passage
    invalid = [i for i in cited_ids(answer) if i not in retrieved.sources]
    if invalid:
        answer = CITATION.sub(
            lambda m: "" if uuid.UUID(m.group(1)) in invalid else m.group(0), answer
        ).strip()
    citations = [
        Citation(
            chunk_id=s.chunk_id,
            document_id=s.document_id,
            document_title=s.document_title,
            heading=s.heading,
        )
        for s in (retrieved.sources[i] for i in cited_ids(answer))
    ]
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    logger.info(
        "Investigation finished",
        extra={
            "fields": {
                "llm_calls": trace.llm_calls,
                "tool_calls": trace.tool_calls,
                "input_tokens": trace.input_tokens,
                "output_tokens": trace.output_tokens,
                "latency_ms": latency_ms,
                "citations": len(citations),
                "invalid_citations": len(invalid),
            }
        },
    )
    return InvestigationResult(
        answer=answer,
        citations=citations,
        invalid_citations=len(invalid),
        trace=trace,
        latency_ms=latency_ms,
        revised=state["revisions"] > 0,
        sources_considered=len(retrieved.sources),
        events=trace.events,
    )
