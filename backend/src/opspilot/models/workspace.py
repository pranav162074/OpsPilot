from typing import TYPE_CHECKING

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from opspilot.models.base import Base, CreatedAtMixin, IdMixin

if TYPE_CHECKING:
    from opspilot.models.api_key import ApiKey


class Workspace(IdMixin, CreatedAtMixin, Base):
    """A tenant. Everything (keys, documents, incidents) belongs to exactly one workspace."""

    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(100), nullable=False)

    api_keys: Mapped[list["ApiKey"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan", passive_deletes=True
    )
