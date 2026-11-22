"""Incidents, their timelines, and background investigations

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("labels", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("alert_count", sa.Integer(), nullable=False),
        sa.Column(
            "first_alert_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_alert_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("investigation_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "severity IN ('critical', 'high', 'medium', 'low')",
            name=op.f("ck_incidents_severity_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'acknowledged', 'resolved')", name=op.f("ck_incidents_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name=op.f("fk_incidents_investigation_id_investigations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_incidents_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incidents")),
    )
    op.create_index(
        "ix_incidents_workspace_id_created_at",
        "incidents",
        ["workspace_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_incidents_open_fingerprint",
        "incidents",
        ["workspace_id", "fingerprint"],
        unique=True,
        postgresql_where=sa.text("status <> 'resolved'"),
    )
    op.create_table(
        "incident_events",
        sa.Column("incident_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name=op.f("fk_incident_events_incident_id_incidents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incident_events")),
        sa.UniqueConstraint("seq", name=op.f("uq_incident_events_seq")),
    )
    op.create_index(
        op.f("ix_incident_events_incident_id"), "incident_events", ["incident_id"], unique=False
    )
    op.add_column(
        "investigations",
        sa.Column("source_types", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "investigations", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("investigations", "started_at")
    op.drop_column("investigations", "source_types")
    op.drop_index(op.f("ix_incident_events_incident_id"), table_name="incident_events")
    op.drop_table("incident_events")
    op.drop_index(
        "uq_incidents_open_fingerprint",
        table_name="incidents",
        postgresql_where=sa.text("status <> 'resolved'"),
    )
    op.drop_index("ix_incidents_workspace_id_created_at", table_name="incidents")
    op.drop_table("incidents")
