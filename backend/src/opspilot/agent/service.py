import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opspilot.agent.graph import CITATION, Trace, build_graph, cited_ids, text_of
from opspilot.agent.planner import PlanState, build_plan_graph, initial_state, split_actions
from opspilot.agent.tools import Retrieved, build_telemetry_tools, build_tools
from opspilot.config import Settings
from opspilot.embeddings import Embedder
from opspilot.embeddings.cache import QueryEmbeddingCache
from opspilot.models import SourceType
from opspilot.telemetry.service import load_telemetry

logger = logging.getLogger(__name__)


@dataclass
class Citation:
    chunk_id: uuid.UUID
    document_id: uuid.UUID | None
    document_title: str
    heading: str
    kind: str = "document"


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
    strategy: str = "react"
    # "plan" runs: each cause considered, the check run for it and the verdict
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    # Changes the agent suggests making; a person approves or rejects each one
    actions: list[dict[str, Any]] = field(default_factory=list)
    budget_exhausted: bool = False
    resumed_from: str | None = None


# What a "plan" run stores after each stage, to resume from if its worker dies
Checkpoint = dict[str, Any]


def make_checkpoint(state: PlanState, retrieved: Retrieved, trace: Trace) -> Checkpoint:
    return {"state": dict(state), "sources": retrieved.to_json(), "trace": trace.to_json()}


def _clean_citations(text: str, retrieved: Retrieved) -> tuple[str, list[uuid.UUID]]:
    """Drops citations of anything the tools never showed the model (a hallucinated id)."""
    invalid = [i for i in cited_ids(text) if i not in retrieved.sources]
    if invalid:
        text = CITATION.sub(
            lambda m: "" if uuid.UUID(m.group(1)) in invalid else m.group(0), text
        ).strip()
    return text, invalid


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
    telemetry_http: httpx.AsyncClient | None = None,
    strategy: str = "react",
    checkpoint: Checkpoint | None = None,
    on_checkpoint: Callable[[Checkpoint], Awaitable[None]] | None = None,
) -> InvestigationResult:
    started = time.perf_counter()
    resume = checkpoint if strategy == "plan" and checkpoint else None
    retrieved = Retrieved.from_json(resume["sources"]) if resume else Retrieved()
    trace = Trace.from_json(resume["trace"]) if resume else Trace()
    tools = build_tools(
        sessionmaker=sessionmaker,
        settings=settings,
        embedder=embedder,
        query_cache=query_cache,
        workspace_id=workspace_id,
        retrieved=retrieved,
        default_source_types=source_types,
    )
    if telemetry_http is not None:
        async with sessionmaker() as session:
            telemetry = await load_telemetry(session, workspace_id, telemetry_http, settings)
        tools += build_telemetry_tools(
            sessionmaker=sessionmaker,
            telemetry=telemetry,
            workspace_id=workspace_id,
            retrieved=retrieved,
        )
    hypotheses: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    if strategy == "plan":

        async def save(state: PlanState) -> None:
            if on_checkpoint is not None:
                await on_checkpoint(make_checkpoint(state, retrieved, trace))

        plan_graph = build_plan_graph(
            llm,
            tools,
            retrieved,
            trace,
            max_parallel=settings.agent_max_parallel_tools,
            tool_timeout=settings.agent_tool_timeout_seconds,
            max_hypotheses=settings.agent_max_hypotheses,
            token_budget=settings.agent_token_budget,
            reflection=settings.agent_reflection,
            on_checkpoint=save,
        )
        start_state: PlanState = resume["state"] if resume else initial_state(question)
        async with asyncio.timeout(settings.agent_timeout_seconds):
            final: PlanState = await plan_graph.ainvoke(start_state, {"recursion_limit": 20})
        answer, actions = split_actions(final["draft"])
        hypotheses = final["hypotheses"]
        revised = final["revisions"] > 0
        budget_exhausted = final["budget_exhausted"]
    else:
        graph = build_graph(
            llm,
            tools,
            retrieved,
            trace,
            question=question,
            max_steps=settings.agent_max_steps,
            reflection=settings.agent_reflection,
            token_budget=settings.agent_token_budget,
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
        revised = state["revisions"] > 0
        budget_exhausted = 0 < settings.agent_token_budget <= trace.tokens

    # A citation is only trusted if the tools actually showed the model that passage
    answer, invalid = _clean_citations(answer, retrieved)
    for action in actions:
        action["citations"] = [c for c in action["citations"] if _is_known(c, retrieved)]
    citations = [
        Citation(
            chunk_id=s.chunk_id,
            document_id=s.document_id,
            document_title=s.document_title,
            heading=s.heading,
            kind=s.kind,
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
                "strategy": strategy,
                "budget_exhausted": budget_exhausted,
                "resumed_from": resume["state"]["stage"] if resume else None,
            }
        },
    )
    return InvestigationResult(
        answer=answer,
        citations=citations,
        invalid_citations=len(invalid),
        trace=trace,
        latency_ms=latency_ms,
        revised=revised,
        sources_considered=len(retrieved.sources),
        events=trace.events,
        strategy=strategy,
        hypotheses=hypotheses,
        actions=actions,
        budget_exhausted=budget_exhausted,
        resumed_from=resume["state"]["stage"] if resume else None,
    )


def _is_known(citation: str, retrieved: Retrieved) -> bool:
    try:
        return uuid.UUID(citation.removeprefix("c:")) in retrieved.sources
    except ValueError:
        return False
