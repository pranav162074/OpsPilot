"""Answer quality: injection flags on chunks and actions, cost per investigation, budgets

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chunks",
        sa.Column(
            "flags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("investigations", sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True))
    op.add_column(
        "investigations",
        sa.Column("flagged_sources", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "investigation_actions",
        sa.Column("verified", sa.Boolean(), server_default="true", nullable=False),
    )
    op.add_column(
        "investigation_actions",
        sa.Column(
            "flags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("workspaces", sa.Column("monthly_budget_usd", sa.Numeric(10, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("workspaces", "monthly_budget_usd")
    op.drop_column("investigation_actions", "flags")
    op.drop_column("investigation_actions", "verified")
    op.drop_column("investigations", "flagged_sources")
    op.drop_column("investigations", "cost_usd")
    op.drop_column("chunks", "flags")
