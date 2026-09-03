"""R022 企业资料字段口径补齐：managers.b_cert_no + cert_valid_until 可空

背景（2026-09-02 R022 开工侦察）：
- F007 §14 实测：注册建造师清单「无证书有效期/安全证书字段」；
  F022 §3 农大最小资料集要求项目经理含「注册证/B 证/无在建/社保」——
  Manager 模型此前缺 b_cert_no（安全生产考核合格证 B 证编号）列，
  匹配引擎 build_enterprise_evidence 无法把 B 证带给规则引擎（NQ 项目经理规则）。
- models.py Manager.cert_valid_until 原为 NOT NULL → 导入器被迫写占位日期
  （2100-12-31），违反「缺失标待补、不推断」强制规则（AGENTS.md 规则 1）。
  本迁移改 nullable=True：缺失 = NULL = 待补，由核验人补充证据后填值。

Revision ID: 0008_r022_manager_bcert
Revises: 0007_r021_confidence_enum
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_r022_manager_bcert"
down_revision = "0007_r021_confidence_enum"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "managers",
        sa.Column("b_cert_no", sa.String(128), nullable=True,
                  comment="安全生产考核合格证 B 证编号（缺失=NULL=待补）"),
        schema="enterprise_data",
    )
    # cert_valid_until NOT NULL → NULL（存量占位 2100-12-31 不回收，核验时人工修正）
    op.alter_column(
        "managers", "cert_valid_until",
        existing_type=sa.Date(),
        nullable=True,
        schema="enterprise_data",
    )


def downgrade() -> None:
    op.drop_column("managers", "b_cert_no", schema="enterprise_data")
    # 回退 NOT NULL 前须把 NULL 行补占位，否则约束失败
    op.execute(
        "UPDATE enterprise_data.managers SET cert_valid_until = '2100-12-31' "
        "WHERE cert_valid_until IS NULL"
    )
    op.alter_column(
        "managers", "cert_valid_until",
        existing_type=sa.Date(),
        nullable=False,
        schema="enterprise_data",
    )
