"""Agent v2: strategies, token budget, resumable runs, approved actions, postmortem drafts

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "investigations",
        sa.Column("strategy", sa.String(length=10), server_default="react", nullable=False),
    )
    op.add_column(
        "investigations",
        sa.Column(
            "hypotheses",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "investigations",
        sa.Column("budget_exhausted", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "investigations",
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "investigations", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "investigations",
        sa.Column("checkpoint", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_investigations_strategy_valid"), "investigations", "strategy IN ('react', 'plan')"
    )
    # Runs that had started before this migration count as one attempt
    op.execute("UPDATE investigations SET attempts = 1 WHERE started_at IS NOT NULL")

    op.create_table(
        "investigation_actions",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("risk", sa.String(length=10), nullable=False),
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("decided_by_key_id", sa.UUID(), nullable=True),
        sa.Column("decided_by", sa.String(length=100), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'executed', 'failed')",
            name=op.f("ck_investigation_actions_status_valid"),
        ),
        sa.CheckConstraint(
            "risk IN ('low', 'medium', 'high')", name=op.f("ck_investigation_actions_risk_valid")
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_key_id"],
            ["api_keys.id"],
            name=op.f("fk_investigation_actions_decided_by_key_id_api_keys"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name=op.f("fk_investigation_actions_investigation_id_investigations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_investigation_actions_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_actions")),
    )
    op.create_index(
        op.f("ix_investigation_actions_investigation_id"),
        "investigation_actions",
        ["investigation_id"],
    )
    op.create_index(
        "ix_investigation_actions_workspace_id_status",
        "investigation_actions",
        ["workspace_id", "status"],
    )

    op.add_column("incidents", sa.Column("postmortem_draft", sa.Text(), nullable=True))
    op.add_column(
        "incidents",
        sa.Column("postmortem_drafted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("incidents", sa.Column("postmortem_document_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_incidents_postmortem_document_id_documents"),
        "incidents",
        "documents",
        ["postmortem_document_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_constraint(op.f("ck_integrations_kind_valid"), "integrations", type_="check")
    op.create_check_constraint(
        op.f("ck_integrations_kind_valid"),
        "integrations",
        "kind IN ('prometheus', 'loki', 'github', 'actions')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM integrations WHERE kind = 'actions'")
    op.drop_constraint(op.f("ck_integrations_kind_valid"), "integrations", type_="check")
    op.create_check_constraint(
        op.f("ck_integrations_kind_valid"),
        "integrations",
        "kind IN ('prometheus', 'loki', 'github')",
    )
    op.drop_constraint(
        op.f("fk_incidents_postmortem_document_id_documents"), "incidents", type_="foreignkey"
    )
    op.drop_column("incidents", "postmortem_document_id")
    op.drop_column("incidents", "postmortem_drafted_at")
    op.drop_column("incidents", "postmortem_draft")
    op.drop_index("ix_investigation_actions_workspace_id_status", "investigation_actions")
    op.drop_index(op.f("ix_investigation_actions_investigation_id"), "investigation_actions")
    op.drop_table("investigation_actions")
    op.drop_constraint(op.f("ck_investigations_strategy_valid"), "investigations", type_="check")
    for column in (
        "checkpoint",
        "heartbeat_at",
        "attempts",
        "budget_exhausted",
        "hypotheses",
        "strategy",
    ):
        op.drop_column("investigations", column)
