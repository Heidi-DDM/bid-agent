"""搜索任务历史软删除（保留候选事实与审计链）。

Revision ID: 0011_search_job_soft_delete
Revises: 0010_announcement_search
Create Date: 2026-09-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_search_job_soft_delete"
down_revision = "0010_announcement_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "analysis_jobs",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_analysis_jobs_deleted_at", "analysis_jobs", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_analysis_jobs_deleted_at", table_name="analysis_jobs")
    op.drop_column("analysis_jobs", "deleted_at")
