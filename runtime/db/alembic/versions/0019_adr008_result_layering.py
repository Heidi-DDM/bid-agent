"""ADR-008 / docs/12: result layering — run_context, candidate plans, parse exceptions,
operation tasks, remediation gap keys, domain summaries, historical sample context.

Revision ID: 0019_adr008_result_layering
Revises: 0018_f026_discovery_pool
Create Date: 2026-09-28

向后兼容扩展（docs/12 §5.1）：只新增列与表，不改写既有 match_items 行；
历史行的 domain/reason_code 为 NULL，投影层按 legacy_ambiguous 处理（不猜测）。
"""
from alembic import op
import sqlalchemy as sa

revision = "0019_adr008_result_layering"
down_revision = "0018_f026_discovery_pool"
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")

ADMISSION = "admission_data"
PUBLIC = "public_data"


def upgrade() -> None:
    # ---- projects：服务端测试上下文（历史解析样本，docs/12 §1.2/§7） ----
    op.add_column("projects", sa.Column("test_context", JSONB, nullable=True), schema=PUBLIC)

    # ---- requirements：工作域与判定口径（docs/12 §3.1） ----
    for col, coltype in [
        ("domain", sa.String(16)),
        ("decision_scope", sa.String(20)),
        ("score_classification", sa.String(24)),
        ("comparison_status", sa.String(28)),
        ("person_binding_policy", sa.String(32)),
        ("role_code", sa.String(32)),
        ("task_kind", sa.String(24)),
    ]:
        op.add_column("requirements", sa.Column(col, coltype, nullable=True), schema=ADMISSION)
    op.add_column("requirements", sa.Column("role_count", sa.Integer(), nullable=True), schema=ADMISSION)

    # ---- match_runs：run_context / 输入幂等指纹 / is_current（docs/12 §3.5） ----
    op.add_column("match_runs", sa.Column("run_context", JSONB, nullable=True), schema=ADMISSION)
    op.add_column("match_runs", sa.Column("run_input_fingerprint", sa.String(64), nullable=True), schema=ADMISSION)
    op.add_column("match_runs", sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()), schema=ADMISSION)
    op.create_index("ix_match_runs_project_current", "match_runs", ["project_id", "is_current"], schema=ADMISSION)
    op.create_index("ix_match_runs_input_fingerprint", "match_runs", ["run_input_fingerprint"], schema=ADMISSION)

    # ---- match_items：事实层（docs/12 §3.2） ----
    for col, coltype in [
        ("domain", sa.String(16)),
        ("reason_code", sa.String(48)),
        ("evidence_state", sa.String(32)),
        ("observed_value", JSONB),
        ("required_value", JSONB),
        ("candidate_plan_id", sa.String(64)),
        ("person_id", sa.String(64)),
        ("next_action", sa.String(48)),
        ("score_classification", sa.String(24)),
        ("task_kind", sa.String(24)),
    ]:
        op.add_column("match_items", sa.Column(col, coltype, nullable=True), schema=ADMISSION)
    op.create_index("ix_match_items_run_domain_result", "match_items", ["run_id", "domain", "match_result"], schema=ADMISSION)
    op.create_index("ix_match_items_candidate_plan", "match_items", ["candidate_plan_id"], schema=ADMISSION)

    # ---- admission_results：分域汇总 + 稳定阻断原因（docs/12 §4.4/§5.2） ----
    for col in ["qualification_summary", "scoring_summary", "operation_summary", "parse_quality_summary", "blocking_reasons"]:
        op.add_column("admission_results", sa.Column(col, JSONB, nullable=True), schema=ADMISSION)

    # ---- remediation_tasks：稳定聚合键（docs/12 §3.4：同一缺口合并任务、保留逐条回链） ----
    for col, coltype in [
        ("gap_key", sa.String(64)),
        ("requirement_refs", JSONB),
        ("clause_refs", JSONB),
        ("lot_id", sa.String(64)),
        ("source_run_id", sa.String(64)),
    ]:
        op.add_column("remediation_tasks", sa.Column(col, coltype, nullable=True), schema=ADMISSION)
    op.create_index("ix_remediation_tasks_gap", "remediation_tasks", ["project_id", "lot_id", "gap_key"], schema=ADMISSION)

    # ---- candidate_plans 三表（docs/12 §3.3） ----
    op.create_table(
        "candidate_plans",
        sa.Column("candidate_plan_id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("lot_id", sa.String(64), nullable=True),
        sa.Column("role_code", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("selection_basis", sa.String(128), nullable=True),
        sa.Column("gap_reason_codes", JSONB, nullable=True),
        sa.Column("selected_by", sa.String(128), nullable=True),
        sa.Column("selected_reason", sa.Text(), nullable=True),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=ADMISSION,
    )
    op.create_index("ix_candidate_plans_run", "candidate_plans", ["run_id"], schema=ADMISSION)
    op.create_index("ix_candidate_plans_project", "candidate_plans", ["project_id"], schema=ADMISSION)

    op.create_table(
        "candidate_plan_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("candidate_plan_id", sa.String(64), nullable=False),
        sa.Column("role_code", sa.String(32), nullable=False),
        sa.Column("person_id", sa.String(64), nullable=False),
        sa.Column("person_kind", sa.String(16), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("availability_as_of", sa.String(64), nullable=True),
        sa.Column("evidence_refs", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("candidate_plan_id", "role_code", "person_id", name="uq_cpm_plan_role_person"),
        schema=ADMISSION,
    )
    op.create_index("ix_cpm_plan", "candidate_plan_members", ["candidate_plan_id"], schema=ADMISSION)
    op.create_index("ix_cpm_person", "candidate_plan_members", ["person_id"], schema=ADMISSION)

    op.create_table(
        "candidate_plan_requirement_links",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("candidate_plan_id", sa.String(64), nullable=False),
        sa.Column("requirement_id", sa.String(64), nullable=False),
        sa.Column("person_id", sa.String(64), nullable=True),
        sa.Column("result", sa.String(24), nullable=True),
        sa.Column("reason_code", sa.String(48), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=ADMISSION,
    )
    op.create_index("ix_cprl_plan", "candidate_plan_requirement_links", ["candidate_plan_id"], schema=ADMISSION)
    op.create_index("ix_cprl_requirement", "candidate_plan_requirement_links", ["requirement_id"], schema=ADMISSION)

    # ---- parse_exceptions（docs/12 §3.4：解析质量独立对象，处置审计结构化） ----
    op.create_table(
        "parse_exceptions",
        sa.Column("parse_exception_id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("material_id", sa.String(64), nullable=False),
        sa.Column("material_version", sa.Integer(), nullable=True),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("snapshot_version", sa.String(64), nullable=False),
        sa.Column("exception_type", sa.String(32), nullable=False),
        sa.Column("risk_rank", sa.Integer(), nullable=False),
        sa.Column("req_type", sa.String(24), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("payload", JSONB, nullable=True),
        sa.Column("decision", sa.String(24), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("search_scope", sa.Text(), nullable=True),
        sa.Column("page_refs", JSONB, nullable=True),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_history", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("snapshot_version", "candidate_id", name="uq_parse_exceptions_version_candidate"),
        schema=ADMISSION,
    )
    op.create_index("ix_parse_exceptions_project", "parse_exceptions", ["project_id", "status"], schema=ADMISSION)

    # ---- operation_tasks（docs/12 §2.4/§3.4：动作任务独立于缺证队列） ----
    op.create_table(
        "operation_tasks",
        sa.Column("operation_task_id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("action_requirement_id", sa.String(64), nullable=False),
        sa.Column("lot_id", sa.String(64), nullable=True),
        sa.Column("task_kind", sa.String(24), nullable=False),
        sa.Column("required_by_stage", sa.String(24), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("clause_ref", sa.String(128), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_role", sa.String(32), nullable=False),
        sa.Column("owner", sa.String(128), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("receipt_evidence_refs", JSONB, nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by", sa.String(128), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "action_requirement_id", name="uq_operation_tasks_project_req"),
        schema=ADMISSION,
    )
    op.create_index("ix_operation_tasks_project_stage", "operation_tasks", ["project_id", "required_by_stage"], schema=ADMISSION)


def downgrade() -> None:
    # 可行部分的降级：新表可整表删除；新列可安全移除（未改写历史行）。
    op.drop_index("ix_operation_tasks_project_stage", table_name="operation_tasks", schema=ADMISSION)
    op.drop_table("operation_tasks", schema=ADMISSION)
    op.drop_index("ix_parse_exceptions_project", table_name="parse_exceptions", schema=ADMISSION)
    op.drop_table("parse_exceptions", schema=ADMISSION)
    op.drop_index("ix_cprl_requirement", table_name="candidate_plan_requirement_links", schema=ADMISSION)
    op.drop_index("ix_cprl_plan", table_name="candidate_plan_requirement_links", schema=ADMISSION)
    op.drop_table("candidate_plan_requirement_links", schema=ADMISSION)
    op.drop_index("ix_cpm_person", table_name="candidate_plan_members", schema=ADMISSION)
    op.drop_index("ix_cpm_plan", table_name="candidate_plan_members", schema=ADMISSION)
    op.drop_table("candidate_plan_members", schema=ADMISSION)
    op.drop_index("ix_candidate_plans_project", table_name="candidate_plans", schema=ADMISSION)
    op.drop_index("ix_candidate_plans_run", table_name="candidate_plans", schema=ADMISSION)
    op.drop_table("candidate_plans", schema=ADMISSION)
    op.drop_index("ix_remediation_tasks_gap", table_name="remediation_tasks", schema=ADMISSION)
    for col in ["source_run_id", "lot_id", "clause_refs", "requirement_refs", "gap_key"]:
        op.drop_column("remediation_tasks", col, schema=ADMISSION)
    for col in ["blocking_reasons", "parse_quality_summary", "operation_summary", "scoring_summary", "qualification_summary"]:
        op.drop_column("admission_results", col, schema=ADMISSION)
    op.drop_index("ix_match_items_candidate_plan", table_name="match_items", schema=ADMISSION)
    op.drop_index("ix_match_items_run_domain_result", table_name="match_items", schema=ADMISSION)
    for col in ["task_kind", "score_classification", "next_action", "person_id", "candidate_plan_id",
                "required_value", "observed_value", "evidence_state", "reason_code", "domain"]:
        op.drop_column("match_items", col, schema=ADMISSION)
    op.drop_index("ix_match_runs_input_fingerprint", table_name="match_runs", schema=ADMISSION)
    op.drop_index("ix_match_runs_project_current", table_name="match_runs", schema=ADMISSION)
    op.drop_column("match_runs", "is_current", schema=ADMISSION)
    op.drop_column("match_runs", "run_input_fingerprint", schema=ADMISSION)
    op.drop_column("match_runs", "run_context", schema=ADMISSION)
    op.drop_column("requirements", "role_count", schema=ADMISSION)
    for col in ["task_kind", "role_code", "person_binding_policy", "comparison_status",
                "score_classification", "decision_scope", "domain"]:
        op.drop_column("requirements", col, schema=ADMISSION)
    op.drop_column("projects", "test_context", schema=PUBLIC)
