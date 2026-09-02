"""R019 初始迁移：数据库逻辑分层与持久化表（F019 §2/§3）

Revision ID: 0002_r019_data_persistence
Revises: 0001_r018_analysis_jobs
Create Date: 2026-08-31
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_r019_data_persistence"
down_revision = "0001_r018_analysis_jobs"
branch_labels = None
depends_on = None

SCHEMAS = ["public_data", "enterprise_data", "admission_data", "audit_data"]


def _create_schemas() -> None:
    for name in SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {name}")


def _drop_schemas() -> None:
    for name in reversed(SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


def upgrade() -> None:
    _create_schemas()

    # ---- public_data：公告/招标文件/项目主卡/事实子卡/公开条款引用（F019 §2）----
    op.create_table(
        "projects",
        sa.Column("project_id", sa.String(64), primary_key=True),
        sa.Column("project_name", sa.String(256), nullable=False),
        sa.Column("tender_document_ref", sa.String(64), nullable=True),
        sa.Column("admission_status", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="public_data",
    )
    op.create_index("ix_projects_admission_status", "projects", ["admission_status"], schema="public_data")

    op.create_table(
        "materials",
        sa.Column("material_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("material_type", sa.String(32), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("owner_type", sa.String(16), nullable=False),
        sa.Column("classification", sa.String(16), nullable=False),
        sa.Column("permission_scope", sa.String(24), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("parse_status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("evidence_refs", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=False),
        sa.Column("data_owner", sa.String(128), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("material_id", "version", name="pk_materials_id_version"),
        schema="public_data",
    )
    op.create_index("ix_materials_owner_type", "materials", ["owner_type"], schema="public_data")
    op.create_index("ix_materials_project_id", "materials", ["project_id"], schema="public_data")

    op.create_table(
        "material_versions",
        sa.Column("material_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("object_uri", sa.String(512), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("material_id", "version", name="pk_material_versions_id_version"),
        schema="public_data",
    )
    op.create_index("ix_material_versions_hash", "material_versions", ["content_hash"], schema="public_data")

    op.create_table(
        "field_traces",
        sa.Column("trace_id", sa.String(64), primary_key=True),
        sa.Column("object_id", sa.String(64), nullable=False),
        sa.Column("field_key", sa.String(128), nullable=False),
        sa.Column("clause", sa.Text(), nullable=False),
        sa.Column("source_link", sa.String(512), nullable=True),
        sa.Column("assertion", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="public_data",
    )
    op.create_index("ix_field_traces_object_field", "field_traces", ["object_id", "field_key"],
                    schema="public_data")

    # ---- enterprise_data：资质/业绩/人员/私有证据元数据（F019 §2，缺证据默认 pending_verification）----
    op.create_table(
        "evidence_files",
        sa.Column("evidence_id", sa.String(64), primary_key=True),
        sa.Column("material_id", sa.String(64), nullable=True),
        sa.Column("file_type", sa.String(32), nullable=False),
        sa.Column("object_uri", sa.String(512), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=True),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("classification", sa.String(16), nullable=False, server_default="internal"),
        sa.Column("uploaded_by", sa.String(128), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="enterprise_data",
    )
    op.create_index("ix_evidence_files_material_id", "evidence_files", ["material_id"],
                    schema="enterprise_data")

    op.create_table(
        "qualifications",
        sa.Column("qualification_id", sa.String(64), primary_key=True),
        sa.Column("material_id", sa.String(64), nullable=True),
        sa.Column("category", sa.String(128), nullable=False),
        sa.Column("level", sa.String(32), nullable=True),
        sa.Column("specialty", sa.String(128), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("issuer", sa.String(256), nullable=True),
        sa.Column("evidence_refs", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=False),
        sa.Column("data_owner", sa.String(128), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending_verification"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="enterprise_data",
    )
    op.create_index("ix_qualifications_category_status", "qualifications", ["category", "status"],
                    schema="enterprise_data")

    op.create_table(
        "performances",
        sa.Column("performance_id", sa.String(64), primary_key=True),
        sa.Column("material_id", sa.String(64), nullable=True),
        sa.Column("project_name", sa.String(256), nullable=False),
        sa.Column("project_type", sa.String(128), nullable=False),
        sa.Column("specialty", sa.String(128), nullable=True),
        sa.Column("contract_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("completed_at", sa.Date(), nullable=True),
        sa.Column("awarded_at", sa.Date(), nullable=True),
        sa.Column("owner_org", sa.String(256), nullable=True),
        sa.Column("scale_metrics", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=True),
        sa.Column("evidence_refs", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=False),
        sa.Column("data_owner", sa.String(128), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending_verification"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="enterprise_data",
    )
    op.create_index("ix_performances_project_type", "performances", ["project_type"],
                    schema="enterprise_data")

    op.create_table(
        "personnel",
        sa.Column("personnel_id", sa.String(64), primary_key=True),
        sa.Column("material_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("organization", sa.String(256), nullable=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("specialty", sa.String(128), nullable=True),
        sa.Column("cert_level", sa.String(32), nullable=True),
        sa.Column("cert_no", sa.String(128), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("issuer", sa.String(256), nullable=True),
        sa.Column("on_site", sa.String(16), nullable=True),
        sa.Column("on_site_project", sa.String(256), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("evidence_refs", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=False),
        sa.Column("data_owner", sa.String(128), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending_verification"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="enterprise_data",
    )
    op.create_index("ix_personnel_category_status", "personnel", ["category", "status"],
                    schema="enterprise_data")
    op.create_index("ix_personnel_name", "personnel", ["name"], schema="enterprise_data")

    op.create_table(
        "managers",
        sa.Column("manager_id", sa.String(64), primary_key=True),
        sa.Column("personnel_id", sa.String(64), nullable=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("organization", sa.String(256), nullable=False),
        sa.Column("specialty", sa.String(128), nullable=False),
        sa.Column("reg_cert_type", sa.String(32), nullable=False),
        sa.Column("reg_cert_no", sa.String(128), nullable=False),
        sa.Column("cert_level", sa.String(32), nullable=False),
        sa.Column("cert_valid_until", sa.Date(), nullable=False),
        sa.Column("edu_safety_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("eligible_project_types", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=False),
        sa.Column("region_restriction", sa.String(128), nullable=True),
        sa.Column("performance_refs", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=False),
        sa.Column("active_projects", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=True),
        sa.Column("expected_available_at", sa.Date(), nullable=True),
        sa.Column("availability", sa.String(16), nullable=False, server_default="available"),
        sa.Column("credit_penalty_status",
                  sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=True),
        sa.Column("evidence_refs", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_owner", sa.String(128), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending_verification"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="enterprise_data",
    )
    op.create_index("ix_managers_status", "managers", ["status"], schema="enterprise_data")
    op.create_index("ix_managers_specialty", "managers", ["specialty"], schema="enterprise_data")

    # ---- admission_data：规则快照/匹配运行/准入结果/审批/豁免（F019 §2）----
    op.create_table(
        "rule_sets",
        sa.Column("rule_set_id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("snapshot", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=False),
        sa.Column("diff", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "version", name="uq_rule_sets_project_version"),
        schema="admission_data",
    )
    op.create_index("ix_rule_sets_project_id", "rule_sets", ["project_id"], schema="admission_data")

    op.create_table(
        "requirements",
        sa.Column("requirement_id", sa.String(64), primary_key=True),
        sa.Column("rule_set_id", sa.String(64), nullable=False),
        sa.Column("req_type", sa.String(24), nullable=False),
        sa.Column("category", sa.String(128), nullable=True),
        sa.Column("lot_id", sa.String(64), nullable=True),
        sa.Column("clause_ref", sa.String(128), nullable=False),
        sa.Column("assertion", sa.Text(), nullable=False),
        sa.Column("rule", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("evidence_required", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=False),
        sa.Column("as_of", sa.String(64), nullable=True),
        sa.Column("missing_action", sa.String(24), nullable=False, server_default="blocked_missing_data"),
        sa.Column("failure_effect", sa.String(32), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=True),
        sa.Column("logic_group", sa.String(64), nullable=True),
        sa.Column("operator", sa.String(16), nullable=True),
        sa.Column("consortium_role", sa.String(16), nullable=False, server_default="none"),
        sa.Column("max_score", sa.Float(), nullable=True),
        sa.Column("weight", sa.Float(), nullable=True),
        sa.Column("score_nature", sa.String(16), nullable=True),
        sa.Column("score_formula", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=True),
        sa.Column("score_inputs", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=True),
        sa.Column("reuse_policy", sa.String(32), nullable=True),
        sa.Column("required_by_stage", sa.String(24), nullable=True),
        sa.Column("action_status", sa.String(24), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="admission_data",
    )
    op.create_index("ix_requirements_rule_set", "requirements", ["rule_set_id"], schema="admission_data")
    op.create_index("ix_requirements_req_type", "requirements", ["req_type"], schema="admission_data")

    op.create_table(
        "match_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("rule_set_id", sa.String(64), nullable=False),
        sa.Column("lot_id", sa.String(64), nullable=True),
        sa.Column("as_of", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False, server_default="gate"),
        sa.Column("coverage", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="completed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="admission_data",
    )
    op.create_index("ix_match_runs_project", "match_runs", ["project_id"], schema="admission_data")
    op.create_index("ix_match_runs_rule_set", "match_runs", ["rule_set_id"], schema="admission_data")

    op.create_table(
        "match_items",
        sa.Column("item_id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("requirement_id", sa.String(64), nullable=False),
        sa.Column("match_result", sa.String(24), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("max_score", sa.Float(), nullable=True),
        sa.Column("missing_items", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=True),
        sa.Column("match_reason", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=True),
        sa.Column("evidence_refs", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_version_refs", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="admission_data",
    )
    op.create_index("ix_match_items_run", "match_items", ["run_id"], schema="admission_data")
    op.create_index("ix_match_items_requirement", "match_items", ["requirement_id"], schema="admission_data")

    op.create_table(
        "admission_results",
        sa.Column("result_id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("lot_id", sa.String(64), nullable=True),
        sa.Column("rule_set_id", sa.String(64), nullable=False),
        sa.Column("qualification_result", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=False),
        sa.Column("scoring_result", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=False),
        sa.Column("operational_readiness",
                  sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("internal_admission_result",
                  sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("internal_admission_eligible", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("total_score", sa.Float(), nullable=True),
        sa.Column("max_total_score", sa.Float(), nullable=True),
        sa.Column("score_gap_items", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=True),
        sa.Column("blocked_items", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=True),
        sa.Column("pending_items", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=True),
        sa.Column("review_items", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=True),
        sa.Column("manager_matches", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=True),
        sa.Column("explanation", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=True),
        sa.Column("result_freshness", sa.String(16), nullable=False, server_default="current"),
        sa.Column("state", sa.String(16), nullable=False, server_default="final"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", name="uq_admission_results_run_id"),
        schema="admission_data",
    )
    op.create_index("ix_admission_results_project", "admission_results", ["project_id"],
                    schema="admission_data")

    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("approver", sa.String(128), nullable=False),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("admission_result_ref", sa.String(64), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="admission_data",
    )
    op.create_index("ix_approvals_project", "approvals", ["project_id"], schema="admission_data")

    op.create_table(
        "waivers",
        sa.Column("waiver_id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("authorizer", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_refs", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=False),
        sa.Column("valid_until", sa.Date(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("covered_items", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
                  nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="admission_data",
    )
    op.create_index("ix_waivers_project", "waivers", ["project_id"], schema="admission_data")

    # ---- audit_data：仅追加审计事件（F019 §2）----
    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("basis", sa.Text(), nullable=True),
        sa.Column("outcome", sa.String(256), nullable=True),
        sa.Column("object_ref", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="audit_data",
    )
    op.create_index("ix_audit_events_object_ref", "audit_events", ["object_ref"], schema="audit_data")
    op.create_index("ix_audit_events_actor", "audit_events", ["actor"], schema="audit_data")


def downgrade() -> None:
    for table, schema in [
        ("audit_events", "audit_data"),
        ("waivers", "admission_data"),
        ("approvals", "admission_data"),
        ("admission_results", "admission_data"),
        ("match_items", "admission_data"),
        ("match_runs", "admission_data"),
        ("requirements", "admission_data"),
        ("rule_sets", "admission_data"),
        ("personnel", "enterprise_data"),
        ("managers", "enterprise_data"),
        ("performances", "enterprise_data"),
        ("qualifications", "enterprise_data"),
        ("evidence_files", "enterprise_data"),
        ("field_traces", "public_data"),
        ("material_versions", "public_data"),
        ("materials", "public_data"),
        ("projects", "public_data"),
    ]:
        op.drop_table(table, schema=schema)
    _drop_schemas()