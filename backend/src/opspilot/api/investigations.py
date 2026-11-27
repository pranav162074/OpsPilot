import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from opspilot.agent.runner import RunOutcome, execute_investigation
from opspilot.api.deps import RateLimiterDep, Responder, SessionDep, SettingsDep, Viewer, enforce
from opspilot.api.errors import ApiError
from opspilot.config import Settings
from opspilot.models import Investigation, InvestigationStatus, SourceType
from opspilot.usage import ensure_within_budget

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/investigations", tags=["investigations"])


class InvestigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: Annotated[str, Field(min_length=3, max_length=2000)]
    source_types: list[SourceType] | None = None
    # Return at once and let the worker run it, instead of waiting for the answer
    background: bool = False
    # "react" or "plan" (see Settings.agent_strategy); the server's default if left out
    strategy: Literal["react", "plan"] | None = None


class CitationOut(BaseModel):
    # For telemetry observations, chunk_id identifies the observation and document_id is null
    chunk_id: uuid.UUID
    document_id: uuid.UUID | None
    document_title: str
    heading: str
    # "document", or "metrics", "logs" or "changes" for telemetry the agent looked at
    kind: str = "document"


class Usage(BaseModel):
    llm_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    latency_ms: float | None
    # At the configured list prices (LLM_INPUT_USD_PER_MTOK / LLM_OUTPUT_USD_PER_MTOK)
    cost_usd: Decimal | None


class Hypothesis(BaseModel):
    statement: str
    likelihood: str
    evidence: list[str]
    check: dict[str, Any] | None
    verdict: str | None
    reason: str | None


class InvestigationOut(BaseModel):
    id: uuid.UUID
    question: str
    status: InvestigationStatus
    strategy: str
    answer: str | None
    citations: list[CitationOut]
    invalid_citations: int
    error: str | None
    model: str | None
    usage: Usage
    # The run hit the token budget and answered with the evidence it had by then
    budget_exhausted: bool
    # Sources it saw that look like prompt injection (see the README)
    flagged_sources: int
    # How many times a runner picked it up (more than 1: it was resumed after a crash)
    attempts: int
    created_at: datetime
    completed_at: datetime | None


class InvestigationDetail(InvestigationOut):
    # Every model call and tool call, in order: how the agent reached its answer
    trace: list[dict[str, Any]]
    # "plan" runs: each cause considered, how it was checked, and the verdict
    hypotheses: list[Hypothesis]
    # Where a running "plan" investigation has got to
    stage: str | None


def _fields(row: Investigation) -> dict[str, Any]:
    return {
        "id": row.id,
        "question": row.question,
        "status": InvestigationStatus(row.status),
        "strategy": row.strategy,
        "answer": row.answer,
        "citations": row.citations,
        "invalid_citations": row.invalid_citations,
        "error": row.error,
        "model": row.model,
        "usage": Usage(
            llm_calls=row.llm_calls,
            tool_calls=row.tool_calls,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            latency_ms=row.latency_ms,
            cost_usd=row.cost_usd,
        ),
        "budget_exhausted": row.budget_exhausted,
        "flagged_sources": row.flagged_sources,
        "attempts": row.attempts,
        "created_at": row.created_at,
        "completed_at": row.completed_at,
    }


def _out(row: Investigation) -> InvestigationOut:
    return InvestigationOut(**_fields(row))


def _detail(row: Investigation) -> InvestigationDetail:
    hypotheses = [
        Hypothesis(**{k: h.get(k) for k in Hypothesis.model_fields}) for h in row.hypotheses or []
    ]
    stage = (row.checkpoint or {}).get("state", {}).get("stage") if row.checkpoint else None
    return InvestigationDetail(**_fields(row), trace=row.trace, hypotheses=hypotheses, stage=stage)


async def _new_investigation(
    body: "InvestigationRequest",
    request: Request,
    response: Response,
    principal: Responder,
    session: SessionDep,
    settings: SettingsDep,
    limiter: RateLimiterDep,
) -> Investigation:
    """The checks every way of asking runs (a model, the budget, the rate limit), then the
    row, committed as "running"."""
    if request.app.state.llm is None:
        raise ApiError(
            503, "agent_unavailable", "Investigations need a language model; set GEMINI_API_KEY"
        )
    await ensure_within_budget(session, principal.workspace)
    # Model calls cost money: a separate, lower limit per workspace
    enforce(
        await limiter.hit(
            f"investigations:{principal.workspace.id}", settings.investigations_per_hour, 3600
        ),
        response,
    )
    row = Investigation(
        workspace_id=principal.workspace.id,
        created_by_key_id=principal.api_key.id,
        question=body.question,
        source_types=[s.value for s in body.source_types] if body.source_types else None,
        status=InvestigationStatus.RUNNING.value,
        strategy=body.strategy or settings.agent_strategy,
        model=settings.llm_model,
        citations=[],
        trace=[],
        hypotheses=[],
        attempts=0,
        budget_exhausted=False,
        flagged_sources=0,
    )
    session.add(row)
    await session.commit()
    return row


def _run(request: Request, settings: Settings, row: Investigation, **extra: Any) -> Any:
    return execute_investigation(
        investigation_id=row.id,
        llm=request.app.state.llm,
        sessionmaker=request.app.state.sessionmaker,
        settings=settings,
        embedder=request.app.state.embedder,
        query_cache=request.app.state.query_cache,
        telemetry_http=request.app.state.telemetry_http,
        reranker=request.app.state.reranker,
        **extra,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_investigation(
    body: InvestigationRequest,
    request: Request,
    response: Response,
    principal: Responder,
    session: SessionDep,
    settings: SettingsDep,
    limiter: RateLimiterDep,
) -> InvestigationDetail:
    """Asks the agent a question. It searches the workspace's documents, answers with
    citations, and has the answer reviewed against its sources before returning it.

    With "background": true it returns 202 at once and the worker runs it; poll
    GET /v1/investigations/{id} until the status isn't "running". To watch it work instead,
    use POST /v1/investigations/stream."""
    row = await _new_investigation(body, request, response, principal, session, settings, limiter)
    if body.background:
        request.app.state.enqueue_investigation(row.id)
        response.status_code = status.HTTP_202_ACCEPTED
        return _detail(row)

    outcome = await _run(request, settings, row)
    if outcome == RunOutcome.TIMED_OUT:
        raise ApiError(504, "agent_timeout", "The investigation took too long")
    if outcome == RunOutcome.FAILED:
        raise ApiError(502, "agent_failed", "The language model call failed")
    await session.refresh(row)
    return _detail(row)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str, separators=(',', ':'))}\n\n"


@router.post("/stream")
async def stream_investigation(
    body: InvestigationRequest,
    request: Request,
    response: Response,
    principal: Responder,
    session: SessionDep,
    settings: SettingsDep,
    limiter: RateLimiterDep,
) -> StreamingResponse:
    """The same investigation, as Server-Sent Events while it runs:

        event: investigation   {"id": ...}               first
        event: step            a model or tool call       as each one finishes
        event: done            the whole investigation    last, if it completed
        event: error           {"code", "message", ...}   last, if it didn't

    plus a comment line every few seconds so proxies don't close an idle connection. The run
    belongs to the server, not the connection: if the client goes away it still finishes and
    is saved, and GET /v1/investigations/{id} has it."""
    row = await _new_investigation(body, request, response, principal, session, settings, limiter)
    investigation_id = row.id
    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(_run(request, settings, row, on_event=events.put_nowait))
    # Held until done: a task nothing references can be garbage-collected mid-run
    running: set[asyncio.Task[Any]] = request.app.state.running_investigations
    running.add(task)
    task.add_done_callback(running.discard)
    sessionmaker = request.app.state.sessionmaker
    heartbeat = settings.sse_heartbeat_seconds

    async def stream() -> AsyncIterator[str]:
        yield _sse("investigation", {"id": str(investigation_id)})
        while True:
            next_event = asyncio.ensure_future(events.get())
            done, _ = await asyncio.wait(
                {next_event, task}, timeout=heartbeat, return_when=asyncio.FIRST_COMPLETED
            )
            if next_event in done:
                yield _sse("step", next_event.result())
                continue
            next_event.cancel()
            if not task.done():
                yield ": keep-alive\n\n"
                continue
            while not events.empty():  # steps that landed together with the end
                yield _sse("step", events.get_nowait())
            break
        outcome = task.result() if not task.cancelled() else RunOutcome.FAILED
        async with sessionmaker() as fresh:
            final = await fresh.get(Investigation, investigation_id)
            detail = _detail(final).model_dump(mode="json") if final else None
        if outcome == RunOutcome.COMPLETED and detail is not None:
            yield _sse("done", detail)
        else:
            code = "agent_timeout" if outcome == RunOutcome.TIMED_OUT else "agent_failed"
            message = (detail or {}).get("error") or "The investigation failed"
            yield _sse("error", {"code": code, "message": message, "investigation": detail})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("")
async def list_investigations(
    principal: Viewer,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[InvestigationOut]:
    rows = await session.scalars(
        select(Investigation)
        .where(Investigation.workspace_id == principal.workspace.id)
        .order_by(Investigation.created_at.desc(), Investigation.id.desc())
        .limit(limit)
    )
    return [_out(row) for row in rows]


@router.get("/{investigation_id}")
async def get_investigation(
    investigation_id: uuid.UUID, principal: Viewer, session: SessionDep
) -> InvestigationDetail:
    row = await session.scalar(
        select(Investigation).where(
            Investigation.id == investigation_id,
            Investigation.workspace_id == principal.workspace.id,
        )
    )
    if row is None:
        raise ApiError(404, "not_found", "Investigation not found")
    return _detail(row)
