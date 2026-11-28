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

import httpx
from celery import Celery, Task
from celery.signals import setup_logging
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from opspilot import dead_letters
from opspilot.agent.llm import build_chat_model
from opspilot.agent.runner import (
    Recovery,
    RunOutcome,
    execute_investigation,
    mark_unrunnable,
    recover_abandoned_investigations,
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
from opspilot.models import Source
from opspilot.search.rerank import build_reranker
from opspilot.sources.sync import SyncResult, SyncRetry, due_sources, sync_source
from opspilot.storage import build_store

logger = logging.getLogger(__name__)

INGEST_TASK = "opspilot.ingest_document"
REQUEUE_TASK = "opspilot.requeue_stuck_documents"
INVESTIGATION_TASK = "opspilot.run_investigation"
ABANDONED_TASK = "opspilot.recover_abandoned_investigations"
SYNC_TASK = "opspilot.sync_source"
DUE_SOURCES_TASK = "opspilot.sync_due_sources"

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
        "recover-abandoned-investigations": {"task": ABANDONED_TASK, "schedule": 120.0},
        # The catch-up for push webhooks that never arrived
        "sync-due-sources": {"task": DUE_SOURCES_TASK, "schedule": 300.0},
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
            telemetry_http = httpx.AsyncClient(
                timeout=settings.telemetry_timeout_seconds, follow_redirects=False
            )
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
                    telemetry_http=telemetry_http,
                    reranker=build_reranker(settings),
                )
            finally:
                await telemetry_http.aclose()
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
def recover_abandoned_investigations_task() -> int:
    """Re-queues investigations whose worker died (they resume from their checkpoint), and
    fails the ones out of attempts."""

    async def job(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> Recovery:
        return await recover_abandoned_investigations(sessionmaker, settings, datetime.now(UTC))

    recovery = _run(job)
    for investigation_id in recovery.resumed:
        enqueue_investigation(investigation_id)
    if recovery.resumed or recovery.failed:
        logger.warning(
            "Abandoned investigations: %s resumed, %s failed",
            len(recovery.resumed),
            recovery.failed,
        )
    return len(recovery.resumed) + recovery.failed


class DeadLetterOnFailure(Task):  # type: ignore[type-arg]
    """Tasks whose last failure (out of retries, or an error retrying can't fix) goes to the
    dead-letter queue instead of only the log."""

    dead_letter_task = "unknown"

    def on_failure(
        self, exc: BaseException, task_id: str, args: Any, kwargs: Any, einfo: Any
    ) -> None:
        subject = (args or [None])[0] or kwargs.get("source_id")

        async def job(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
            workspace_id = None
            if self.dead_letter_task == dead_letters.SOURCE_SYNC and subject:
                async with sessionmaker() as session:
                    source = await session.get(Source, uuid.UUID(str(subject)))
                    workspace_id = source.workspace_id if source else None
            await dead_letters.record_now(
                sessionmaker,
                task=self.dead_letter_task,
                job_key=f"source:{subject}",
                payload={
                    "source_id": str(subject),
                    **{k: v for k, v in kwargs.items() if k == "force"},
                },
                error=f"{type(exc).__name__}: {exc}",
                attempts=self.request.retries + 1,
                workspace_id=workspace_id,
            )

        try:
            _run(job)
        except Exception:
            logger.exception("Couldn't record the dead letter for task %s", task_id)


@celery_app.task(
    name=SYNC_TASK,
    bind=True,
    base=DeadLetterOnFailure,
    dead_letter_task=dead_letters.SOURCE_SYNC,
    max_retries=5,
)
def sync_source_task(self: Task[Any, Any], source_id: str, force: bool = False) -> dict[str, Any]:
    async def job(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> SyncResult:
        async with httpx.AsyncClient() as http:
            result = await sync_source(
                sessionmaker,
                settings,
                uuid.UUID(source_id),
                http,
                build_store(settings),
                force=force,
                attempt=self.request.retries + 1,
            )
        for document_id in result.queued:
            enqueue_ingestion(document_id)
        return result

    try:
        return _run(job).summary()
    except SyncRetry as exc:
        # 30s, 1m, 2m, 4m, 8m: or as long as GitHub's rate limit says
        countdown = max(30 * 2**self.request.retries, int(exc.retry_after or 0))
        raise self.retry(exc=exc, countdown=min(countdown, 3600)) from exc


def enqueue_source_sync(source_id: uuid.UUID, force: bool = False) -> None:
    sync_source_task.apply_async(args=(str(source_id),), kwargs={"force": force})


@celery_app.task(name=DUE_SOURCES_TASK)
def sync_due_sources_task() -> int:
    async def find(
        sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
    ) -> list[uuid.UUID]:
        async with sessionmaker() as session:
            return await due_sources(session, settings, datetime.now(UTC))

    due = _run(find)
    for source_id in due:
        enqueue_source_sync(source_id)
    return len(due)
