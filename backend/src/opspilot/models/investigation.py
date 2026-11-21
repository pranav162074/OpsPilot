import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from opspilot.models.base import Base, CreatedAtMixin, IdMixin


class InvestigationStatus(enum.StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Investigation(IdMixin, CreatedAtMixin, Base):
    """One question put to the agent, its answer, and the full trace of how it got there."""

    __tablename__ = "investigations"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'completed', 'failed')", name="status_valid"),
        Index("ix_investigations_workspace_id_created_at", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    created_by_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL")
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    answer: Mapped[str | None] = mapped_column(Text)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    trace: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(100))
    # Cost and speed, per run: what the dashboard and alerts will be built on
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_citations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
