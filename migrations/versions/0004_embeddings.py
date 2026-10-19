"""Versioned vectors and one explicit durable indexing execution per ingest job."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "embedding_profiles",
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("model_name", sa.String(100), nullable=False),
        sa.Column("model_digest", sa.String(64), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("preprocessing_version", sa.String(100), nullable=False),
        sa.Column("normalization", sa.String(100), nullable=False),
        sa.Column("ingest_version", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("version", name="pk_embedding_profiles"),
        sa.CheckConstraint("dimensions = 768", name="ck_embedding_profiles_dimensions"),
        sa.CheckConstraint("model_digest ~ '^[0-9a-f]{64}$'", name="ck_embedding_profiles_digest"),
    )
    op.create_table(
        "chunk_embeddings",
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("index_version", sa.String(32), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("embedding", VECTOR(768), nullable=False),
        sa.PrimaryKeyConstraint("chunk_id", "index_version", name="pk_chunk_embeddings"),
        sa.ForeignKeyConstraint(
            ["chunk_id"], ["chunks.id"], name="fk_chunk_embeddings_chunk", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["index_version"], ["embedding_profiles.version"], name="fk_chunk_embeddings_profile"
        ),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_chunk_embeddings_hash"),
        sa.CheckConstraint("vector_norm(embedding) > 0", name="ck_chunk_embeddings_nonzero"),
    )
    op.create_table(
        "index_tasks",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("index_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("budget", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("job_id", name="pk_index_tasks"),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name="fk_index_tasks_job", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["index_version"], ["embedding_profiles.version"], name="fk_index_tasks_profile"
        ),
        sa.UniqueConstraint("execution_id", name="uq_index_tasks_execution"),
        sa.CheckConstraint(
            "status IN ('RUNNING','SUCCEEDED','FAILED')", name="ck_index_tasks_status"
        ),
        sa.CheckConstraint("deadline_at > started_at", name="ck_index_tasks_deadline"),
        sa.CheckConstraint(
            "(status = 'RUNNING' AND finished_at IS NULL) OR (status IN ('SUCCEEDED','FAILED') AND finished_at IS NOT NULL)",
            name="ck_index_tasks_finished",
        ),
    )


def downgrade():
    op.drop_table("index_tasks")
    op.drop_table("chunk_embeddings")
    op.drop_table("embedding_profiles")
