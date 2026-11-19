import base64
import binascii
import hashlib
import logging
import uuid
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Request, Response, UploadFile, status
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from opspilot.api.deps import Responder, SessionDep, SettingsDep, Viewer
from opspilot.api.errors import ApiError
from opspilot.api.schemas import ChunkOut, DocumentOut, DocumentPage
from opspilot.models import Chunk, Document, DocumentStatus, SourceType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/documents", tags=["documents"])

MEDIA_TYPES = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain"}
# Room for the multipart boundaries and the other form fields around the file
MULTIPART_OVERHEAD = 16 * 1024


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "Document not found")


async def _enqueue(request: Request, document_id: uuid.UUID) -> None:
    try:
        # Publishing to Celery is blocking network I/O, so it runs off the event loop
        await run_in_threadpool(request.app.state.enqueue_ingestion, document_id)
    except Exception:
        # The document is saved as pending; the requeue job will send it again
        logger.warning("Could not queue document %s for ingestion", document_id, exc_info=True)


def _decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")  # -sig drops the byte-order mark some editors add
    except UnicodeDecodeError as exc:
        raise ApiError(422, "invalid_encoding", "The file must be UTF-8 text") from exc
    if "\x00" in text:
        raise ApiError(422, "invalid_encoding", "The file looks binary, not text")
    # Same text from Windows and Linux should hash the same, so duplicates are caught
    return text.replace("\r\n", "\n").replace("\r", "\n")


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    request: Request,
    principal: Responder,
    session: SessionDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File(description="A .md or .txt file")],
    title: Annotated[str | None, Form(max_length=200)] = None,
    source_type: Annotated[SourceType, Form()] = SourceType.OTHER,
) -> DocumentOut:
    """Stores the file and queues it for indexing. Poll the document until status is ready."""
    limit = settings.max_document_bytes
    too_large = ApiError(413, "too_large", f"Files can be at most {limit} bytes")
    # Cheap early check; the real check is on the bytes actually read
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + MULTIPART_OVERHEAD:
        raise too_large

    filename = PurePath(file.filename or "").name
    media_type = MEDIA_TYPES.get(PurePath(filename).suffix.lower())
    if media_type is None:
        raise ApiError(415, "unsupported_file_type", "Upload a .md, .markdown or .txt file")

    data = await file.read(limit + 1)
    if len(data) > limit:
        raise too_large
    content = _decode(data)
    if not content.strip():
        raise ApiError(422, "empty_document", "The file has no text")

    content_hash = hashlib.sha256(content.encode()).hexdigest()
    workspace_id = principal.workspace.id

    async def duplicate() -> ApiError | None:
        existing = await session.scalar(
            select(Document.id).where(
                Document.workspace_id == workspace_id, Document.content_hash == content_hash
            )
        )
        if existing is None:
            return None
        return ApiError(
            409,
            "duplicate_document",
            "This workspace already has a document with the same content",
            details={"document_id": str(existing)},
        )

    if error := await duplicate():
        raise error

    document = Document(
        workspace_id=workspace_id,
        title=(title or "").strip() or PurePath(filename).stem or "Untitled",
        filename=filename,
        source_type=source_type.value,
        media_type=media_type,
        content=content,
        content_hash=content_hash,
        size_bytes=len(data),
        uploaded_by_key_id=principal.api_key.id,
        updated_at=datetime.now(UTC),
    )
    session.add(document)
    try:
        await session.commit()
    except IntegrityError:
        # Two identical uploads at the same moment: the unique constraint let only one in
        await session.rollback()
        if error := await duplicate():
            raise error from None
        raise

    await _enqueue(request, document.id)
    return DocumentOut.model_validate(document)


def _encode_cursor(document: Document) -> str:
    raw = f"{document.created_at.isoformat()}|{document.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        created_at, document_id = raw.split("|")
        return datetime.fromisoformat(created_at), uuid.UUID(document_id)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ApiError(422, "invalid_cursor", "The cursor is not valid") from exc


@router.get("")
async def list_documents(
    principal: Viewer,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
    status: DocumentStatus | None = None,
    source_type: SourceType | None = None,
) -> DocumentPage:
    """Newest first. Keyset pagination: pages stay correct while documents are added."""
    query = select(Document).where(Document.workspace_id == principal.workspace.id)
    if status is not None:
        query = query.where(Document.status == status.value)
    if source_type is not None:
        query = query.where(Document.source_type == source_type.value)
    if cursor is not None:
        created_at, document_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                Document.created_at < created_at,
                and_(Document.created_at == created_at, Document.id < document_id),
            )
        )
    rows = list(
        await session.scalars(
            query.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit + 1)
        )
    )
    page, more = rows[:limit], len(rows) > limit
    return DocumentPage(
        items=[DocumentOut.model_validate(row) for row in page],
        next_cursor=_encode_cursor(page[-1]) if more else None,
    )


async def _get(session: SessionDep, principal: Viewer, document_id: uuid.UUID) -> Document:
    document = await session.scalar(
        select(Document).where(
            Document.id == document_id, Document.workspace_id == principal.workspace.id
        )
    )
    if document is None:
        raise _not_found()
    return document


@router.get("/{document_id}")
async def get_document(
    document_id: uuid.UUID, principal: Viewer, session: SessionDep
) -> DocumentOut:
    return DocumentOut.model_validate(await _get(session, principal, document_id))


@router.get("/{document_id}/chunks")
async def list_chunks(
    document_id: uuid.UUID, principal: Viewer, session: SessionDep
) -> list[ChunkOut]:
    await _get(session, principal, document_id)
    rows = await session.scalars(
        select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.ordinal)
    )
    return [ChunkOut.model_validate(row) for row in rows]


@router.post("/{document_id}/reingest", status_code=status.HTTP_202_ACCEPTED)
async def reingest_document(
    request: Request, document_id: uuid.UUID, principal: Responder, session: SessionDep
) -> DocumentOut:
    """Indexes the document again, for example after chunking settings change."""
    document = await _get(session, principal, document_id)
    result = await session.execute(
        update(Document)
        .where(
            Document.id == document.id,
            Document.status.in_([DocumentStatus.READY.value, DocumentStatus.FAILED.value]),
        )
        .values(
            status=DocumentStatus.PENDING.value,
            attempts=0,
            error=None,
            updated_at=datetime.now(UTC),
        )
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise ApiError(409, "ingestion_in_progress", "The document is already being indexed")
    await session.commit()
    await session.refresh(document)
    await _enqueue(request, document.id)
    return DocumentOut.model_validate(document)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID, principal: Responder, session: SessionDep
) -> Response:
    document = await _get(session, principal, document_id)
    await session.delete(document)  # chunks go with it (ON DELETE CASCADE)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
