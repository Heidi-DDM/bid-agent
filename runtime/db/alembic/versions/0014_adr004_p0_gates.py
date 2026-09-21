"""ADR-004 Iteration 0 P0 门禁：worker 进程心跳、项目身份校验、投标截止时间。

背景（2026-09-16 完整优化方案 §1.2 三个 P0）：
- P0-01 worker 无进程级心跳、/readyz 不反映异步能力 → 新增 worker_heartbeats（public schema，
  与 analysis_jobs 同级运行时表），worker 每轮 upsert，/readyz 以最近心跳判定 worker 可用；
- P0-02 公告/招标文件/规则/结果可能串档 → 新增 public_data.project_identities，落库身份校验
  三态与冲突明细，identity_conflict 阻断正式匹配与审批；
- P0-03 已过截止项目仍可重算/审批 → projects 增 bid_deadline（缺失=NULL=待补，不推断），
  已过 → overdue，禁止新建匹配/重算/审批。

Revision ID: 0014_adr004_p0_gates
Revises: 0013_r025_reranker_audit
Create Date: 2026-09-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0014_adr004_p0_gates"
down_revision = "0013_r025_reranker_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- P0-01：worker 进程级心跳 ----
    op.create_table(
        "worker_heartbeats",
        sa.Column("runner_id", sa.String(64), primary_key=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("poll_interval_seconds", sa.Float(), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("hostname", sa.String(128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_worker_heartbeats_heartbeat_at", "worker_heartbeats", ["heartbeat_at"])

    # ---- P0-03：项目投标截止时间 ----
    op.add_column(
        "projects",
        sa.Column("bid_deadline", sa.Date(), nullable=True,
                  comment="投标截止的日期级原文事实（仅有日期时保留日期级不确定性；缺失=NULL=待补，不得由 as_of 推断）；已过 → overdue"),
        schema="public_data",
    )

    # ---- P0-02：项目身份校验 ----
    op.create_table(
        "project_identities",
        sa.Column("project_id", sa.String(64), primary_key=True),
        sa.Column("normalized_project_name", sa.String(256), nullable=True),
        sa.Column("purchaser", sa.String(256), nullable=True),
        sa.Column("location", sa.String(128), nullable=True),
        sa.Column("project_type", sa.String(64), nullable=True),
        sa.Column("announcement_no", sa.String(128), nullable=True),
        sa.Column("procurement_no", sa.String(128), nullable=True),
        sa.Column("lot_id", sa.String(64), nullable=True),
        sa.Column("budget_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("bid_deadline", sa.Date(), nullable=True),
        sa.Column("identity_status", sa.String(24), nullable=False,
                  server_default="identity_warning"),
        sa.Column("identity_conflicts", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("identity_warnings", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("compared_fields", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("expected_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("actual_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("source_refs", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("checked_by", sa.String(128), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("confirmed_by", sa.String(128), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        schema="public_data",
    )
    op.create_index("ix_project_identities_status", "project_identities",
                    ["identity_status"], schema="public_data")


def downgrade() -> None:
    op.drop_index("ix_project_identities_status", table_name="project_identities",
                  schema="public_data")
    op.drop_table("project_identities", schema="public_data")
    op.drop_column("projects", "bid_deadline", schema="public_data")
    op.drop_index("ix_worker_heartbeats_heartbeat_at", table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
