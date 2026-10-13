"""API key digests and real file references; preserve Day 3 metadata-only rows."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("api_key_digest", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_users_api_key_digest", "users", ["api_key_digest"])
    op.create_check_constraint(
        "ck_users_api_key_digest", "users", "api_key_digest ~ '^[0-9a-f]{64}$'"
    )
    op.add_column("documents", sa.Column("storage_key", sa.String(32), nullable=True))
    op.add_column("documents", sa.Column("file_size_bytes", sa.Integer(), nullable=True))
    op.add_column("documents", sa.Column("file_media_type", sa.String(32), nullable=True))
    op.create_unique_constraint("uq_documents_storage_key", "documents", ["storage_key"])
    op.create_check_constraint(
        "ck_documents_stored_file",
        "documents",
        "(storage_key IS NULL AND file_size_bytes IS NULL AND file_media_type IS NULL) OR "
        "(storage_key IS NOT NULL AND file_size_bytes IS NOT NULL "
        "AND file_media_type IS NOT NULL AND storage_key ~ '^[0-9a-f]{32}$' "
        "AND file_size_bytes BETWEEN 1 AND 5242880 "
        "AND file_media_type IN ('text/plain','application/pdf'))",
    )


def downgrade() -> None:
    op.drop_constraint("ck_documents_stored_file", "documents", type_="check")
    op.drop_constraint("uq_documents_storage_key", "documents", type_="unique")
    for column in ("file_media_type", "file_size_bytes", "storage_key"):
        op.drop_column("documents", column)
    op.drop_constraint("ck_users_api_key_digest", "users", type_="check")
    op.drop_constraint("uq_users_api_key_digest", "users", type_="unique")
    op.drop_column("users", "api_key_digest")
