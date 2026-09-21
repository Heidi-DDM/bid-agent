"""ADR-004 P0 查漏补缺：带时区的精确投标截止时点。

保留 0014 的 bid_deadline（日期）以兼容历史及原文仅给日期的情形；新增
bid_deadline_at 仅在原文明确时分秒时赋值，避免迁移回填伪造精确时点。

Revision ID: 0015_adr004_deadline_timestamp
Revises: 0014_adr004_p0_gates
Create Date: 2026-09-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_adr004_deadline_timestamp"
down_revision = "0014_adr004_p0_gates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("bid_deadline_at", sa.DateTime(timezone=True), nullable=True,
                  comment="原文明确时分秒的投标截止时点；NULL=仅日期/待补"), schema="public_data")
    op.add_column("project_identities", sa.Column("bid_deadline_at", sa.DateTime(timezone=True), nullable=True,
                  comment="身份校验提取的精确截止时点；NULL=仅日期/待补"), schema="public_data")


def downgrade() -> None:
    op.drop_column("project_identities", "bid_deadline_at", schema="public_data")
    op.drop_column("projects", "bid_deadline_at", schema="public_data")
