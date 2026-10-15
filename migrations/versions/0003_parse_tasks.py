"""Durable parse stage; existing documents and ingest jobs retain their meaning."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "parse_tasks",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), server_default="PENDING", nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("queued_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("deadline_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("job_id", name="pk_parse_tasks"),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name="fk_parse_tasks_job", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("execution_id", name="uq_parse_tasks_execution"),
        sa.CheckConstraint(
            "status IN ('PENDING','QUEUED','RUNNING','SUCCEEDED','FAILED')",
            name="ck_parse_tasks_status",
        ),
        sa.CheckConstraint(
            "(status = 'RUNNING' AND started_at IS NOT NULL AND deadline_at IS NOT NULL) OR status <> 'RUNNING'",
            name="ck_parse_tasks_running",
        ),
        sa.CheckConstraint(
            "(status IN ('SUCCEEDED','FAILED') AND finished_at IS NOT NULL) OR (status NOT IN ('SUCCEEDED','FAILED') AND finished_at IS NULL)",
            name="ck_parse_tasks_finished",
        ),
    )
    op.create_index("ix_parse_tasks_status_created", "parse_tasks", ["status", "created_at"])


def downgrade() -> None:
    op.drop_table("parse_tasks")
