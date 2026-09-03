"""R004/F020：真实公告搜索落地

- announcement_candidates（public_data）：manual_trigger 搜索结果候选暂存，
  投标专员确认后由 announcement.import_detail 任务抓详情原文入库
  （region/publish_date 列表页不标注 → NULL=待补，不推断填充）。
- analysis_jobs.result_summary（JSONB）：announcement.search 逐源结果摘要
  [{source_id,status,count,note}]，GET 轮询如实展示（限频/robots 拒绝不静默）。

Revision ID: 0010_announcement_search
Revises: 0009_r022_evidence_review
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010_announcement_search"
down_revision = "0009_r022_evidence_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "announcement_candidates",
        sa.Column("candidate_id", sa.String(64), primary_key=True),
        sa.Column("search_job_id", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(32), nullable=False),
        sa.Column("source_name", sa.String(64), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("publish_date", sa.Date(), nullable=True),
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("import_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("import_job_id", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="public_data",
    )
    op.create_index("ix_announcement_candidates_job", "announcement_candidates",
                    ["search_job_id"], schema="public_data")
    op.create_index("ix_announcement_candidates_project", "announcement_candidates",
                    ["project_id"], schema="public_data")
    op.create_index("ix_announcement_candidates_import", "announcement_candidates",
                    ["import_status"], schema="public_data")
    op.add_column(
        "analysis_jobs",
        sa.Column("result_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("analysis_jobs", "result_summary")
    op.drop_index("ix_announcement_candidates_import", table_name="announcement_candidates",
                  schema="public_data")
    op.drop_index("ix_announcement_candidates_project", table_name="announcement_candidates",
                  schema="public_data")
    op.drop_index("ix_announcement_candidates_job", table_name="announcement_candidates",
                  schema="public_data")
    op.drop_table("announcement_candidates", schema="public_data")