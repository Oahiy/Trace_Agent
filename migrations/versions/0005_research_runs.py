"""Durable research input, execution lease, shared budget and result."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "research_runs",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("workflow_version", sa.String(40), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("budget", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(none_as_null=True)),
        sa.Column("result", sa.JSON(none_as_null=True)),
        sa.Column("queued_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("deadline_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("job_id", name="pk_research_runs"),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name="fk_research_runs_job", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("id", name="uq_research_runs_id"),
        sa.UniqueConstraint("execution_id", name="uq_research_runs_execution"),
        sa.CheckConstraint(
            "(started_at IS NULL AND deadline_at IS NULL) OR "
            "(started_at IS NOT NULL AND deadline_at > started_at)",
            name="ck_research_runs_deadline",
        ),
        sa.CheckConstraint(
            "result IS NULL OR finished_at IS NOT NULL", name="ck_research_runs_result"
        ),
    )


def downgrade():
    op.drop_table("research_runs")
