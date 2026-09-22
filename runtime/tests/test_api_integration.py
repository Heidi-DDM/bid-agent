# F020 §7：R020 集成测试（真实 PostgreSQL，integration 标记）
# 覆盖：解析完成只触发一次首次匹配、重算幂等、非满分拒绝创建审批、
# 驳回 comment 必填、豁免过期回阻断、越权审计留痕。
#
# 运行方式（沙盒外终端）：
#   source .venv/bin/activate
#   export DATABASE_URL="postgresql+psycopg://bid_agent:bid_agent_dev@127.0.0.1:5432/bid_agent"
#   python -m pytest runtime/tests/test_api_integration.py -m integration -v
import os

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from runtime.core import orchestration, rbac
from runtime.core.errors import ApiError
from runtime.db import api_service, worker_service
from runtime.db.models import (
    AdmissionResult,
    AnalysisJob,
    Approval,
    AuditEvent,
    Material,
    MatchRun,
    Project,
    Waiver,
)

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("DATABASE_URL", "")
from runtime.tests._dbguard import integration_db_allowed
_DB_OK, _DB_WHY = integration_db_allowed()
needs_db = pytest.mark.skipif(not _DB_OK, reason=_DB_WHY)



@pytest.fixture()
def session():
    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE analysis_jobs, public_data.projects, public_data.materials, "
                "public_data.material_versions, admission_data.match_runs, "
                "admission_data.admission_results, admission_data.approvals, "
                "admission_data.waivers, audit_data.audit_events RESTART IDENTITY CASCADE"
            )
        )
    with Session(engine) as s:
        yield s


def _setup_project_with_tender(session, project_id="ND-2025") -> None:
    api_service.create_project(session, project_id=project_id, project_name="农大项目", actor="测试")
    # 招标文件已解析（parse_status=parsed）
    material = Material(
        material_id="MAT-ND-001", version=1, material_type="tender_document",
        source_type="uploaded", owner_type="public", classification="public",
        permission_scope="public_read", content_hash="a" * 64, parse_status="parsed",
        status="active", evidence_refs=[], data_owner="甲", project_id=project_id,
    )
    session.add(material)
    session.commit()


def _count_jobs(session, kind: str) -> int:
    return len(session.scalars(select(AnalysisJob).where(AnalysisJob.kind == kind)).all())


def _make_match_run(session, run_id: str, project_id: str = "ND-2025") -> None:
    """构造带 RAG 快照的匹配运行（F024 §2 门禁：创建审批前 MatchRun 必须携带
    retrieval_run_id / evidence_snapshot_hash / candidate_chunk_ids）。"""
    session.add(MatchRun(
        run_id=run_id, project_id=project_id, rule_set_id="RS-1",
        as_of="2025-10-30T00:00:00+08:00", mode="gate", status="completed",
        retrieval_run_id=f"rr-{run_id.lower()}",
        index_version="bge-m3-local:abcdefabcdef12:v1",
        candidate_chunk_ids=["CH-RAG-1", "CH-RAG-2"],
        structured_verification={"status": "verified", "verified_at": "2025-10-30T00:00:00+08:00"},
        evidence_snapshot_hash="e" * 64,
    ))
    session.commit()


# ---------- 任务编排（F020 §2.1/§7） ----------

@needs_db
def test_parse_completed_triggers_first_match_once(session):
    _setup_project_with_tender(session)
    # 解析完成 -> 自动触发一次 match.run
    job, created = worker_service.create_job(
        session, kind="match.run", input_ref="ND-2025", project_id="ND-2025"
    )
    assert created is True
    # 再次触发（幂等键去重）-> 不产生第二个任务
    job2, created2 = worker_service.create_job(
        session, kind="match.run", input_ref="ND-2025", project_id="ND-2025"
    )
    assert created2 is False
    assert job2.job_id == job.job_id
    assert _count_jobs(session, "match.run") == 1


@needs_db
def test_should_trigger_first_match_requires_parsed(session):
    _setup_project_with_tender(session)
    materials = [{"material_type": "tender_document", "parse_status": "pending"}]
    assert orchestration.should_trigger_first_match(materials, 0) is False
    materials[0]["parse_status"] = "parsed"
    assert orchestration.should_trigger_first_match(materials, 0) is True
    assert orchestration.should_trigger_first_match(materials, 1) is False


@needs_db
def test_recalculate_idempotent_by_evidence_version(session):
    _setup_project_with_tender(session)
    ev = "evidence:ev-2025-09-01"
    job, created = worker_service.create_job(
        session, kind="match.recalculate",
        input_ref=ev, project_id="ND-2025",
    )
    assert created is True
    # 同一证据版本重复调用 -> 既有任务（created=False，job_id 不变）
    job2, created2 = worker_service.create_job(
        session, kind="match.recalculate",
        input_ref=ev, project_id="ND-2025",
    )
    assert created2 is False
    assert job2.job_id == job.job_id
    existing = session.scalar(
        select(AnalysisJob).where(
            AnalysisJob.idempotency_key == f"match.recalculate:{ev}:ND-2025"
        )
    )
    assert existing is not None
    assert existing.job_id == job.job_id


# ---------- 审批（F020 §2.2.6） ----------

@needs_db
def test_approval_create_denied_without_full_score(session):
    _setup_project_with_tender(session)
    # 无匹配结果（无满分）-> 拒绝创建审批
    with pytest.raises(ApiError) as excinfo:
        api_service.create_approval(
            session, project_id="ND-2025", role=rbac.BUSINESS_HEAD, actor="经营负责人"
        )
    assert excinfo.value.code == "invalid_state_transition"
    # 审计留痕
    events = session.scalars(select(AuditEvent)).all()
    assert any(e.action == "approval.create_denied" for e in events)


@needs_db
def test_approval_create_with_full_score(session):
    _setup_project_with_tender(session)
    _make_match_run(session, "RUN-1")  # RAG 快照门禁（F024 §2）
    result = AdmissionResult(
        result_id="AR-1", run_id="RUN-1", project_id="ND-2025",
        rule_set_id="RS-1", qualification_result={"status": "passed"},
        scoring_result={}, operational_readiness={},
        internal_admission_result={}, internal_admission_eligible=True,
        state="final",
    )
    session.add(result)
    session.commit()
    data = api_service.create_approval(
        session, project_id="ND-2025", role=rbac.BUSINESS_HEAD, actor="经营负责人"
    )
    assert data["state"] == "pending_bid_approval"


@needs_db
def test_approval_reject_requires_comment(session):
    _setup_project_with_tender(session)
    _make_match_run(session, "RUN-2")  # RAG 快照门禁（F024 §2）
    result = AdmissionResult(
        result_id="AR-2", run_id="RUN-2", project_id="ND-2025",
        rule_set_id="RS-1", qualification_result={"status": "passed"},
        scoring_result={}, operational_readiness={},
        internal_admission_result={}, internal_admission_eligible=True,
        state="final",
    )
    session.add(result)
    session.commit()
    api_service.create_approval(session, project_id="ND-2025",
                                role=rbac.BUSINESS_HEAD, actor="经营负责人")
    with pytest.raises(ApiError) as excinfo:
        api_service.decide_approval(
            session, project_id="ND-2025", role=rbac.BUSINESS_HEAD,
            actor="经营负责人", decision="rejected", comment="",
        )
    assert excinfo.value.code == "invalid_request"


@needs_db
def test_waiver_expiry_blocks_approval(session):
    _setup_project_with_tender(session)
    _make_match_run(session, "RUN-3")  # RAG 快照门禁（F024 §2）
    result = AdmissionResult(
        result_id="AR-3", run_id="RUN-3", project_id="ND-2025",
        rule_set_id="RS-1", qualification_result={"status": "passed"},
        scoring_result={}, operational_readiness={},
        internal_admission_result={}, internal_admission_eligible=True,
        state="final",
    )
    session.add(result)
    session.commit()
    api_service.create_approval(session, project_id="ND-2025",
                                role=rbac.BUSINESS_HEAD, actor="经营负责人")
    # 登记已过期的豁免
    api_service.add_waiver(
        session, project_id="ND-2025", role=rbac.BUSINESS_HEAD, actor="经营负责人",
        reason="材料在途", evidence_refs=["E-1"], valid_until="2000-01-01",
        covered_items=["NQ-H-007"],
    )
    expired = api_service.expire_waivers(session, "ND-2025")
    assert expired == 1
    approval = session.scalar(select(Approval).where(Approval.project_id == "ND-2025"))
    assert approval.state == "blocked_waiver_expired"
    waiver = session.scalar(select(Waiver).where(Waiver.project_id == "ND-2025"))
    assert waiver.state == "expired"


# ---------- 越权审计（F020 §5） ----------

@needs_db
def test_rbac_denial_writes_audit(session):
    _setup_project_with_tender(session)
    material = session.scalar(select(Material).where(Material.material_id == "MAT-ND-001"))
    # 审批层数据：投标专员不可读（F003 §6.2 / F026 §5），验证越权拒绝 + 审计留痕
    material.permission_scope = "approver_only"
    session.commit()
    with pytest.raises(ApiError) as excinfo:
        api_service.require_scope_or_403(session, "bid_specialist", material)
    assert excinfo.value.code == "forbidden"
    events = session.scalars(select(AuditEvent)).all()
    assert any(e.action.startswith("rbac.deny") for e in events)


@needs_db
def test_two_role_scope_contract(session):
    """F026 §5 两级角色数据分层契约（真库回归）。

    - 投标专员（含其 Agent）承接原 data_admin 职责：可读企业私有资料层；
    - 历史 data_admin/legal 为兼容别名，归并为投标专员，权限一致；
    - 审批层（approver_only）仍仅经营负责人可读。
    """
    assert rbac.can_read_scope("bid_specialist", "enterprise_read") is True
    assert rbac.can_read_scope("data_admin", "enterprise_read") is True   # 兼容别名
    assert rbac.can_read_scope("legal", "enterprise_read") is True        # 兼容别名
    assert rbac.can_read_scope("bid_specialist", "approver_only") is False
    assert rbac.can_read_scope("data_admin", "approver_only") is False    # 别名不得借道审批层
    assert rbac.can_read_scope("business_head", "approver_only") is True