"""候选公告详情初筛抽取存留（2026-09-09）。

import_detail 抓详情原文后，运行 announcement_prescreen 确定性抽取资质/人员/信用/
金额/地区/工期/标段等初审字段，以 JSON 存到候选表 detail_summary，
供候选卡/详情卡展示（有值才显示），不再对已入库候选显示"待详情页确认"。

Revision ID: 0012_candidate_detail_summary
Revises: 0011_search_job_soft_delete
Create Date: 2026-09-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_candidate_detail_summary"
down_revision = "0011_search_job_soft_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "announcement_candidates",
        sa.Column("detail_summary", sa.JSON(), nullable=True),
        schema="public_data",
    )


def downgrade() -> None:
    op.drop_column("announcement_candidates", "detail_summary", schema="public_data")