# R024（08-清单 §2.4 第 1/3 项）：审批/驳回/人工豁免/审计查询 + 结果页只读专项
# 沙盒 sqlite service 层测试：流程闭环、豁免字段/过期回阻断、审计完整、
# 结果页只读（视图函数不隐式创建 run/admission，F020 §2.2.4/§7）、空态结构。
from __future__ import annotations

import datetime as _dt
import re as _re

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from runtime.core import rbac
from runtime.core.errors import ApiError
from runtime.db import api_service
from runtime.db.models import (
    AdmissionResult,
    Approval,
    AuditEvent,
    Base,
    MatchRun,
    Project,
    Waiver,
)

pytestmark = pytest.mark.rag

_SCHEMA_PREFIX = _re.compile(
    r"\b(?:public_data|enterprise_data|admission_data|audit_data|knowledge_data)\."
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _strip_schema(conn, cursor, statement, parameters, context, executemany):
        return _SCHEMA_PREFIX.sub("", statement), parameters

    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# ---------- fixtures ----------

def _project(session, project_id="ND-2025"):
    session.add(Project(project_id=project_id, project_name="农大项目",
                        admission_status="qualified_full_score"))
    session.commit()


def _rag_run(session, run_id="MR-1") -> MatchRun:
    run = MatchRun(
        run_id=run_id, project_id="ND-2025", rule_set_id="RS-1", as_of="2025-10-30",
        mode="gate", coverage={"executed": 1, "declared": 1, "complete": True},
        status="completed", retrieval_run_id="rr-1", index_version="iv-1",
        candidate_chunk_ids=["CH-1"],
        structured_verification={"rag_degraded": None, "checked": [], "removed_material_ids": []},
        evidence_snapshot_hash="e" * 64,
    )
    session.add(run)
    session.commit()
    return run


def _full_score(session, run: MatchRun) -> AdmissionResult:
    ar = AdmissionResult(
        result_id="AR-1", run_id=run.run_id, project_id="ND-2025", rule_set_id="RS-1",
        qualification_result={"status": "passed", "satisfied": 1, "total": 1, "items": []},
        scoring_result={"status": "full", "objective_score": 5, "objective_max": 5,
                        "internal_full_score_ready": True, "items": []},
        operational_readiness={"status": "ready", "approval_ready": True,
                               "action_done": 0, "action_total": 0, "items": []},
        internal_admission_result={"status": "qualified_full_score", "eligible": True,
                                   "decision": "全满足"},
        internal_admission_eligible=True, total_score=5.0, max_total_score=5.0,
        score_gap_items=[], blocked_items=[], pending_items=[], review_items=[],
        manager_matches=[], explanation=[], result_freshness="current", state="final",
    )
    session.add(ar)
    session.commit()
    return ar


def _head_ctx():
    return {"role": rbac.BUSINESS_HEAD, "actor": "business_head"}


# ---------- §2.4 第 3 项：审批/驳回/豁免/审计 ----------

def test_approve_flow_full_lifecycle_and_terminal_no_redouble(session):
    _project(session)
    run = _rag_run(session)
    _full_score(session, run)

    created = api_service.create_approval(session, project_id="ND-2025",
                                          role=rbac.BUSINESS_HEAD, actor="business_head")
    assert created["state"] == "pending_bid_approval"
    assert session.get(Project, "ND-2025").admission_status == "pending_bid_approval"

    decided = api_service.decide_approval(
        session, project_id="ND-2025", role=rbac.BUSINESS_HEAD, actor="business_head",
        decision="approved", comment="资料齐备，同意投标")
    assert decided["outcome"] == "approved_for_bidding"
    assert session.get(Project, "ND-2025").admission_status == "approved_for_bidding"

    # 终态不可重复决策：无待审审批 → not_found
    with pytest.raises(ApiError) as excinfo:
        api_service.decide_approval(
            session, project_id="ND-2025", role=rbac.BUSINESS_HEAD, actor="business_head",
            decision="approved", comment="再次决策")
    assert excinfo.value.code == "not_found"


def test_reject_requires_comment(session):
    _project(session)
    _full_score(session, _rag_run(session))
    api_service.create_approval(session, project_id="ND-2025",
                                role=rbac.BUSINESS_HEAD, actor="business_head")
    with pytest.raises(ApiError) as excinfo:
        api_service.decide_approval(
            session, project_id="ND-2025", role=rbac.BUSINESS_HEAD, actor="business_head",
            decision="rejected", comment="")
    assert excinfo.value.code == "invalid_request"
    # 拒绝后项目态 → rejected_by_approver；audit 含 decision
    api_service.decide_approval(
        session, project_id="ND-2025", role=rbac.BUSINESS_HEAD, actor="business_head",
        decision="rejected", comment="保证金形式不符")
    assert session.get(Project, "ND-2025").admission_status == "rejected_by_approver"


def test_waiver_fields_validation(session):
    _project(session)
    base = dict(role=rbac.BUSINESS_HEAD, actor="business_head", project_id="ND-2025")
    with pytest.raises(ApiError) as e1:
        api_service.add_waiver(session, reason="", evidence_refs=["E-1"],
                               valid_until="2026-12-31", covered_items=["NQ-H-011"], **base)
    assert e1.value.code == "invalid_request"
    with pytest.raises(ApiError) as e2:
        api_service.add_waiver(session, reason="续期受理中", evidence_refs=[],
                               valid_until="2026-12-31", covered_items=["NQ-H-011"], **base)
    assert "证据" in e2.value.message
    with pytest.raises(ApiError) as e3:
        api_service.add_waiver(session, reason="续期受理中", evidence_refs=["E-1"],
                               valid_until="not-a-date", covered_items=["NQ-H-011"], **base)
    assert e3.value.code == "invalid_request"
    with pytest.raises(ApiError) as e4:
        api_service.add_waiver(session, reason="续期受理中", evidence_refs=["E-1"],
                               valid_until="2026-12-31", covered_items=[], **base)
    assert "覆盖项" in e4.value.message


def test_waiver_registration_records_fields_and_audit(session):
    _project(session)
    data = api_service.add_waiver(
        session, project_id="ND-2025", role=rbac.BUSINESS_HEAD, actor="business_head",
        reason="安全员C证续期已受理（受理回执）", evidence_refs=["EV-20260902-01"],
        valid_until="2026-12-31", covered_items=["NQ-H-005"])
    waiver = session.get(Waiver, data["waiver_id"])
    assert waiver is not None
    assert waiver.authorizer == "business_head"
    assert waiver.reason == "安全员C证续期已受理（受理回执）"
    assert waiver.evidence_refs == ["EV-20260902-01"]
    assert waiver.covered_items == ["NQ-H-005"]
    assert waiver.state == "active"
    events = session.scalars(select(AuditEvent)).all()
    assert any(e.action == "add_waiver" and e.actor == "business_head" for e in events)


def test_waiver_expiry_blocks_pending_approval_and_project(session):
    """豁免过期：waiver active→expired、pending approval→blocked_waiver_expired、
    项目态 pending_bid_approval→blocked_waiver_expired（F009 §6.3 / ADR-001 §2.2）。"""
    _project(session)
    _full_score(session, _rag_run(session))
    api_service.create_approval(session, project_id="ND-2025",
                                role=rbac.BUSINESS_HEAD, actor="business_head")
    wdata = api_service.add_waiver(
        session, project_id="ND-2025", role=rbac.BUSINESS_HEAD, actor="business_head",
        reason="在途材料", evidence_refs=["EV-1"], valid_until="2026-01-01",  # 已过期
        covered_items=["NQ-H-011"])

    expired = api_service.expire_waivers(session, "ND-2025")
    assert expired == 1
    assert session.get(Waiver, wdata["waiver_id"]).state == "expired"
    approval = session.scalar(select(Approval).where(Approval.project_id == "ND-2025"))
    assert approval.state == "blocked_waiver_expired"
    assert session.get(Project, "ND-2025").admission_status == "blocked_waiver_expired"
    # 被阻断后无法再决策
    with pytest.raises(ApiError) as excinfo:
        api_service.decide_approval(
            session, project_id="ND-2025", role=rbac.BUSINESS_HEAD, actor="business_head",
            decision="approved", comment="x")
    assert excinfo.value.code == "not_found"


def test_audit_timeline_complete_and_ordered(session):
    _project(session)
    _full_score(session, _rag_run(session))
    api_service.create_approval(session, project_id="ND-2025",
                                role=rbac.BUSINESS_HEAD, actor="business_head")
    api_service.add_waiver(session, project_id="ND-2025", role=rbac.BUSINESS_HEAD,
                           actor="business_head", reason="保证金保函在途",
                           evidence_refs=["EV-9"], valid_until="2026-12-31",
                           covered_items=["NQ-H-011"])
    api_service.decide_approval(session, project_id="ND-2025",
                                role=rbac.BUSINESS_HEAD, actor="business_head",
                                decision="rejected", comment="报价超限价")
    timeline = api_service.list_audit(session, "ND-2025", rbac.BUSINESS_HEAD)
    actions = [e["action"] for e in timeline]
    assert "create_approval" in actions
    assert "add_waiver" in actions
    assert "rejected" in actions
    # 时间升序（仅追加）
    stamps = [e["at"] for e in timeline]
    assert stamps == sorted(stamps)


def test_approval_denied_for_non_head_role(session):
    """最小权限：投标专员不可审批/豁免/审计（服务层 require_approval_role）。"""
    _project(session)
    _full_score(session, _rag_run(session))
    with pytest.raises(ApiError) as e1:
        api_service.create_approval(session, project_id="ND-2025",
                                    role=rbac.BID_SPECIALIST, actor="spec")
    assert e1.value.code == "forbidden"
    with pytest.raises(ApiError) as e2:
        api_service.list_audit(session, "ND-2025", rbac.BID_SPECIALIST)
    assert e2.value.code == "forbidden"


# ---------- §2.4 第 1 项：结果页只读（视图不建 run/admission；空态结构） ----------

def _snapshot_counts(session):
    return {
        "runs": len(session.scalars(select(MatchRun)).all()),
        "admissions": len(session.scalars(select(AdmissionResult)).all()),
        "jobs": len(session.scalars(select(Approval)).all()),
    }


def test_result_pages_readonly_no_implicit_run(session):
    """结果页视图函数只读：多次调用不新增 MatchRun/Admission/Approval（F020 §2.2.4/§7）。"""
    _project(session)
    _full_score(session, _rag_run(session))
    for _ in range(3):
        api_service.admission_summary(session, "ND-2025")
        api_service.queues_summary(session, "ND-2025")
        api_service.matrix_view(session, "ND-2025")
        api_service.match_runs_latest(session, "ND-2025")
    before = _snapshot_counts(session)
    api_service.admission_summary(session, "ND-2025")
    api_service.queues_summary(session, "ND-2025")
    api_service.match_runs_latest(session, "ND-2025")
    assert _snapshot_counts(session) == before


def test_result_pages_empty_state_structure(session):
    """空态：无任何运行/结果的项目，结果页返回约定空结构而非 500。"""
    _project(session)
    summary = api_service.admission_summary(session, "ND-2025")
    assert summary["admission"]["eligible"] is False
    assert summary["admission"]["state"] == "collecting"
    assert summary["admission"]["missing"] == []
    queues = api_service.queues_summary(session, "ND-2025")
    assert queues["queues"] == {
        "blocked_hard_requirement": [], "blocked_missing_data": [], "manual_review": []}
    latest = api_service.match_runs_latest(session, "ND-2025")
    assert latest["run_id"] is None
