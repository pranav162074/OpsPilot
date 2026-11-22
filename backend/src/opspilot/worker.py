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
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from opspilot.agent.llm import build_chat_model
from opspilot.agent.runner import (
    RunOutcome,
    execute_investigation,
    fail_abandoned_investigations,
    mark_unrunnable,
)
from opspilot.config import Settings, get_settings
from opspilot.embeddings import EmbeddingUnavailable, build_embedder
from opspilot.embeddings.cache import QueryEmbeddingCache
from opspilot.ingestion.service import (
    TRANSIENT_ERRORS,
    Outcome,
    find_stuck_documents,
    ingest_document,
)
from opspilot.logging import configure_logging, request_id_var

logger = logging.getLogger(__name__)

INGEST_TASK = "opspilot.ingest_document"
REQUEUE_TASK = "opspilot.requeue_stuck_documents"
INVESTIGATION_TASK = "opspilot.run_investigation"
ABANDONED_TASK = "opspilot.fail_abandoned_investigations"

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
        "fail-abandoned-investigations": {"task": ABANDONED_TASK, "schedule": 300.0},
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

        async def job(
            sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
        ) -> Outcome:
            embedder = build_embedder(settings)
            try:
                return await ingest_document(
                    sessionmaker, settings, uuid.UUID(document_id), embedder
                )
            finally:
                await embedder.aclose()

        return _run(job).value
    except TRANSIENT_ERRORS as exc:
        # Back off 5s, 10s, 20s, 40s, 60s, or as long as a rate-limited API asks
        countdown = min(60, 5 * 2**self.request.retries)
        if isinstance(exc, EmbeddingUnavailable) and exc.retry_after:
            countdown = max(countdown, min(300, int(exc.retry_after)))
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


@celery_app.task(name=INVESTIGATION_TASK)
def run_investigation_task(investigation_id: str, request_id: str = "-") -> str:
    """Runs a stored investigation. Not retried: a failed model call costs money, and the
    failure is recorded on the investigation for someone to look at and start again."""
    token = request_id_var.set(request_id)
    try:

        async def job(
            sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
        ) -> RunOutcome:
            llm = build_chat_model(settings)
            embedder = build_embedder(settings)
            redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2)
            try:
                if llm is None:
                    await mark_unrunnable(sessionmaker, uuid.UUID(investigation_id))
                    return RunOutcome.FAILED
                return await execute_investigation(
                    investigation_id=uuid.UUID(investigation_id),
                    llm=llm,
                    sessionmaker=sessionmaker,
                    settings=settings,
                    embedder=embedder,
                    query_cache=QueryEmbeddingCache(redis, settings.query_embedding_cache_seconds),
                )
            finally:
                await embedder.aclose()
                await redis.aclose()

        return _run(job).value
    finally:
        request_id_var.reset(token)


def enqueue_investigation(investigation_id: uuid.UUID) -> None:
    run_investigation_task.apply_async(
        args=(str(investigation_id),), kwargs={"request_id": request_id_var.get()}
    )


@celery_app.task(name=ABANDONED_TASK)
def fail_abandoned_investigations_task() -> int:
    async def job(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> int:
        return await fail_abandoned_investigations(sessionmaker, settings, datetime.now(UTC))

    count = _run(job)
    if count:
        logger.warning("Marked %s abandoned investigations as failed", count)
    return count
