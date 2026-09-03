"""R022 OCR 复核状态列：evidence_files 增加 review 标记

背景（2026-09-02 R022-② 侦察）：
- F022 §2.6/§5：OCR 低置信度进入人工复核队列；「人工确认需填写修正值、
  依据和时间」；「OCR 结果包含 source_hash、页码、页面/字段置信度和复核状态」。
- 0002 建 evidence_files 只有 ocr_confidence/page_no，无复核状态列；
  ocr.review 端点此前只写审计、不改证据状态（review 形同虚设）。
- 本迁移补四列：review_status（pending_review/approved/rejected/revised）、
  reviewed_by、reviewed_at、review_note。存量行默认 NULL = 无需复核
  （文本 PDF 直接可用）；低置信 OCR 行由 worker 落 pending_review。

Revision ID: 0009_r022_evidence_review
Revises: 0008_r022_manager_bcert
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_r022_evidence_review"
down_revision = "0008_r022_manager_bcert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "evidence_files",
        sa.Column("review_status", sa.String(24), nullable=True,
                  comment="NULL=无需复核/文本直接可用；pending_review/approved/rejected/revised"),
        schema="enterprise_data",
    )
    op.add_column(
        "evidence_files",
        sa.Column("reviewed_by", sa.String(128), nullable=True),
        schema="enterprise_data",
    )
    op.add_column(
        "evidence_files",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        schema="enterprise_data",
    )
    op.add_column(
        "evidence_files",
        sa.Column("review_note", sa.Text(), nullable=True),
        schema="enterprise_data",
    )


def downgrade() -> None:
    for col in ("review_note", "reviewed_at", "reviewed_by", "review_status"):
        op.drop_column("evidence_files", col, schema="enterprise_data")
