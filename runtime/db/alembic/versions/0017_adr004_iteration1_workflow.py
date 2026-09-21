"""ADR-004 Iteration 1 workflow: prescreen, preparation, remediation tasks.

Revision ID: 0017_adr004_iteration1_workflow
Revises: 0016_adr004_deadline_comment
Create Date: 2026-09-17

The tables keep early preparation decisions and issue handling separate from
formal admission/approval. They contain no automatic bid or pricing path.
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_adr004_iteration1_workflow"
down_revision = "0016_adr004_deadline_comment"
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "quick_prescreen_runs",
        sa.Column("prescreen_id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.Column("disclaimer", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="admission_data",
    )
    op.create_index("ix_quick_prescreen_runs_project", "quick_prescreen_runs", ["project_id"], schema="admission_data")

    op.create_table(
        "preparation_records",
        sa.Column("preparation_id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="admission_data",
    )
    op.create_index("ix_preparation_records_project_state", "preparation_records", ["project_id", "state"], schema="admission_data")

    op.create_table(
        "remediation_tasks",
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("task_type", sa.String(40), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(128), nullable=False),
        sa.Column("source_fingerprint", sa.String(128), nullable=False),
        sa.Column("source_match_run_id", sa.String(64), nullable=True),
        sa.Column("requirement_id", sa.String(64), nullable=True),
        sa.Column("assignee_role", sa.String(32), nullable=False),
        sa.Column("assignee", sa.String(128), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_required", JSONB, nullable=False),
        sa.Column("evidence_refs", JSONB, nullable=False),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("closing_condition", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("closed_by", sa.String(128), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_fingerprint", name="uq_remediation_tasks_source_fingerprint"),
        schema="admission_data",
    )
    op.create_index("ix_remediation_tasks_project_state", "remediation_tasks", ["project_id", "state"], schema="admission_data")
    op.create_index("ix_remediation_tasks_assignee_state", "remediation_tasks", ["assignee_role", "state"], schema="admission_data")


def downgrade() -> None:
    op.drop_index("ix_remediation_tasks_assignee_state", table_name="remediation_tasks", schema="admission_data")
    op.drop_index("ix_remediation_tasks_project_state", table_name="remediation_tasks", schema="admission_data")
    op.drop_table("remediation_tasks", schema="admission_data")
    op.drop_index("ix_preparation_records_project_state", table_name="preparation_records", schema="admission_data")
    op.drop_table("preparation_records", schema="admission_data")
    op.drop_index("ix_quick_prescreen_runs_project", table_name="quick_prescreen_runs", schema="admission_data")
    op.drop_table("quick_prescreen_runs", schema="admission_data")
