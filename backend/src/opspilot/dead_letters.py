"""The dead-letter queue: background jobs that failed for good, kept for a person.

Retries handle what time fixes (a rate limit, a restart). What's left after them, or what
fails in a way retrying can't fix (a revoked GitHub token, an embedding model that rejects the
text), lands here with the job's arguments and error, instead of in a log line nobody reads.
From here an admin retries it (once the cause is fixed) or discards it.

One open dead letter per job: the same job failing again updates it (and counts the failure),
so a broken source synced every 30 minutes is one row, not fifty.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opspilot.models import DeadLetter

logger = logging.getLogger(__name__)

# Tasks a dead letter can be retried as. Each maps to how it's re-queued (see api/dead_letters)
INGESTION = "ingest_document"
SOURCE_SYNC = "sync_source"
DELETE_OBJECT = "delete_object"

MAX_ERROR_CHARS = 4000


async def record(
    session: AsyncSession,
    *,
    task: str,
    job_key: str,
    payload: dict[str, Any],
    error: str,
    attempts: int = 1,
    workspace_id: uuid.UUID | None = None,
) -> None:
    """Adds the job to the queue, or counts another failure if it's already there. Joins the
    caller's transaction; the caller commits."""
    now = datetime.now(UTC)
    statement = insert(DeadLetter).values(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        task=task,
        job_key=job_key,
        payload=payload,
        error=error[:MAX_ERROR_CHARS],
        attempts=attempts,
        failures=1,
        last_failed_at=now,
        status="dead",
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["task", "job_key"],
            index_where=DeadLetter.status == "dead",
            set_={
                "error": statement.excluded.error,
                "attempts": func.greatest(DeadLetter.attempts, statement.excluded.attempts),
                "failures": DeadLetter.failures + 1,
                "last_failed_at": now,
                "payload": statement.excluded.payload,
            },
        )
    )
    logger.error("Dead letter: %s %s failed for good: %s", task, job_key, error[:200])


async def record_now(sessionmaker: async_sessionmaker[AsyncSession], **kwargs: Any) -> None:
    """The same, in its own transaction (for callers with no session of their own)."""
    async with sessionmaker() as session:
        await record(session, **kwargs)
        await session.commit()
