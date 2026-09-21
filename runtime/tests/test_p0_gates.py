# ADR-004 Iteration 0（2026-09-16 完整优化方案 §14 退出标准）：P0 服务端门禁自动化阻断用例
#
# 覆盖：
# - P0-01 worker 进程心跳（record_heartbeat / worker_status：无心跳/新鲜/超时）；
# - P0-02 项目身份校验（纯逻辑三态 + 服务层落库/确认/自动评估 + 匹配/审批阻断）；
# - P0-03 截止过期（overdue 迁移、人工终态不改写、重算/审批门禁、延期恢复、readiness）；
# - 证据链最低要求（satisfied 无 evidence_refs → manual_review；联合体极性豁免；动作项不降级）；
# - 评分不可计算不为 0（主观项未过评审 score=None）。
# sqlite 内存库 + schema 前缀剥离（与 test_worker_match.py 同口径）。
from __future__ import annotations

import datetime as _dt
import re as _re
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from runtime.core import identity as identity_logic
from runtime.core import matching
from runtime.core.errors import ApiError
from runtime.db import api_service, identity_service, lifecycle_service, worker_service
from runtime.db.models import (
    AdmissionResult,
    AnalysisJob,
    AuditEvent,
    Base,
    MatchRun,
    Project,
    ProjectIdentity,
    Requirement,
    RuleSet,
    WorkerHeartbeat,
)

pytestmark = pytest.mark.rag

_SCHEMA_PREFIX = _re.compile(
    r"\b(?:public_data|enterprise_data|admission_data|audit_data|knowledge_data)\."
)

TODAY = _dt.date(2026, 9, 16)
PAST = _dt.date(2025, 10, 30)
FUTURE = _dt.date(2026, 12, 31)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _strip_schema(conn, cursor, statement, parameters, context, executemany):
        return _SCHEMA_PREFIX.sub("", statement), parameters

    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _project(session, project_id="ND-2025", *, status="qualified_full_score",
             bid_deadline=None, bid_deadline_at=None, name="河北农业大学教学楼施工") -> Project:
    project = Project(project_id=project_id, project_name=name,
                      admission_status=status, bid_deadline=bid_deadline,
                      bid_deadline_at=bid_deadline_at)
    session.add(project)
    session.commit()
    return project


def _full_score(session, project_id="ND-2025") -> AdmissionResult:
    run = MatchRun(
        run_id=f"MR-{project_id}", project_id=project_id, rule_set_id="RS-1", as_of="2025-10-30",
        mode="gate", coverage={"executed": 1, "declared": 1, "complete": True},
        status="completed", retrieval_run_id="rr-1", index_version="iv-1",
        candidate_chunk_ids=["CH-1"], structured_verification={},
        evidence_snapshot_hash="e" * 64,
    )
    session.add(run)
    ar = AdmissionResult(
        result_id=f"AR-{project_id}", run_id=run.run_id, project_id=project_id, rule_set_id="RS-1",
        qualification_result={"status": "passed"}, scoring_result={"status": "full"},
        operational_readiness={"status": "ready"},
        internal_admission_result={"status": "qualified_full_score", "eligible": True, "decision": "全满足"},
        internal_admission_eligible=True, result_freshness="current", state="final",
    )
    session.add(ar)
    session.commit()
    return ar


def _audits(session, action: str) -> list[AuditEvent]:
    return session.scalars(select(AuditEvent).where(AuditEvent.action == action)).all()


# ============================================================
# P0-02 身份校验：纯逻辑
# ============================================================

def test_identity_code_mismatch_is_hard_conflict():
    r = identity_logic.compare_identity(
        {"project_name": "博野县城区道路改造工程", "tender_no": "HBGR-2024085"},
        {"project_name": "博野县城区道路改造工程", "tender_no": "HBGR-2024099"},
    )
    assert r.status == identity_logic.IDENTITY_CONFLICT
    assert r.blocks_matching is True
    assert [c.field for c in r.conflicts] == ["tender_no"]
    assert r.conflicts[0].severity == identity_logic.SEVERITY_HARD


def test_identity_code_normalization_treats_separators_equal():
    assert identity_logic.normalize_code("HBGR-2024085") == identity_logic.normalize_code("hbgr 2024085")
    r = identity_logic.compare_identity({"tender_no": "HBGR-2024085"}, {"tender_no": "hbgr 2024085"})
    assert r.status == identity_logic.IDENTITY_CONFIRMED and not r.conflicts


def test_identity_dissimilar_name_and_purchaser_is_high_conflict():
    r = identity_logic.compare_identity(
        {"project_name": "博野县城区道路改造工程", "purchaser": "博野县住房和城乡建设局"},
        {"project_name": "石家庄市第二医院门诊楼装修", "purchaser": "石家庄市第二医院"},
    )
    assert r.status == identity_logic.IDENTITY_CONFLICT
    assert {c.field for c in r.conflicts} == {"project_name", "purchaser"}
    assert all(c.severity == identity_logic.SEVERITY_HIGH for c in r.conflicts)


def test_identity_deadline_and_budget_difference_is_warning_not_conflict():
    r = identity_logic.compare_identity(
        {"project_name": "博野县城区道路改造工程", "bid_deadline": "2025年10月30日 09时30分",
         "budget_amount": "1200万元"},
        {"project_name": "博野县城区道路改造工程施工招标文件", "bid_deadline": "2025-11-06",
         "budget_amount": "1500万元"},
    )
    assert r.status == identity_logic.IDENTITY_WARNING
    assert not r.conflicts
    assert {w.field for w in r.warnings} == {"bid_deadline", "budget_amount"}
    assert r.similarity["project_name"] >= identity_logic.NAME_SIMILAR_MIN


def test_identity_confirmed_when_comparable_fields_agree():
    r = identity_logic.compare_identity(
        {"project_name": "博野县城区道路改造工程", "purchaser": "博野县住房和城乡建设局",
         "bid_deadline": "2025-10-30", "tender_no": "HBGR-2024085"},
        {"project_name": "博野县城区道路改造工程", "purchaser": "博野县住房和城乡建设局",
         "bid_deadline": "2025-10-30", "tender_no": "HBGR-2024085"},
    )
    assert r.status == identity_logic.IDENTITY_CONFIRMED
    assert set(r.compared_fields) == {"tender_no", "project_name", "purchaser", "bid_deadline"}


def test_identity_without_comparable_fields_requires_manual_check():
    r = identity_logic.compare_identity({"project_name": "甲"}, {"purchaser": "乙"})
    assert r.status == identity_logic.IDENTITY_WARNING
    assert r.warnings[0].field == "*"
    assert r.blocks_matching is False


# ============================================================
# P0-02 身份校验：服务层（落库 / 确认 / 自动评估）
# ============================================================

def test_identity_service_persists_result_and_writes_back_bid_deadline(session):
    project = _project(session, status="matching")
    payload = identity_service.evaluate_project_identity(
        session, project_id="ND-2025", actor="toubiao",
        expected={"tender_no": "HBGR-2024085", "bid_deadline": "2025-10-30"},
        actual={"tender_no": "HBGR-2024099", "bid_deadline": "2025年10月30日"},
    )
    assert payload["status"] == "identity_conflict" and payload["blocks_matching"] is True
    row = session.get(ProjectIdentity, "ND-2025")
    assert row.identity_status == "identity_conflict"
    assert row.identity_conflicts[0]["field"] == "tender_no"
    assert row.bid_deadline == PAST
    assert session.get(Project, "ND-2025").bid_deadline == PAST      # P0-03 数据来源回写
    assert row.expected_snapshot["tender_no"] == "HBGR-2024085"
    assert any(a.outcome.startswith("identity_conflict") for a in _audits(session, "project.identity_checked"))


def test_identity_confirm_only_allowed_for_warning(session):
    _project(session, status="matching")
    identity_service.evaluate_project_identity(
        session, project_id="ND-2025", actor="toubiao",
        expected={"project_name": "河北农业大学教学楼", "bid_deadline": "2025-10-30"},
        actual={"project_name": "河北农业大学教学楼施工招标文件", "bid_deadline": "2025-11-06"},
    )
    assert session.get(ProjectIdentity, "ND-2025").identity_status == "identity_warning"
    with pytest.raises(ApiError) as exc:
        identity_service.confirm_project_identity(session, project_id="ND-2025", actor="toubiao", note="")
    assert exc.value.code == "invalid_request"
    out = identity_service.confirm_project_identity(
        session, project_id="ND-2025", actor="toubiao", note="核对延期公告 2025-11-06，同一项目")
    assert out["status"] == "identity_confirmed" and out["confirmed_by"] == "toubiao"

    # conflict 不允许一键确认
    identity_service.evaluate_project_identity(
        session, project_id="ND-2025", actor="toubiao",
        expected={"tender_no": "A-1"}, actual={"tender_no": "B-2"},
    )
    with pytest.raises(ApiError) as exc:
        identity_service.confirm_project_identity(session, project_id="ND-2025", actor="toubiao", note="强行确认")
    assert exc.value.code == "invalid_state_transition"


def test_ensure_identity_evaluates_once_and_keeps_existing(session):
    _project(session, status="matching")
    row = identity_service.ensure_project_identity(session, project_id="ND-2025", actor="system:match")
    assert row is not None and row.identity_status == "identity_warning"   # 仅项目名，无可比对字段
    row.identity_status = "identity_confirmed"
    session.commit()
    again = identity_service.ensure_project_identity(session, project_id="ND-2025", actor="system:match")
    assert again.identity_status == "identity_confirmed"                    # 不覆盖既有结果
    assert identity_service.ensure_project_identity(session, project_id="NOPE", actor="x") is None


# ============================================================
# P0-03 截止过期：生命周期与门禁
# ============================================================

def test_overdue_moves_machine_state_and_audits_once(session):
    project = _project(session, status="qualified_full_score", bid_deadline=PAST)
    assert lifecycle_service.refresh_overdue(session, project, today=TODAY) is True
    assert project.admission_status == "overdue"
    assert lifecycle_service.refresh_overdue(session, project, today=TODAY) is True
    assert len(_audits(session, "project.overdue")) == 1


def test_overdue_does_not_rewrite_human_decided_states(session):
    project = _project(session, status="approved_for_bidding", bid_deadline=PAST)
    assert lifecycle_service.refresh_overdue(session, project, today=TODAY) is True
    assert project.admission_status == "approved_for_bidding"
    assert not _audits(session, "project.overdue")


def test_deadline_today_is_not_overdue_and_missing_deadline_never_gates(session):
    assert lifecycle_service.is_overdue(_project(session, "P-TODAY", bid_deadline=TODAY), today=TODAY) is False
    project = _project(session, "P-NONE")
    assert lifecycle_service.gate_reason(session, project, action="approval", today=TODAY) is None


def test_gate_reason_prefers_overdue_then_identity_conflict(session):
    project = _project(session, status="qualified_full_score", bid_deadline=PAST)
    code, message = lifecycle_service.gate_reason(session, project, action="recalculate", today=TODAY)
    assert code == "overdue" and "重算" in message

    fresh = _project(session, "P-ID", status="matching", bid_deadline=FUTURE)
    identity_service.evaluate_project_identity(
        session, project_id="P-ID", actor="t", expected={"tender_no": "A"}, actual={"tender_no": "B"})
    code, message = lifecycle_service.gate_reason(session, fresh, action="match", today=TODAY)
    assert code == "identity_conflict" and "新建匹配任务" in message


def test_deny_if_gated_raises_409_with_audit(session):
    project = _project(session, status="qualified_full_score", bid_deadline=PAST)
    with pytest.raises(ApiError) as exc:
        lifecycle_service.deny_if_gated(session, project, action="approval", actor="jingying",
                                        audit_action="approval.create_denied", today=TODAY)
    assert exc.value.code == "invalid_state_transition" and exc.value.status_code == 409
    assert exc.value.detail == {"gate": "approval", "reason": "overdue"}
    denied = _audits(session, "approval.create_denied")
    assert len(denied) == 1 and denied[0].outcome == "overdue"


def test_set_bid_deadline_extension_recovers_overdue_project(session):
    project = _project(session, status="matching", bid_deadline=PAST)
    lifecycle_service.refresh_overdue(session, project, today=TODAY)
    assert project.admission_status == "overdue"
    lifecycle_service.set_bid_deadline(session, project, FUTURE, actor="toubiao", source="延期公告 2026-09-10")
    session.commit()
    assert project.bid_deadline == FUTURE and project.admission_status == "matching"
    assert _audits(session, "project.bid_deadline_set") and _audits(session, "project.overdue_recovered")


def test_create_approval_denied_for_overdue_project(session):
    _project(session, status="qualified_full_score", bid_deadline=PAST)
    _full_score(session)
    with pytest.raises(ApiError) as exc:
        api_service.create_approval(session, project_id="ND-2025", role="business_head", actor="jingying")
    assert exc.value.code == "invalid_state_transition"
    assert "已过投标截止" in exc.value.message
    assert session.get(Project, "ND-2025").admission_status == "overdue"
    assert not session.scalars(select(AuditEvent).where(AuditEvent.action == "create_approval")).all()


def test_create_approval_denied_for_identity_conflict(session):
    _project(session, status="qualified_full_score", bid_deadline=FUTURE)
    _full_score(session)
    identity_service.evaluate_project_identity(
        session, project_id="ND-2025", actor="t", expected={"tender_no": "A"}, actual={"tender_no": "B"})
    with pytest.raises(ApiError) as exc:
        api_service.create_approval(session, project_id="ND-2025", role="business_head", actor="jingying")
    assert exc.value.code == "invalid_state_transition" and "串档" in exc.value.message
    assert _audits(session, "approval.create_denied")[0].outcome == "identity_conflict"


def test_create_approval_still_allowed_when_gates_pass(session):
    _project(session, status="qualified_full_score", bid_deadline=FUTURE)
    _full_score(session)
    identity_service.evaluate_project_identity(
        session, project_id="ND-2025", actor="t",
        expected={"tender_no": "A", "project_name": "河北农业大学教学楼施工"},
        actual={"tender_no": "A", "project_name": "河北农业大学教学楼施工"})
    created = api_service.create_approval(session, project_id="ND-2025", role="business_head", actor="jingying")
    assert created["state"] == "pending_bid_approval"


def test_project_readiness_reports_facts_and_blocking_reasons(session):
    _project(session, status="qualified_full_score", bid_deadline=PAST)
    _full_score(session)
    data = lifecycle_service.project_readiness(session, "ND-2025", today=TODAY)
    assert data["overdue"] is True and data["stage"] == "overdue"
    assert data["days_to_deadline"] < 0
    assert data["identity"]["status"] is None
    codes = {b["code"] for b in data["blocking_reasons"]}
    assert {"overdue", "identity_unchecked"} <= codes
    assert data["gates"] == {"can_match": False, "can_recalculate": False, "can_create_approval": False}
    assert data["latest_admission"]["eligible"] is True and data["latest_admission"]["freshness"] == "current"

    _project(session, "P-OK", status="matching")
    ok = lifecycle_service.project_readiness(session, "P-OK", today=TODAY)
    assert ok["bid_deadline"] is None
    assert {b["code"] for b in ok["blocking_reasons"]} >= {"bid_deadline_missing", "identity_unchecked"}
    assert ok["gates"]["can_match"] is True


# ============================================================
# worker 匹配执行器门禁（gate 运行终态失败留痕）
# ============================================================

def _rule_set_with_requirement(session, project_id="ND-2025"):
    session.add(RuleSet(rule_set_id="RS-1", project_id=project_id, version="1.0.0",
                        created_by="测试", snapshot={}))
    session.add(Requirement(
        requirement_id="NQ-H-001", rule_set_id="RS-1", req_type="hard_requirement",
        category="资质", clause_ref="第三章四(1)", assertion="具备建筑工程施工总承包二级及以上资质",
        rule={"type": "qualification", "qualification_type": "建筑工程施工总承包", "level": "二级"},
        evidence_required=["qualification_record"], missing_action="blocked_missing_data",
        failure_effect="not_qualified", as_of="2025-10-30",
    ))
    session.commit()


def test_worker_match_run_blocked_for_overdue_project(session, monkeypatch):
    from runtime.worker import _execute_match_run

    monkeypatch.setattr(lifecycle_service, "today_cn", lambda now=None: TODAY)
    _project(session, status="matching", bid_deadline=PAST)
    _rule_set_with_requirement(session)
    with pytest.raises(matching.GateBlockedError) as exc:
        _execute_match_run(session, "ND-2025")
    assert exc.value.code == "overdue"
    assert session.scalar(select(MatchRun)) is None                        # 未产生运行快照
    assert _audits(session, "match.gate_blocked")[0].outcome == "overdue"


def test_worker_match_run_blocked_for_identity_conflict_but_diagnostic_allowed(session, monkeypatch):
    from runtime.worker import _execute_match_run

    monkeypatch.setattr(lifecycle_service, "today_cn", lambda now=None: TODAY)
    _project(session, status="matching", bid_deadline=FUTURE)
    _rule_set_with_requirement(session)
    identity_service.evaluate_project_identity(
        session, project_id="ND-2025", actor="t", expected={"tender_no": "A"}, actual={"tender_no": "B"})
    with pytest.raises(matching.GateBlockedError) as exc:
        _execute_match_run(session, "ND-2025", mode="gate")
    assert exc.value.code == "identity_conflict"
    # 诊断运行不驱动准入状态，不受门禁限制（可用于人工核对）
    _execute_match_run(session, "ND-2025", mode="diagnostic")
    assert session.scalar(select(MatchRun)).mode == "diagnostic"


def test_process_one_marks_gate_blocked_job_failed_without_retry(session, monkeypatch):
    from runtime.worker import process_one

    monkeypatch.setattr(lifecycle_service, "today_cn", lambda now=None: TODAY)
    _project(session, status="matching", bid_deadline=PAST)
    _rule_set_with_requirement(session)
    job, created = worker_service.create_job(session, kind="match.run", input_ref="ND-2025", project_id="ND-2025")
    assert created
    assert process_one(session, "runner-test", stale_seconds=600) is True
    session.expire_all()
    job = session.get(AnalysisJob, job.job_id)
    assert job.status == "failed" and job.attempts == 1
    assert job.error_code == "gate_overdue"
    assert "已过投标截止" in job.error_message


# ============================================================
# 证据链最低要求：satisfied 无回链降级
# ============================================================

def _engine_result(entries):
    return {
        "matrix": entries,
        "coverage": {"executed": len(entries), "declared": len(entries), "complete": True},
        "blocked": [], "pending": [], "review": [],
        "qualification_result": "passed", "scoring_result": "full",
        "operational_readiness": "ready", "internal_admission_eligible": True,
    }


def test_downgrade_satisfied_without_evidence_refs():
    entries = [
        {"requirement_id": "H-1", "req_type": "hard_requirement", "match_result": "satisfied",
         "match_reason": "资质满足", "score": None},
        {"requirement_id": "H-2", "req_type": "hard_requirement", "match_result": "satisfied",
         "match_reason": "安许证有效", "score": None},
        {"requirement_id": "S-1", "req_type": "scored_requirement", "match_result": "satisfied",
         "match_reason": "客观项可复算 5/5", "score": 5, "max_score": 5},
        {"requirement_id": "A-1", "req_type": "action_requirement", "match_result": "satisfied",
         "match_reason": "投标动作已完成", "score": None},
    ]
    reqs = [{"requirement_id": e["requirement_id"], "rule": {"type": "qualification"}} for e in entries]
    result = matching.downgrade_satisfied_without_evidence(
        _engine_result(entries), {"H-1": {"evidence_refs": ["MAT-Q-1:v1:p1"]}}, reqs)
    by_id = {e["requirement_id"]: e for e in result["matrix"]}
    assert by_id["H-1"]["match_result"] == "satisfied"                 # 有回链保持
    assert by_id["H-2"]["match_result"] == "manual_review" and by_id["H-2"]["evidence_downgraded"] is True
    assert matching.EVIDENCE_DOWNGRADE_NOTE in by_id["H-2"]["match_reason"]
    assert by_id["S-1"]["match_result"] == "manual_review" and by_id["S-1"]["score"] is None
    assert by_id["A-1"]["match_result"] == "satisfied"                 # 动作项由人员登记，不需企业证据
    assert result["internal_admission_eligible"] is False
    assert result["scoring_result"] == "not_full" and result["qualification_result"] == "pending"
    assert result["coverage"]["evidence_downgraded"] == ["H-2", "S-1"]
    assert [e["requirement_id"] for e in result["review"]] == ["H-2", "S-1"]


def test_downgrade_exempts_consortium_polarity_document_fact():
    entries = [{"requirement_id": "H-9", "req_type": "hard_requirement", "match_result": "satisfied",
                "match_reason": "招标文件接受联合体投标", "score": None}]
    reqs = [{"requirement_id": "H-9", "rule": {"type": "consortium", "accepts_consortium": True}}]
    result = matching.downgrade_satisfied_without_evidence(_engine_result(entries), {}, reqs)
    assert result["matrix"][0]["match_result"] == "satisfied"
    assert result["internal_admission_eligible"] is True
    assert "evidence_downgraded" not in result["coverage"]


def test_downgrade_leaves_non_satisfied_entries_untouched():
    entries = [{"requirement_id": "H-3", "req_type": "hard_requirement", "match_result": "unverifiable",
                "match_reason": "缺证", "score": None}]
    result = matching.downgrade_satisfied_without_evidence(_engine_result(entries), {}, [])
    assert result["matrix"][0]["match_result"] == "unverifiable" and result["review"] == []


# ============================================================
# 评分不可计算不为 0
# ============================================================

def test_engine_subjective_unreviewed_score_is_none_not_zero():
    from scripts.matching import engine

    requirements = [{
        "requirement_id": "NQ-S-001", "req_type": "scored_requirement", "category": "技术标",
        "clause_ref": "评标办法 2.2", "assertion": "施工组织设计评审", "evidence_required": [],
        "rule": {}, "score_nature": "subjective", "max_score": 20,
    }]
    out = engine.evaluate(requirements, {}, as_of="2025-10-30", mode="diagnostic")
    entry = out["matrix"][0]
    assert entry["match_result"] == "manual_review"
    assert entry["score"] is None
    assert out["scoring_result"] == "not_full" and out["internal_admission_eligible"] is False


# ============================================================
# P0-01 worker 进程心跳
# ============================================================

def test_worker_heartbeat_status_transitions(session):
    now = _dt.datetime(2026, 9, 16, 8, 0, tzinfo=_dt.timezone.utc)
    none = worker_service.worker_status(session, stale_seconds=60, now=now)
    assert none["available"] is False and none["state"] == "unavailable" and "无 worker 心跳" in none["error"]

    worker_service.record_heartbeat(session, "worker-1", poll_interval_seconds=5, pid=123, hostname="demo")
    row = session.get(WorkerHeartbeat, "worker-1")
    fresh = worker_service.worker_status(session, stale_seconds=60, now=row.heartbeat_at + _dt.timedelta(seconds=10))
    assert fresh["available"] is True and fresh["state"] == "ok" and fresh["runner_id"] == "worker-1"

    stale = worker_service.worker_status(session, stale_seconds=60, now=row.heartbeat_at + _dt.timedelta(seconds=90))
    assert stale["available"] is False and stale["state"] == "stale" and "超时" in stale["error"]

    worker_service.record_heartbeat(session, "worker-1", poll_interval_seconds=5)
    assert session.get(WorkerHeartbeat, "worker-1").heartbeat_at >= row.heartbeat_at   # upsert 同一行
    assert len(session.scalars(select(WorkerHeartbeat)).all()) == 1


# ============================================================
# ADR-004 P0 查漏补缺：三个入口先校验身份，精确截止时点
# ============================================================

def test_identity_same_day_different_deadline_clock_is_warning():
    result = identity_logic.compare_identity(
        {"bid_deadline": "2026-09-16 09:00"},
        {"bid_deadline": "2026年9月16日10时00分"},
    )
    assert result.status == identity_logic.IDENTITY_WARNING
    assert result.warnings[0].field == "bid_deadline"


def test_precise_deadline_closes_at_exact_time_and_date_only_is_conservative(session):
    point = _dt.datetime(2026, 9, 16, 9, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    project = _project(session, "P-POINT", bid_deadline=TODAY, bid_deadline_at=point)
    before = point - _dt.timedelta(seconds=1)
    assert lifecycle_service.is_overdue(project, now=before) is False
    assert lifecycle_service.is_overdue(project, now=point) is True
    # 当日仅日期不可视为已过期，且 readiness 必须披露该降级。
    date_only = _project(session, "P-DATE", bid_deadline=TODAY)
    data = lifecycle_service.project_readiness(session, "P-DATE", now=point)
    assert lifecycle_service.is_overdue(date_only, now=point) is False
    assert data["deadline_precision"] == "date" and "时分秒" in data["deadline_accuracy_note"]


def test_precise_deadline_extension_recovers_overdue_project(session):
    past_point = _dt.datetime(2026, 9, 16, 9, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    future_point = _dt.datetime(2026, 9, 17, 9, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    project = _project(session, "P-EXT", status="matching", bid_deadline=TODAY, bid_deadline_at=past_point)
    assert lifecycle_service.refresh_overdue(session, project, now=past_point) is True
    lifecycle_service.set_bid_deadline(session, project, future_point.date(), value_at=future_point,
                                       actor="toubiao", source="延期公告 P-EXT")
    assert project.admission_status == "matching"
    assert project.bid_deadline_at == future_point
    assert _audits(session, "project.overdue_recovered")


def test_set_precise_deadline_rejects_naive_or_date_mismatch(session):
    project = _project(session, "P-BAD")
    naive = _dt.datetime(2026, 9, 16, 9, 0)
    with pytest.raises(ApiError):
        lifecycle_service.set_bid_deadline(session, project, TODAY, value_at=naive, actor="x", source="s")
    aware = naive.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=8)))
    with pytest.raises(ApiError):
        lifecycle_service.set_bid_deadline(session, project, FUTURE, value_at=aware, actor="x", source="s")


def test_approval_auto_identity_check_conflict_is_rejected_and_audited(session, monkeypatch):
    _project(session, status="qualified_full_score", bid_deadline=FUTURE)
    _full_score(session)
    def conflict(session, *, project_id, actor):
        identity_service.evaluate_project_identity(
            session, project_id=project_id, actor=actor,
            expected={"tender_no": "A"}, actual={"tender_no": "B"})
    monkeypatch.setattr(identity_service, "ensure_project_identity", conflict)
    with pytest.raises(ApiError) as exc:
        api_service.create_approval(session, project_id="ND-2025", role="business_head", actor="jingying")
    assert exc.value.status_code == 409 and exc.value.detail["reason"] == "identity_conflict"
    assert _audits(session, "project.identity_checked") and _audits(session, "approval.create_denied")


def test_match_and_recalculate_auto_identity_check_conflict_before_job(session, monkeypatch):
    # 路由共用集中函数；此处通过真实集中层验证两个动作的同步拒绝、未创建作业和审计。
    project = _project(session, status="matching", bid_deadline=FUTURE)
    def conflict(session, *, project_id, actor):
        identity_service.evaluate_project_identity(
            session, project_id=project_id, actor=actor,
            expected={"tender_no": "A"}, actual={"tender_no": "B"})
    monkeypatch.setattr(identity_service, "ensure_project_identity", conflict)
    for action, audit in (("match", "match.trigger_denied"), ("recalculate", "match.recalculate_denied")):
        with pytest.raises(ApiError) as exc:
            lifecycle_service.ensure_identity_then_deny(session, project, action=action,
                                                        actor="toubiao", audit_action=audit)
        assert exc.value.status_code == 409 and exc.value.detail["reason"] == "identity_conflict"
    assert session.scalar(select(AnalysisJob)) is None
    assert len(_audits(session, "project.identity_checked")) == 2
    assert len(_audits(session, "match.trigger_denied")) == 1
    assert len(_audits(session, "match.recalculate_denied")) == 1



@pytest.fixture()
def api_client(monkeypatch):
    """项目门禁 HTTP 契约：共享 sqlite 单连接，避免误把服务层测试当成路由验收。"""
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool
    from runtime.api import app
    from runtime.routers.deps import get_db

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _strip_schema(conn, cursor, statement, parameters, context, executemany):
        return _SCHEMA_PREFIX.sub("", statement), parameters

    Base.metadata.create_all(engine)

    def _db_override():
        with Session(engine) as s:
            yield s

    monkeypatch.setenv("AUTH_DEV_HEADERS", "true")
    app.dependency_overrides[get_db] = _db_override
    with TestClient(app, raise_server_exceptions=False) as client:
        client.engine = engine
        yield client
    app.dependency_overrides.clear()


def _api_project(engine, project_id="P-API", *, deadline=FUTURE):
    with Session(engine) as session:
        _project(session, project_id, status="qualified_full_score", bid_deadline=deadline)
        _full_score(session, project_id)


def test_deadline_endpoint_rejects_naive_mismatch_and_accepts_precise(api_client):
    _api_project(api_client.engine)
    headers = {"X-Role": "business_head", "X-Actor": "jingying"}
    base = "/api/v1/projects/P-API/bid-deadline"
    naive = api_client.post(base, headers=headers, json={
        "bid_deadline_at": "2026-09-16T09:00:00", "source": "招标文件第 1 页"})
    assert naive.status_code == 400 and naive.json()["error"]["code"] == "invalid_request"
    mismatch = api_client.post(base, headers=headers, json={
        "bid_deadline": "2026-09-17", "bid_deadline_at": "2026-09-16T09:00:00+08:00",
        "source": "招标文件第 1 页"})
    assert mismatch.status_code == 400 and mismatch.json()["error"]["code"] == "invalid_request"
    ok = api_client.post(base, headers=headers, json={
        "bid_deadline_at": "2026-09-16T09:00:00+08:00", "source": "招标文件第 1 页"})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["bid_deadline"] == "2026-09-16"
    assert body["deadline_precision"] == "datetime"
    assert body["bid_deadline_at"].startswith("2026-09-16T09:00:00+08:00")
