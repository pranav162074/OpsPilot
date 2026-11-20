"""Chunk embeddings and search indexes

Adds an embedding (pgvector) and a full-text search vector to every chunk, with an HNSW index
for nearest-neighbour search and a GIN index for keyword search. Existing chunks get no
embedding until their documents are re-indexed (`python -m opspilot.cli reindex`).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("embedding", pgvector.sqlalchemy.Vector(768), nullable=True))
    op.add_column("chunks", sa.Column("embedding_model", sa.String(length=100), nullable=True))
    op.add_column(
        "chunks",
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', heading || ' ' || content)", persisted=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_chunks_search_vector", "chunks", ["search_vector"], unique=False, postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_search_vector", table_name="chunks")
    op.drop_index("ix_chunks_embedding_hnsw", table_name="chunks")
    op.drop_column("chunks", "search_vector")
    op.drop_column("chunks", "embedding_model")
    op.drop_column("chunks", "embedding")
