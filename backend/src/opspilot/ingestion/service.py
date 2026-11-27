"""Turns an uploaded document into stored chunks.

Safe to run more than once for the same document, and from several workers at once:
- a worker first claims the document (pending -> processing) in one UPDATE, so only one
  worker processes it at a time;
- each claim increments `attempts`, and the result is saved only if `attempts` is still the
  value this worker claimed with, so a slow worker can't overwrite a newer one's result;
- chunks are replaced and the document marked ready in one transaction, so readers never
  see half a document.
"""

import enum
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, insert, or_, select, update
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opspilot.config import Settings
from opspilot.embeddings import Embedder, EmbeddingError, EmbeddingUnavailable
from opspilot.ingestion.chunking import ChunkDraft, chunk_text
from opspilot.models import Chunk, Document, DocumentStatus
from opspilot.security.injection import scan

logger = logging.getLogger(__name__)

MARKDOWN = "text/markdown"

# Errors worth retrying: the database or network had a moment, the document is fine
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    OperationalError,
    InterfaceError,
    OSError,
    TimeoutError,
    EmbeddingUnavailable,
)


class Outcome(enum.StrEnum):
    READY = "ready"
    FAILED = "failed"
    SKIPPED = "skipped"  # not pending, deleted, or another worker has it
    SUPERSEDED = "superseded"  # a newer attempt claimed it while this one ran


def _claimable(settings: Settings, now: datetime) -> object:
    stale_before = now - timedelta(seconds=settings.ingestion_stale_seconds)
    return or_(
        Document.status == DocumentStatus.PENDING.value,
        # A worker that crashed mid-way leaves the document "processing"; take it over
        and_(
            Document.status == DocumentStatus.PROCESSING.value,
            Document.updated_at < stale_before,
        ),
    )


async def _fail(session: AsyncSession, document_id: uuid.UUID, attempt: int, error: str) -> None:
    await session.execute(
        update(Document)
        .where(Document.id == document_id, Document.attempts == attempt)
        .values(status=DocumentStatus.FAILED.value, error=error, updated_at=datetime.now(UTC))
    )
    await session.commit()


async def release(
    sessionmaker: async_sessionmaker[AsyncSession], document_id: uuid.UUID, attempt: int
) -> None:
    """Hands a claimed document back after a temporary error, so a retry can claim it."""
    async with sessionmaker() as session:
        await session.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.status == DocumentStatus.PROCESSING.value,
                Document.attempts == attempt,
            )
            .values(status=DocumentStatus.PENDING.value, updated_at=datetime.now(UTC))
        )
        await session.commit()


async def ingest_document(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    document_id: uuid.UUID,
    embedder: Embedder,
) -> Outcome:
    now = datetime.now(UTC)
    async with sessionmaker() as session:
        claimed = (
            await session.execute(
                update(Document)
                .where(Document.id == document_id, _claimable(settings, now))  # type: ignore[arg-type]
                .values(
                    status=DocumentStatus.PROCESSING.value,
                    attempts=Document.attempts + 1,
                    error=None,
                    updated_at=now,
                )
                .returning(Document.attempts, Document.workspace_id, Document.media_type)
            )
        ).one_or_none()
        await session.commit()
        if claimed is None:
            return Outcome.SKIPPED
        attempt = claimed.attempts

        if attempt > settings.ingestion_max_attempts:
            await _fail(session, document_id, attempt, f"Gave up after {attempt - 1} attempts")
            return Outcome.FAILED

    try:
        return await _process(
            sessionmaker,
            settings,
            embedder,
            document_id,
            attempt,
            claimed.workspace_id,
            claimed.media_type,
        )
    except TRANSIENT_ERRORS:
        try:
            await release(sessionmaker, document_id, attempt)
        except Exception:
            # Still unreachable. The document stays "processing" and is picked up again
            # once it counts as stale.
            logger.warning("Could not release document %s", document_id, exc_info=True)
        raise


def embedding_text(draft: ChunkDraft) -> str:
    """What gets embedded: the heading path gives the chunk context its own text may lack."""
    return f"{draft.heading}\n\n{draft.content}" if draft.heading else draft.content


async def _process(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: Embedder,
    document_id: uuid.UUID,
    attempt: int,
    workspace_id: uuid.UUID,
    media_type: str,
) -> Outcome:
    async with sessionmaker() as session:
        # Loaded separately so the claim stays a small, fast UPDATE
        content = await session.scalar(select(Document.content).where(Document.id == document_id))
    if content is None:
        return Outcome.SKIPPED

    drafts = chunk_text(
        content,
        markdown=media_type == MARKDOWN,
        max_tokens=settings.chunk_max_tokens,
        overlap_tokens=settings.chunk_overlap_tokens,
    )
    if not drafts:
        async with sessionmaker() as session:
            await _fail(session, document_id, attempt, "The document has no text to index")
        return Outcome.FAILED

    # Called with no database connection held: an embedding API call can take seconds
    try:
        vectors = await embedder.embed_documents([embedding_text(d) for d in drafts])
    except EmbeddingError as exc:
        async with sessionmaker() as session:
            await _fail(session, document_id, attempt, f"Embedding failed: {exc}")
        return Outcome.FAILED

    async with sessionmaker() as session:
        try:
            await session.execute(delete(Chunk).where(Chunk.document_id == document_id))
            await session.execute(
                insert(Chunk),
                [
                    {
                        "id": uuid.uuid4(),
                        "document_id": document_id,
                        "workspace_id": workspace_id,
                        "ordinal": draft.ordinal,
                        "heading": draft.heading,
                        "content": draft.content,
                        "flags": scan(f"{draft.heading}\n{draft.content}"),
                        "token_count": draft.token_count,
                        "char_start": draft.char_start,
                        "char_end": draft.char_end,
                        "embedding": vector,
                        "embedding_model": embedder.model,
                    }
                    for draft, vector in zip(drafts, vectors, strict=True)
                ],
            )
            saved = await session.execute(
                update(Document)
                .where(
                    Document.id == document_id,
                    Document.status == DocumentStatus.PROCESSING.value,
                    Document.attempts == attempt,
                )
                .values(
                    status=DocumentStatus.READY.value,
                    chunk_count=len(drafts),
                    processed_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            if saved.rowcount != 1:  # type: ignore[attr-defined]
                await session.rollback()
                logger.info(
                    "Document %s was claimed again; dropping attempt %s", document_id, attempt
                )
                return Outcome.SUPERSEDED
            await session.commit()
        except IntegrityError:
            # The document was deleted, or a newer attempt is writing the same chunks
            await session.rollback()
            return Outcome.SUPERSEDED

    logger.info("Indexed document %s into %s chunks", document_id, len(drafts))
    return Outcome.READY


async def find_stuck_documents(
    session: AsyncSession, settings: Settings, now: datetime, limit: int = 100
) -> list[uuid.UUID]:
    """Documents whose task seems lost: pending for too long, or processing but abandoned."""
    requeue_before = now - timedelta(seconds=settings.ingestion_requeue_after_seconds)
    stale_before = now - timedelta(seconds=settings.ingestion_stale_seconds)
    rows = await session.scalars(
        select(Document.id)
        .where(
            or_(
                and_(
                    Document.status == DocumentStatus.PENDING.value,
                    Document.updated_at < requeue_before,
                ),
                and_(
                    Document.status == DocumentStatus.PROCESSING.value,
                    Document.updated_at < stale_before,
                ),
            )
        )
        .order_by(Document.updated_at)
        .limit(limit)
    )
    return list(rows)


async def mark_for_reindex(
    session: AsyncSession, model: str, everything: bool = False
) -> list[uuid.UUID]:
    """Queues documents whose chunks weren't embedded by `model` (or all, with everything=True)
    to be indexed again, for example after switching embedding models."""
    query = select(Document.id).where(
        Document.status.in_([DocumentStatus.READY.value, DocumentStatus.FAILED.value])
    )
    if not everything:
        outdated = select(Chunk.document_id).where(
            or_(Chunk.embedding_model.is_(None), Chunk.embedding_model != model)
        )
        query = query.where(
            or_(Document.id.in_(outdated), Document.status == DocumentStatus.FAILED.value)
        )
    ids = list(await session.scalars(query))
    if ids:
        await session.execute(
            update(Document)
            .where(Document.id.in_(ids))
            .values(
                status=DocumentStatus.PENDING.value,
                attempts=0,
                error=None,
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return ids
