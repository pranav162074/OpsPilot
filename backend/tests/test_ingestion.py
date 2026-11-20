import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select, update

from opspilot import worker
from opspilot.config import Settings
from opspilot.embeddings import HashingEmbedder
from opspilot.ingestion import service
from opspilot.ingestion.service import Outcome, find_stuck_documents
from opspilot.models import Chunk, Document
from tests.conftest import upload

Ingest = Callable[..., Awaitable[Outcome]]


@pytest.fixture
async def document_id(
    client: AsyncClient, create_workspace: Callable[..., Any], enqueued: list[uuid.UUID]
) -> uuid.UUID:
    _, key = await create_workspace()
    res = await upload(client, key, "# Runbook\n\nStep one.\n\n## More\n\nStep two.\n")
    return uuid.UUID(res.json()["id"])


async def set_document(app: FastAPI, document_id: uuid.UUID, **values: Any) -> None:
    async with app.state.sessionmaker() as session:
        await session.execute(update(Document).where(Document.id == document_id).values(**values))
        await session.commit()


async def get_document(app: FastAPI, document_id: uuid.UUID) -> Document:
    async with app.state.sessionmaker() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        return document


async def chunk_count(app: FastAPI) -> int:
    async with app.state.sessionmaker() as session:
        count = await session.scalar(select(func.count()).select_from(Chunk))
        return count or 0


async def test_running_twice_does_nothing_the_second_time(
    app: FastAPI, document_id: uuid.UUID, ingest: Ingest
) -> None:
    assert await ingest(document_id) == Outcome.READY
    assert await ingest(document_id) == Outcome.SKIPPED
    assert await chunk_count(app) == 2


async def test_two_workers_at_once_index_it_only_once(
    app: FastAPI, document_id: uuid.UUID, ingest: Ingest
) -> None:
    outcomes = await asyncio.gather(*(ingest(document_id) for _ in range(5)))

    assert sorted(outcomes) == sorted([Outcome.READY] + [Outcome.SKIPPED] * 4)
    assert (await get_document(app, document_id)).attempts == 1
    assert await chunk_count(app) == 2


async def test_takes_over_from_a_crashed_worker(
    app: FastAPI, document_id: uuid.UUID, ingest: Ingest
) -> None:
    long_ago = datetime.now(UTC) - timedelta(hours=1)
    await set_document(app, document_id, status="processing", attempts=1, updated_at=long_ago)

    assert await ingest(document_id) == Outcome.READY
    assert (await get_document(app, document_id)).attempts == 2


async def test_leaves_a_document_another_worker_is_still_processing(
    app: FastAPI, document_id: uuid.UUID, ingest: Ingest
) -> None:
    await set_document(
        app, document_id, status="processing", attempts=1, updated_at=datetime.now(UTC)
    )

    assert await ingest(document_id) == Outcome.SKIPPED


async def test_a_slow_worker_cannot_overwrite_a_newer_attempt(
    app: FastAPI, settings: Settings, document_id: uuid.UUID
) -> None:
    # Worker A claimed attempt 1, then stalled; worker B took over as attempt 2
    document = await get_document(app, document_id)
    await set_document(app, document_id, status="processing", attempts=2)

    outcome = await service._process(
        app.state.sessionmaker,
        settings,
        HashingEmbedder(),
        document_id,
        1,
        document.workspace_id,
        document.media_type,
    )

    assert outcome == Outcome.SUPERSEDED
    assert await chunk_count(app) == 0
    assert (await get_document(app, document_id)).status == "processing"


async def test_a_document_deleted_mid_way_is_dropped(
    app: FastAPI, settings: Settings, document_id: uuid.UUID
) -> None:
    document = await get_document(app, document_id)
    async with app.state.sessionmaker() as session:
        await session.delete(await session.get(Document, document_id))
        await session.commit()

    outcome = await service._process(
        app.state.sessionmaker,
        settings,
        HashingEmbedder(),
        document_id,
        1,
        document.workspace_id,
        document.media_type,
    )

    assert outcome == Outcome.SKIPPED


async def test_gives_up_after_the_maximum_attempts(
    app: FastAPI, document_id: uuid.UUID, ingest: Ingest
) -> None:
    await set_document(app, document_id, attempts=3)

    assert await ingest(document_id) == Outcome.FAILED
    document = await get_document(app, document_id)
    assert document.status == "failed"
    assert document.error == "Gave up after 3 attempts"


async def test_a_temporary_error_hands_the_document_back(
    app: FastAPI, document_id: uuid.UUID, ingest: Ingest, monkeypatch: pytest.MonkeyPatch
) -> None:
    def flaky(*_: Any, **__: Any) -> Any:
        raise ConnectionResetError("database went away")

    monkeypatch.setattr(service, "chunk_text", flaky)

    with pytest.raises(ConnectionResetError):
        await ingest(document_id)

    # Pending again, so the Celery retry can claim it straight away
    document = await get_document(app, document_id)
    assert (document.status, document.attempts) == ("pending", 1)
    monkeypatch.undo()
    assert await ingest(document_id) == Outcome.READY


async def test_finds_documents_whose_task_was_lost(
    app: FastAPI,
    settings: Settings,
    client: AsyncClient,
    create_workspace: Callable[..., Any],
    enqueued: list[uuid.UUID],
) -> None:
    _, key = await create_workspace()
    ids = {}
    for name in ["old_pending", "new_pending", "stale_processing", "busy_processing", "ready"]:
        ids[name] = uuid.UUID((await upload(client, key, name, f"{name}.txt")).json()["id"])
    now = datetime.now(UTC)
    hour_ago = now - timedelta(hours=1)
    await set_document(app, ids["old_pending"], updated_at=hour_ago)
    await set_document(app, ids["stale_processing"], status="processing", updated_at=hour_ago)
    await set_document(app, ids["busy_processing"], status="processing", updated_at=now)
    await set_document(app, ids["ready"], status="ready", updated_at=hour_ago)

    async with app.state.sessionmaker() as session:
        stuck = await find_stuck_documents(session, settings, now)

    assert set(stuck) == {ids["old_pending"], ids["stale_processing"]}


async def test_celery_tasks(
    app: FastAPI,
    settings: Settings,
    document_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The tasks build their own database connections from settings, so point them at the
    # test database, and capture what they would queue
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    sent: list[uuid.UUID] = []
    monkeypatch.setattr(worker, "enqueue_ingestion", sent.append)
    await set_document(app, document_id, updated_at=datetime.now(UTC) - timedelta(hours=1))

    requeued = await asyncio.to_thread(worker.requeue_stuck_documents_task.apply)
    result = await asyncio.to_thread(worker.ingest_document_task.apply, args=(str(document_id),))

    assert requeued.get() == 1
    assert sent == [document_id]
    assert result.get() == "ready"
    assert (await get_document(app, document_id)).status == "ready"
