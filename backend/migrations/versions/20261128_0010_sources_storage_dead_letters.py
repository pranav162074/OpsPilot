"""Sources (GitHub), raw files in object storage, dead letters, code documents

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("token_encrypted", sa.Text(), nullable=True),
        sa.Column("webhook_secret_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("sync_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_commit", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("last_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('github')", name=op.f("ck_sources_kind_valid")),
        sa.CheckConstraint(
            "status IN ('idle', 'syncing', 'failed')", name=op.f("ck_sources_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_sources_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
    )
    op.create_index(
        "ix_sources_status_last_synced_at", "sources", ["status", "last_synced_at"], unique=False
    )
    op.create_table(
        "dead_letters",
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("task", sa.String(length=100), nullable=False),
        sa.Column("job_key", sa.String(length=200), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_key_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('dead', 'retried', 'discarded')", name=op.f("ck_dead_letters_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_key_id"],
            ["api_keys.id"],
            name=op.f("fk_dead_letters_resolved_by_key_id_api_keys"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_dead_letters_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dead_letters")),
    )
    op.create_index(
        "ix_dead_letters_workspace_id_status_created_at",
        "dead_letters",
        ["workspace_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_dead_letters_open_job",
        "dead_letters",
        ["task", "job_key"],
        unique=True,
        postgresql_where="status = 'dead'",
    )
    op.add_column("documents", sa.Column("source_id", sa.UUID(), nullable=True))
    op.add_column("documents", sa.Column("external_path", sa.String(length=1024), nullable=True))
    op.add_column("documents", sa.Column("external_sha", sa.String(length=64), nullable=True))
    op.add_column("documents", sa.Column("storage_key", sa.String(length=512), nullable=True))
    op.drop_constraint(op.f("uq_documents_workspace_id_content_hash"), "documents", type_="unique")
    op.create_unique_constraint(
        op.f("uq_documents_source_id_external_path"), "documents", ["source_id", "external_path"]
    )
    op.create_index(
        "uq_documents_upload_content_hash",
        "documents",
        ["workspace_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text("source_id IS NULL"),
    )
    op.create_foreign_key(
        op.f("fk_documents_source_id_sources"),
        "documents",
        "sources",
        ["source_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(op.f("ck_documents_source_type_valid"), "documents", type_="check")
    op.create_check_constraint(
        op.f("ck_documents_source_type_valid"),
        "documents",
        "source_type IN ('runbook', 'postmortem', 'architecture', 'code', 'other')",
    )


def downgrade() -> None:
    # Synced documents can't exist without their sources, and code wasn't a type before
    op.execute("DELETE FROM documents WHERE source_id IS NOT NULL OR source_type = 'code'")
    op.drop_constraint(op.f("ck_documents_source_type_valid"), "documents", type_="check")
    op.create_check_constraint(
        op.f("ck_documents_source_type_valid"),
        "documents",
        "source_type IN ('runbook', 'postmortem', 'architecture', 'other')",
    )
    op.drop_constraint(op.f("fk_documents_source_id_sources"), "documents", type_="foreignkey")
    op.drop_index(
        "uq_documents_upload_content_hash",
        table_name="documents",
        postgresql_where=sa.text("source_id IS NULL"),
    )
    op.drop_constraint(op.f("uq_documents_source_id_external_path"), "documents", type_="unique")
    op.create_unique_constraint(
        op.f("uq_documents_workspace_id_content_hash"),
        "documents",
        ["workspace_id", "content_hash"],
    )
    op.drop_column("documents", "storage_key")
    op.drop_column("documents", "external_sha")
    op.drop_column("documents", "external_path")
    op.drop_column("documents", "source_id")
    op.drop_index(
        "uq_dead_letters_open_job", table_name="dead_letters", postgresql_where="status = 'dead'"
    )
    op.drop_index("ix_dead_letters_workspace_id_status_created_at", table_name="dead_letters")
    op.drop_table("dead_letters")
    op.drop_index("ix_sources_status_last_synced_at", table_name="sources")
    op.drop_table("sources")
