import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from opspilot.models.base import Base, CreatedAtMixin, IdMixin


class Severity(enum.StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"critical": 4, "high": 3, "medium": 2, "low": 1}[self.value]


class IncidentStatus(enum.StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class Incident(IdMixin, CreatedAtMixin, Base):
    """A problem being handled. Repeats of the same alert attach to the open incident instead
    of opening new ones."""

    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'acknowledged', 'resolved')", name="status_valid"),
        CheckConstraint("severity IN ('critical', 'high', 'medium', 'low')", name="severity_valid"),
        # At most one unresolved incident per alert fingerprint: what makes deduplication
        # safe when the same alert arrives many times at once
        Index(
            "uq_incidents_open_fingerprint",
            "workspace_id",
            "fingerprint",
            unique=True,
            postgresql_where=text("status <> 'resolved'"),
        ),
        Index("ix_incidents_workspace_id_created_at", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    alert_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_alert_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_alert_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investigations.id", ondelete="SET NULL")
    )
    # A postmortem the agent drafted from the timeline and the investigation. It's only
    # searchable once someone publishes it (it becomes a postmortem document)
    postmortem_draft: Mapped[str | None] = mapped_column(Text)
    postmortem_drafted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    postmortem_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL")
    )


class IncidentEvent(IdMixin, CreatedAtMixin, Base):
    """The incident's timeline: opened, acknowledged, investigated, resolved."""

    __tablename__ = "incident_events"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Events written in one transaction share created_at (it's the transaction's start
    # time), so the timeline is ordered by this ever-increasing number instead
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False, unique=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
