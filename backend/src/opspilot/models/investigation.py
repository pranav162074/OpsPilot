import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
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
        CheckConstraint("strategy IN ('react', 'plan')", name="strategy_valid"),
        Index("ix_investigations_workspace_id_created_at", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    created_by_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL")
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    source_types: Mapped[list[str] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    strategy: Mapped[str] = mapped_column(String(10), nullable=False, default="react")
    answer: Mapped[str | None] = mapped_column(Text)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    # "plan" runs: the hypotheses considered and what checking each one showed
    hypotheses: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
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
    # The run hit the token budget and answered with the evidence it had by then
    budget_exhausted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Set when a runner claims it: a second delivery of the same task finds it taken and
    # skips it, instead of paying for the same model calls twice
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Resumable runs: how many times a runner has claimed it, when the running one last
    # showed signs of life, and the state after its last finished stage ("plan" runs)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ActionStatus(enum.StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"  # approved, to be carried out by a person (no actions webhook)
    REJECTED = "rejected"
    EXECUTED = "executed"  # approved and sent to the actions webhook, which accepted it
    FAILED = "failed"  # approved, but the actions webhook refused it or couldn't be reached


class InvestigationAction(IdMixin, CreatedAtMixin, Base):
    """Something the agent suggests doing (restart, roll back, scale). It is never carried out
    by the agent: a person approves or rejects it, and only an approved action is sent on."""

    __tablename__ = "investigation_actions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'executed', 'failed')",
            name="status_valid",
        ),
        CheckConstraint("risk IN ('low', 'medium', 'high')", name="risk_valid"),
        Index("ix_investigation_actions_workspace_id_status", "workspace_id", "status"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("investigations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Order within the investigation's answer (rows of one run share created_at)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    command: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(String(10), nullable=False)
    # The sources (runbook passages, observations) the suggestion rests on
    citations: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")
    decided_by_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL")
    )
    decided_by: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    # What the actions webhook answered: {"status_code": 200, "body": "..."} or {"error": ...}
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
