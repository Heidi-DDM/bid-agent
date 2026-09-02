"""R022/F022 遗留补齐：performances 增加 verified_at 核验时间字段（F022 §2.3 / F006 §4.2）

背景：F019 §3 建表时 performances 缺 verified_at，导致 runtime 匹配执行器
build_enterprise_evidence 无法把业绩放入时点有效证据快照（_snapshot_eligible 要求
verified_at <= as_of），类似业绩客观项（NQ-S-003/004）在运行时恒判 unverifiable。
本迁移补齐该列；存量行置 NULL = 未核验，保持缺失阻断语义，不推断。

Revision ID: 0005_performances_verified_at
Revises: 0004_r025_knowledge
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_performances_verified_at"
down_revision = "0004_r025_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "performances",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        schema="enterprise_data",
    )


def downgrade() -> None:
    op.drop_column("performances", "verified_at", schema="enterprise_data")
