"""F026 搜索预筛待选池与规则版本。

Revision ID: 0018_f026_discovery_pool
Revises: 0017_adr004_iteration1_workflow
Create Date: 2026-09-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_f026_discovery_pool"
down_revision = "0017_adr004_iteration1_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "selection_pool_items",
        sa.Column("pool_item_id", sa.String(64), primary_key=True),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("project_key", sa.String(512), nullable=False),
        sa.Column("pool_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("screening", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True)),
        sa.Column("dismissed_at", sa.DateTime(timezone=True)),
        sa.Column("dismissed_by", sa.String(128)),
        sa.Column("dismissed_reason", sa.Text()),
        sa.Column("replaced_by_candidate_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("candidate_id", name="uq_selection_pool_candidate"),
        schema="public_data",
    )
    op.create_index("ix_selection_pool_project_status", "selection_pool_items", ["project_key", "pool_status"], schema="public_data")
    op.create_index("ix_selection_pool_status_seen", "selection_pool_items", ["pool_status", "last_seen_at"], schema="public_data")
    op.create_table(
        "discovery_rule_profiles",
        sa.Column("rule_profile_id", sa.String(64), primary_key=True),
        sa.Column("version", sa.String(64), nullable=False, unique=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="admission_data",
    )
    op.create_index("ix_discovery_rule_profiles_active", "discovery_rule_profiles", ["is_active"], schema="admission_data")


def downgrade() -> None:
    op.drop_index("ix_discovery_rule_profiles_active", table_name="discovery_rule_profiles", schema="admission_data")
    op.drop_table("discovery_rule_profiles", schema="admission_data")
    op.drop_index("ix_selection_pool_status_seen", table_name="selection_pool_items", schema="public_data")
    op.drop_index("ix_selection_pool_project_status", table_name="selection_pool_items", schema="public_data")
    op.drop_table("selection_pool_items", schema="public_data")
