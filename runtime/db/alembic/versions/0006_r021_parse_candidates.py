"""R021/F021：招标解析候选与人工复核表（parse_candidates）

背景：F021 §2.7 要求"投标专员确认后才写入主卡/子卡和 RuleSet 草案"——解析器
（runtime/parsing/extractor.py）产出 RuleCandidate/MainCardCandidate 后暂存本表，
人工复核（确认/驳回/修正）后才写入 admission_data.rule_sets/requirements 与
public_data.field_traces；历史版本不可覆盖（RuleSet/Requirement 由现有唯一约束保证）。

表语义：
- candidate_id：候选 ID（如 ND-H-001-draft）
- material_id/version：来源材料（不可变版本引用）
- project_id：项目
- kind：rule_candidate（规则候选）/ main_card_field（主卡字段候选）
- payload：RuleCandidate/MainCardCandidate 完整 JSON（含 clause/page/assertion/rule）
- status：pending（待复核）/ approved（已确认）/ rejected（驳回）/ revised（修正后确认）
- reviewer/revised_payload/review_note/decided_at：复核留痕（F005 §8 人工修正留痕）
- 幂等：唯一键 (candidate_id, material_id, version)——同一材料版本不重复插入

Revision ID: 0006_r021_parse_candidates
Revises: 0005_performances_verified_at
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_r021_parse_candidates"
down_revision = "0005_performances_verified_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "parse_candidates",
        sa.Column("candidate_id", sa.String(128), primary_key=True),
        sa.Column("material_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),  # rule_candidate / main_card_field
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        # pending / approved / rejected / revised
        sa.Column("reviewer", sa.String(128)),
        sa.Column("revised_payload", sa.JSON()),
        sa.Column("review_note", sa.Text()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "candidate_id", "material_id", "version", name="uq_parse_candidates_idem"
        ),
        sa.Index("ix_parse_candidates_material", "material_id", "version"),
        sa.Index("ix_parse_candidates_project", "project_id"),
        sa.Index("ix_parse_candidates_status", "status"),
        schema="public_data",
    )


def downgrade() -> None:
    op.drop_table("parse_candidates", schema="public_data")
