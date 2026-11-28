import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from opspilot.models.base import Base, CreatedAtMixin, IdMixin


class DeadLetterStatus(enum.StrEnum):
    DEAD = "dead"  # waiting for a person
    RETRIED = "retried"  # sent back to the queue
    DISCARDED = "discarded"  # a person decided it isn't worth retrying


class DeadLetter(IdMixin, CreatedAtMixin, Base):
    """A background job that failed for good: out of retries, or failed in a way retrying
    can't fix. Kept with its arguments and error, so it can be looked at and sent again,
    instead of disappearing into a log line."""

    __tablename__ = "dead_letters"
    __table_args__ = (
        CheckConstraint("status IN ('dead', 'retried', 'discarded')", name="status_valid"),
        Index(
            "ix_dead_letters_workspace_id_status_created_at", "workspace_id", "status", "created_at"
        ),
        # One open dead letter per job: failing again updates it
        Index(
            "uq_dead_letters_open_job",
            "task",
            "job_key",
            unique=True,
            postgresql_where="status = 'dead'",
        ),
    )

    # None for jobs that belong to no workspace
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE")
    )
    task: Mapped[str] = mapped_column(String(100), nullable=False)
    # What the job was about ("document:<id>", "source:<id>"), to find it again
    job_key: Mapped[str] = mapped_column(String(200), nullable=False)
    # The task's arguments, to send it again exactly
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    # How many times it ran before landing here, across retries
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Failures of the same job while it was dead (each one bumps this)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="dead")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL")
    )
