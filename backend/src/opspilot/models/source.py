import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from opspilot.models.base import Base, CreatedAtMixin, IdMixin


class SourceKind(enum.StrEnum):
    GITHUB = "github"


class SourceStatus(enum.StrEnum):
    IDLE = "idle"
    SYNCING = "syncing"
    FAILED = "failed"


class Source(IdMixin, CreatedAtMixin, Base):
    """Somewhere documents come from by themselves: a GitHub repository whose docs (and,
    optionally, code) are kept in sync, instead of uploaded one by one."""

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint("kind IN ('github')", name="kind_valid"),
        CheckConstraint("status IN ('idle', 'syncing', 'failed')", name="status_valid"),
        Index("ix_sources_status_last_synced_at", "status", "last_synced_at"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # github: {"repository": "owner/name", "ref": "main", "paths": ["docs/"],
    #          "include_code": false, "source_type": "runbook"}
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Fernet-encrypted (security/secrets.py): the access token (private repositories), and
    # the secret GitHub signs push webhooks with
    token_encrypted: Mapped[str | None] = mapped_column(Text)
    webhook_secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="idle")
    sync_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The commit the documents match; a sync at the same commit has nothing to do
    last_commit: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    # Consecutive failed syncs; reset by a good one
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # What the last sync did: {"added": 3, "updated": 1, "deleted": 0, "unchanged": 40, ...}
    last_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
