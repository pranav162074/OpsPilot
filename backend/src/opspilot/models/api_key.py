import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from opspilot.models.base import Base, CreatedAtMixin, IdMixin

if TYPE_CHECKING:
    from opspilot.models.workspace import Workspace


class Role(enum.StrEnum):
    VIEWER = "viewer"  # read documents, search, see incidents
    RESPONDER = "responder"  # also run investigations and update incidents
    ADMIN = "admin"  # also manage API keys

    @property
    def rank(self) -> int:
        return _RANKS[self]

    def allows(self, required: "Role") -> bool:
        return self.rank >= required.rank


_RANKS = {Role.VIEWER: 1, Role.RESPONDER: 2, Role.ADMIN: 3}


class ApiKey(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint("role IN ('viewer', 'responder', 'admin')", name="role_valid"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    # The start of the key in plain text, so people can tell their keys apart
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    # SHA-256 of the full key. The key itself is never stored.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workspace: Mapped["Workspace"] = relationship(back_populates="api_keys")

    def is_active(self, now: datetime) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > now
