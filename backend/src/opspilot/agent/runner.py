"""Runs one stored investigation to completion: from the API (synchronously) or a worker.

The investigation row is created first, as "running". The runner claims it with a single
UPDATE (so a task delivered twice runs once), runs the agent, and writes the outcome back,
including to the incident that asked for it, if any.

Runs are resumable. A "plan" run stores its state after every stage (and that doubles as a
heartbeat). If its worker dies, recover_abandoned_investigations() notices the silence,
releases the claim and queues it again, and the next runner carries on from the last
finished stage, up to agent_max_attempts claims in all.
"""

import enum
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opspilot.agent.service import Checkpoint, InvestigationResult, run_investigation
from opspilot.config import Settings
from opspilot.embeddings import Embedder
from opspilot.embeddings.cache import QueryEmbeddingCache
from opspilot.models import (
    ActionStatus,
    Investigation,
    InvestigationAction,
    InvestigationStatus,
    SourceType,
)
from opspilot.models.incident import Incident, IncidentEvent
from opspilot.search.rerank import Reranker

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
        row.checkpoint = None  # finished either way: nothing left to resume
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
            row.hypotheses = result.hypotheses
            row.budget_exhausted = result.budget_exhausted
            row.cost_usd = result.cost_usd
            row.flagged_sources = result.flagged_sources
            for position, action in enumerate(result.actions):
                session.add(
                    InvestigationAction(
                        workspace_id=row.workspace_id,
                        investigation_id=row.id,
                        position=position,
                        status=ActionStatus.PROPOSED.value,
                        **action,
                    )
                )
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
    reranker: Reranker | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
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
                .values(
                    started_at=datetime.now(UTC),
                    heartbeat_at=datetime.now(UTC),
                    attempts=Investigation.attempts + 1,
                )
                .returning(
                    Investigation.workspace_id,
                    Investigation.question,
                    Investigation.source_types,
                    Investigation.strategy,
                    Investigation.checkpoint,
                    Investigation.attempts,
                )
            )
        ).one_or_none()
        await session.commit()
    if claimed is None:
        return RunOutcome.SKIPPED
    if claimed.checkpoint:
        logger.info(
            "Resuming investigation %s from %s (attempt %s)",
            investigation_id,
            claimed.checkpoint.get("state", {}).get("stage"),
            claimed.attempts,
        )

    async def save_checkpoint(checkpoint: Checkpoint) -> None:
        async with sessionmaker() as session:
            await session.execute(
                update(Investigation)
                .where(
                    Investigation.id == investigation_id,
                    Investigation.status == InvestigationStatus.RUNNING.value,
                )
                .values(checkpoint=checkpoint, heartbeat_at=datetime.now(UTC))
            )
            await session.commit()

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
            strategy=claimed.strategy,
            checkpoint=claimed.checkpoint,
            on_checkpoint=save_checkpoint,
            reranker=reranker,
            on_event=on_event,
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


@dataclass
class Recovery:
    # Released and to be queued again: they resume from their checkpoint
    resumed: list[uuid.UUID] = field(default_factory=list)
    # Out of attempts: marked failed
    failed: int = 0


async def recover_abandoned_investigations(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, now: datetime
) -> Recovery:
    """Investigations still "running" with no sign of life for 3× the timeout belong to a
    worker that died (or a task that was lost before any worker got it). Those with attempts
    left are released for another run; the caller queues them. The rest are failed.

    Each row is changed with a conditional UPDATE, so two schedulers running this at once
    can't both release (or fail) the same investigation."""
    cutoff = now - timedelta(seconds=settings.agent_timeout_seconds * 3)
    last_seen = func.coalesce(Investigation.heartbeat_at, Investigation.created_at)
    stale = (
        Investigation.status == InvestigationStatus.RUNNING.value,
        last_seen < cutoff,
    )
    recovery = Recovery()
    async with sessionmaker() as session:
        recovery.resumed = list(
            (
                await session.scalars(
                    update(Investigation)
                    .where(*stale, Investigation.attempts < settings.agent_max_attempts)
                    .values(started_at=None, heartbeat_at=now)
                    .returning(Investigation.id)
                )
            ).all()
        )
        failed = (
            await session.scalars(
                update(Investigation)
                .where(*stale, Investigation.attempts >= settings.agent_max_attempts)
                .values(
                    status=InvestigationStatus.FAILED.value,
                    error="The worker running this investigation stopped, and it ran out of "
                    "attempts",
                    completed_at=now,
                    checkpoint=None,
                )
                .returning(Investigation.id)
            )
        ).all()
        for investigation_id in failed:
            await _record_on_incident(session, investigation_id, "investigation_failed")
        for investigation_id in recovery.resumed:
            await _record_on_incident(session, investigation_id, "investigation_resumed")
        await session.commit()
    recovery.failed = len(failed)
    return recovery


async def mark_unrunnable(
    sessionmaker: async_sessionmaker[AsyncSession], investigation_id: uuid.UUID
) -> None:
    """The worker has no model configured: fail the investigation instead of leaving it running."""
    await _finish(
        sessionmaker, investigation_id, None, "No language model is configured on the worker"
    )
