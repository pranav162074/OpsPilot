"""The dead-letter queue, for admins: what failed for good, why, and sending it again."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update

from opspilot import dead_letters
from opspilot.api.deps import Admin, SessionDep
from opspilot.api.errors import ApiError
from opspilot.models import DeadLetter, DeadLetterStatus, Document, DocumentStatus
from opspilot.storage import StorageError

router = APIRouter(prefix="/v1/dead-letters", tags=["dead letters"])


class DeadLetterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    task: str
    job_key: str
    payload: dict[str, Any]
    error: str
    attempts: int
    failures: int
    status: DeadLetterStatus
    created_at: datetime
    last_failed_at: datetime
    resolved_at: datetime | None


async def _load(session: SessionDep, principal: Admin, dead_letter_id: uuid.UUID) -> DeadLetter:
    row = await session.scalar(
        select(DeadLetter).where(
            DeadLetter.id == dead_letter_id, DeadLetter.workspace_id == principal.workspace.id
        )
    )
    if row is None:
        raise ApiError(404, "not_found", "Dead letter not found")
    return row


async def _close(
    session: SessionDep, principal: Admin, row: DeadLetter, to: DeadLetterStatus
) -> DeadLetter:
    """Conditional: two admins retrying at once send it once."""
    closed = await session.scalar(
        update(DeadLetter)
        .where(DeadLetter.id == row.id, DeadLetter.status == DeadLetterStatus.DEAD.value)
        .values(
            status=to.value, resolved_at=datetime.now(UTC), resolved_by_key_id=principal.api_key.id
        )
        .returning(DeadLetter)
        .execution_options(populate_existing=True)
    )
    if closed is None:
        raise ApiError(409, "invalid_state", f"This dead letter is already {row.status}")
    return closed


@router.get("")
async def list_dead_letters(
    principal: Admin,
    session: SessionDep,
    status_filter: Annotated[
        DeadLetterStatus | None, Query(alias="status")
    ] = DeadLetterStatus.DEAD,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DeadLetterOut]:
    query = select(DeadLetter).where(DeadLetter.workspace_id == principal.workspace.id)
    if status_filter is not None:
        query = query.where(DeadLetter.status == status_filter.value)
    rows = await session.scalars(query.order_by(DeadLetter.last_failed_at.desc()).limit(limit))
    return [DeadLetterOut.model_validate(r) for r in rows]


@router.post("/{dead_letter_id}/retry")
async def retry_dead_letter(
    dead_letter_id: uuid.UUID, request: Request, principal: Admin, session: SessionDep
) -> DeadLetterOut:
    """Sends the job again (once whatever made it fail is fixed). If it fails again, it comes
    back here."""
    row = await _load(session, principal, dead_letter_id)
    payload = row.payload
    if row.task == dead_letters.INGESTION:
        document_id = uuid.UUID(payload["document_id"])
        # A fresh start: its attempts were used up
        reset = await session.scalar(
            update(Document)
            .where(Document.id == document_id, Document.workspace_id == principal.workspace.id)
            .values(
                status=DocumentStatus.PENDING.value,
                attempts=0,
                error=None,
                updated_at=datetime.now(UTC),
            )
            .returning(Document.id)
        )
        if reset is None:
            closed = await _close(session, principal, row, DeadLetterStatus.DISCARDED)
            await session.commit()
            raise ApiError(410, "gone", "The document was deleted; discarded the dead letter")
        closed = await _close(session, principal, row, DeadLetterStatus.RETRIED)
        await session.commit()
        await run_in_threadpool(request.app.state.enqueue_ingestion, document_id)
    elif row.task == dead_letters.SOURCE_SYNC:
        closed = await _close(session, principal, row, DeadLetterStatus.RETRIED)
        await session.commit()
        await run_in_threadpool(
            request.app.state.enqueue_source_sync,
            uuid.UUID(payload["source_id"]),
            bool(payload.get("force")),
        )
    elif row.task == dead_letters.DELETE_OBJECT:
        # Small enough to do now: if it fails, the dead letter stays
        try:
            await request.app.state.store.delete(payload["key"])
        except StorageError as exc:
            raise ApiError(503, "storage_unavailable", f"Still failing: {exc}") from exc
        closed = await _close(session, principal, row, DeadLetterStatus.RETRIED)
        await session.commit()
    else:
        raise ApiError(422, "not_retryable", f"Don't know how to retry {row.task}")
    return DeadLetterOut.model_validate(closed)


@router.post("/{dead_letter_id}/discard")
async def discard_dead_letter(
    dead_letter_id: uuid.UUID, principal: Admin, session: SessionDep
) -> DeadLetterOut:
    row = await _load(session, principal, dead_letter_id)
    closed = await _close(session, principal, row, DeadLetterStatus.DISCARDED)
    await session.commit()
    return DeadLetterOut.model_validate(closed)
