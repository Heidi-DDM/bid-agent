"""R020 修复：approvals.state 列宽不足（blocked_waiver_expired 22 字符）

Revision ID: 0003_r020_approval_state_width
Revises: 0002_r019_data_persistence
Create Date: 2026-08-31
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_r020_approval_state_width"
down_revision = "0002_r019_data_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "approvals",
        "state",
        existing_type=sa.String(16),
        type_=sa.String(32),
        existing_nullable=False,
        existing_server_default="pending",
        schema="admission_data",
    )


def downgrade() -> None:
    op.alter_column(
        "approvals",
        "state",
        existing_type=sa.String(32),
        type_=sa.String(16),
        existing_nullable=False,
        existing_server_default="pending",
        schema="admission_data",
    )