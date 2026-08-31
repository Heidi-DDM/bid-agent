"""R018 初始迁移：analysis_jobs 任务表（F019 §3）

Revision ID: 0001_r018_analysis_jobs
Revises:
Create Date: 2026-08-31
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_r018_analysis_jobs"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_jobs",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("input_ref", sa.String(512), nullable=True),
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("idempotency_key", sa.String(512), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("runner_id", sa.String(64), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_analysis_jobs_idempotency_key"),
    )
    op.create_index("ix_analysis_jobs_status", "analysis_jobs", ["status"])
    op.create_index("ix_analysis_jobs_project_id", "analysis_jobs", ["project_id"])
    op.create_index(
        "ix_analysis_jobs_kind_input_project", "analysis_jobs", ["kind", "input_ref", "project_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_analysis_jobs_kind_input_project", table_name="analysis_jobs")
    op.drop_index("ix_analysis_jobs_project_id", table_name="analysis_jobs")
    op.drop_index("ix_analysis_jobs_status", table_name="analysis_jobs")
    op.drop_table("analysis_jobs")