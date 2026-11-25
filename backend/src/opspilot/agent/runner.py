"""Runs one stored investigation to completion: from the API (synchronously) or a worker.

The investigation row is created first, as "running". The runner claims it with a single
UPDATE (so a task delivered twice runs once), runs the agent, and writes the outcome back,
including to the incident that asked for it, if any.
"""

import enum
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from langchain_core.language_models import BaseChatModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opspilot.agent.service import InvestigationResult, run_investigation
from opspilot.config import Settings
from opspilot.embeddings import Embedder
from opspilot.embeddings.cache import QueryEmbeddingCache
from opspilot.models import Investigation, InvestigationStatus, SourceType
from opspilot.models.incident import Incident, IncidentEvent

logger = logging.getLogger(__name__)


class RunOutcome(enum.StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"  # already claimed by another runner, or not running


async def _record_on_incident(
    session: AsyncSession, investigation_id: uuid.UUID, type_: str
) -> None:
    incident_id = await session.scalar(
        select(Incident.id).where(Incident.investigation_id == investigation_id)
    )
    if incident_id is not None:
        session.add(
            IncidentEvent(
                incident_id=incident_id,
                type=type_,
                data={"investigation_id": str(investigation_id)},
            )
        )


async def _finish(
    sessionmaker: async_sessionmaker[AsyncSession],
    investigation_id: uuid.UUID,
    result: InvestigationResult | None,
    error: str | None,
) -> None:
    async with sessionmaker() as session:
        row = await session.get(Investigation, investigation_id)
        if row is None:
            return
        row.completed_at = datetime.now(UTC)
        if result is None:
            row.status = InvestigationStatus.FAILED.value
            row.error = error
            await _record_on_incident(session, investigation_id, "investigation_failed")
        else:
            row.status = InvestigationStatus.COMPLETED.value
            row.answer = result.answer
            row.citations = [
                {
                    "chunk_id": str(c.chunk_id),
                    "document_id": str(c.document_id) if c.document_id else None,
                    "kind": c.kind,
                    "document_title": c.document_title,
                    "heading": c.heading,
                }
                for c in result.citations
            ]
            row.trace = result.events
            row.invalid_citations = result.invalid_citations
            row.llm_calls = result.trace.llm_calls
            row.tool_calls = result.trace.tool_calls
            row.input_tokens = result.trace.input_tokens
            row.output_tokens = result.trace.output_tokens
            row.latency_ms = result.latency_ms
            await _record_on_incident(session, investigation_id, "investigation_completed")
        await session.commit()


async def execute_investigation(
    *,
    investigation_id: uuid.UUID,
    llm: BaseChatModel,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: Embedder,
    query_cache: QueryEmbeddingCache,
    telemetry_http: httpx.AsyncClient | None = None,
) -> RunOutcome:
    async with sessionmaker() as session:
        claimed = (
            await session.execute(
                update(Investigation)
                .where(
                    Investigation.id == investigation_id,
                    Investigation.status == InvestigationStatus.RUNNING.value,
                    Investigation.started_at.is_(None),
                )
                .values(started_at=datetime.now(UTC))
                .returning(
                    Investigation.workspace_id,
                    Investigation.question,
                    Investigation.source_types,
                )
            )
        ).one_or_none()
        await session.commit()
    if claimed is None:
        return RunOutcome.SKIPPED

    try:
        result = await run_investigation(
            llm=llm,
            sessionmaker=sessionmaker,
            settings=settings,
            embedder=embedder,
            query_cache=query_cache,
            workspace_id=claimed.workspace_id,
            question=claimed.question,
            source_types=[SourceType(s) for s in claimed.source_types or []] or None,
            telemetry_http=telemetry_http,
        )
    except TimeoutError:
        await _finish(
            sessionmaker, investigation_id, None, "The investigation took too long and was stopped"
        )
        return RunOutcome.TIMED_OUT
    except Exception as exc:
        logger.exception("Investigation %s failed", investigation_id)
        await _finish(
            sessionmaker, investigation_id, None, f"The agent failed: {type(exc).__name__}"
        )
        return RunOutcome.FAILED

    await _finish(sessionmaker, investigation_id, result, None)
    return RunOutcome.COMPLETED


async def fail_abandoned_investigations(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, now: datetime
) -> int:
    """Investigations still "running" long after their timeout belong to a worker that died."""
    cutoff = now - timedelta(seconds=settings.agent_timeout_seconds * 3)
    async with sessionmaker() as session:
        rows = (
            await session.scalars(
                update(Investigation)
                .where(
                    Investigation.status == InvestigationStatus.RUNNING.value,
                    Investigation.created_at < cutoff,
                )
                .values(
                    status=InvestigationStatus.FAILED.value,
                    error="The worker running this investigation stopped",
                    completed_at=now,
                )
                .returning(Investigation.id)
            )
        ).all()
        for investigation_id in rows:
            await _record_on_incident(session, investigation_id, "investigation_failed")
        await session.commit()
    return len(rows)


async def mark_unrunnable(
    sessionmaker: async_sessionmaker[AsyncSession], investigation_id: uuid.UUID
) -> None:
    """The worker has no model configured: fail the investigation instead of leaving it running."""
    await _finish(
        sessionmaker, investigation_id, None, "No language model is configured on the worker"
    )
