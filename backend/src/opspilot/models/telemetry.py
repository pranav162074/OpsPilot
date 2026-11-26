import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from opspilot.models.base import Base, CreatedAtMixin, IdMixin


class IntegrationKind(enum.StrEnum):
    PROMETHEUS = "prometheus"
    LOKI = "loki"
    GITHUB = "github"
    # Not telemetry: where approved actions are sent (a signed webhook). The agent never
    # calls it; only a person approving an action does.
    ACTIONS = "actions"


class Integration(IdMixin, CreatedAtMixin, Base):
    """Where a workspace's telemetry lives: its Prometheus, its Loki, its GitHub repository.
    The agent reads from these while investigating; it never writes to them."""

    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "kind"),
        CheckConstraint("kind IN ('prometheus', 'loki', 'github', 'actions')", name="kind_valid"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    # A bearer token or GitHub token, encrypted at rest (see security/secrets.py); never
    # returned by the API
    secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Kind-specific settings, e.g. {"repository": "owner/name"} for GitHub
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Deploy(IdMixin, CreatedAtMixin, Base):
    """A release that went out, reported by CI. "What changed just before this started?" is
    the first question in most incidents."""

    __tablename__ = "deploys"
    __table_args__ = (Index("ix_deploys_workspace_id_deployed_at", "workspace_id", "deployed_at"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    service: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    environment: Mapped[str] = mapped_column(String(50), nullable=False)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    deployed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
