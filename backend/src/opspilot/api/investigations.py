import logging
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from opspilot.agent.runner import RunOutcome, execute_investigation
from opspilot.api.deps import RateLimiterDep, Responder, SessionDep, SettingsDep, Viewer, enforce
from opspilot.api.errors import ApiError
from opspilot.models import Investigation, InvestigationStatus, SourceType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/investigations", tags=["investigations"])


class InvestigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: Annotated[str, Field(min_length=3, max_length=2000)]
    source_types: list[SourceType] | None = None
    # Return at once and let the worker run it, instead of waiting for the answer
    background: bool = False


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


class InvestigationOut(BaseModel):
    id: uuid.UUID
    question: str
    status: InvestigationStatus
    answer: str | None
    citations: list[CitationOut]
    invalid_citations: int
    error: str | None
    model: str | None
    usage: Usage
    created_at: datetime
    completed_at: datetime | None


class InvestigationDetail(InvestigationOut):
    # Every model call and tool call, in order: how the agent reached its answer
    trace: list[dict[str, Any]]


def _fields(row: Investigation) -> dict[str, Any]:
    return {
        "id": row.id,
        "question": row.question,
        "status": InvestigationStatus(row.status),
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
        ),
        "created_at": row.created_at,
        "completed_at": row.completed_at,
    }


def _out(row: Investigation) -> InvestigationOut:
    return InvestigationOut(**_fields(row))


def _detail(row: Investigation) -> InvestigationDetail:
    return InvestigationDetail(**_fields(row), trace=row.trace)


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
    GET /v1/investigations/{id} until the status isn't "running"."""
    llm = request.app.state.llm
    if llm is None:
        raise ApiError(
            503, "agent_unavailable", "Investigations need a language model; set GEMINI_API_KEY"
        )
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
        model=settings.llm_model,
        citations=[],
        trace=[],
    )
    session.add(row)
    await session.commit()

    if body.background:
        request.app.state.enqueue_investigation(row.id)
        response.status_code = status.HTTP_202_ACCEPTED
        return _detail(row)

    outcome = await execute_investigation(
        investigation_id=row.id,
        llm=llm,
        sessionmaker=request.app.state.sessionmaker,
        settings=settings,
        embedder=request.app.state.embedder,
        query_cache=request.app.state.query_cache,
        telemetry_http=request.app.state.telemetry_http,
    )
    if outcome == RunOutcome.TIMED_OUT:
        raise ApiError(504, "agent_timeout", "The investigation took too long")
    if outcome == RunOutcome.FAILED:
        raise ApiError(502, "agent_failed", "The language model call failed")
    await session.refresh(row)
    return _detail(row)


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
