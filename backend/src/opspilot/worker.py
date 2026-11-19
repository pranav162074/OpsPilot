"""Celery worker: runs ingestion outside the request, so uploads return straight away.

Run it with:  celery -A opspilot.worker worker --loglevel=info   (add --pool=solo on Windows)
and the scheduler that re-sends lost tasks with:  celery -A opspilot.worker beat
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from celery import Celery, Task
from celery.signals import setup_logging
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from opspilot.config import Settings, get_settings
from opspilot.ingestion.service import (
    TRANSIENT_ERRORS,
    find_stuck_documents,
    ingest_document,
)
from opspilot.logging import configure_logging, request_id_var

logger = logging.getLogger(__name__)

INGEST_TASK = "opspilot.ingest_document"
REQUEUE_TASK = "opspilot.requeue_stuck_documents"

celery_app = Celery("opspilot", broker=get_settings().celery_broker_url)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    # Acknowledge a task only after it finishes, so a worker that dies mid-task doesn't
    # lose it: the broker hands it to another worker. Ingestion is safe to repeat.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Take one task at a time, so long documents don't hold others hostage on one worker
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    # Don't hang an upload for long if Redis is down; the requeue job catches up later
    broker_connection_timeout=2,
    task_publish_retry_policy={"max_retries": 2, "interval_start": 0.1, "interval_step": 0.2},
    beat_schedule={
        "requeue-stuck-documents": {"task": REQUEUE_TASK, "schedule": 60.0},
    },
)


@setup_logging.connect
def _logging(**_: Any) -> None:
    # Same JSON logs as the API, instead of Celery's own format
    configure_logging(get_settings().log_level)


def _run[T](job: Callable[[async_sessionmaker[AsyncSession], Settings], Awaitable[T]]) -> T:
    """Runs async database code from a (synchronous) Celery task."""

    async def main() -> T:
        settings = get_settings()
        # NullPool: each task gets its own event loop, and pooled connections can't be
        # shared between loops
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            return await job(async_sessionmaker(engine, expire_on_commit=False), settings)
        finally:
            await engine.dispose()

    return asyncio.run(main())


@celery_app.task(name=INGEST_TASK, bind=True, max_retries=5)
def ingest_document_task(self: Task[Any, Any], document_id: str, request_id: str = "-") -> str:
    # Log lines from the worker carry the ID of the upload request that caused them
    token = request_id_var.set(request_id)
    try:
        outcome = _run(
            lambda sessionmaker, settings: ingest_document(
                sessionmaker, settings, uuid.UUID(document_id)
            )
        )
        return outcome.value
    except TRANSIENT_ERRORS as exc:
        # Back off 5s, 10s, 20s, 40s, 60s
        countdown = min(60, 5 * 2**self.request.retries)
        logger.warning("Ingestion of %s failed, retrying in %ss", document_id, countdown)
        raise self.retry(exc=exc, countdown=countdown) from exc
    finally:
        request_id_var.reset(token)


def enqueue_ingestion(document_id: uuid.UUID) -> None:
    ingest_document_task.apply_async(
        args=(str(document_id),), kwargs={"request_id": request_id_var.get()}
    )


@celery_app.task(name=REQUEUE_TASK)
def requeue_stuck_documents_task() -> int:
    async def find(
        sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
    ) -> list[uuid.UUID]:
        async with sessionmaker() as session:
            return await find_stuck_documents(session, settings, datetime.now(UTC))

    stuck = _run(find)
    for document_id in stuck:
        enqueue_ingestion(document_id)
    if stuck:
        logger.warning("Re-sent %s documents whose ingestion task was lost", len(stuck))
    return len(stuck)
