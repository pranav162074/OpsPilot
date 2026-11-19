import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from opspilot.models.base import Base, CreatedAtMixin, IdMixin


class DocumentStatus(enum.StrEnum):
    PENDING = "pending"  # uploaded, waiting for a worker
    PROCESSING = "processing"  # a worker is chunking it
    READY = "ready"  # chunks are stored
    FAILED = "failed"  # gave up; `error` says why


class SourceType(enum.StrEnum):
    RUNBOOK = "runbook"
    POSTMORTEM = "postmortem"
    ARCHITECTURE = "architecture"
    OTHER = "other"


def _one_of(column: str, values: type[enum.StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


class Document(IdMixin, CreatedAtMixin, Base):
    """An uploaded file. Its text is split into chunks by the ingestion worker."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(_one_of("status", DocumentStatus), name="status_valid"),
        CheckConstraint(_one_of("source_type", SourceType), name="source_type_valid"),
        # The same file uploaded twice to one workspace is a duplicate, not a second document
        UniqueConstraint("workspace_id", "content_hash"),
        # Lets chunks reference (id, workspace_id), so a chunk can't end up in another workspace
        UniqueConstraint("id", "workspace_id"),
        # Serves the newest-first, cursor-paginated document list
        Index("ix_documents_workspace_id_created_at", "workspace_id", "created_at", "id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    media_type: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DocumentStatus.PENDING.value, server_default="pending"
    )
    # Incremented each time a worker claims the document. Doubles as a fencing token: a
    # worker only saves its result if no newer worker has claimed the document since.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    uploaded_by_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Chunk(IdMixin, Base):
    """A slice of a document's text, small enough to embed and to quote as a source."""

    __tablename__ = "chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "workspace_id"],
            ["documents.id", "documents.workspace_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("document_id", "ordinal"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Stored on the chunk too, so search can filter by workspace without a join
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    # Markdown headings above this chunk, like "Database > Failover"
    heading: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Where the chunk sits in the document, so answers can point at the exact passage
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
