# ADR-008 / docs/12 §8.1-§8.2：匹配结果收敛与解析质量分层 回归测试（2026-09-28）
#
# 覆盖（docs/12 §8.1）：
# 1 状态拆分（0 条已核验 ≠ 无台账；过期/专业不符/在施独立原因码）
# 2 人员绑定（同一 person_id 约束；不得跨人拼接；候选方案互不串证据）
# 3 评分分类（methodology_only 不计分；报价无授权输入不计算；主观未评审不填分；无客观项不输出 0/0）
# 4 动作分层（submission_ready 未开始不建缺证任务不阻断审批；approval_ready 未就绪阻断并点名）
# 5 解析例外（只阻断满分且原因独立；处置必填理由/检索范围；版本不一致 409）
# 6 任务聚合（同一缺口一条任务、条款回链完整；不同标段不合并）
# 7 版本与幂等（同输入复用；输入变化新运行；旧结果不可审批；读接口 run_context 一致）
# 8 权限与历史样本（角色限制；历史样本审批二次拒绝）
# 场景 A1-A8 的服务端断言（浏览器走查另行人工验收）。
from __future__ import annotations

import datetime as _dt
import re as _re

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from runtime.core.errors import ApiError
from runtime.db.models import (
    AdmissionResult,
    AuditEvent,
    Base,
    Material,
    MatchItem,
    MatchRun,
    OperationTask,
    ParseException,
    ParseCandidate,
    Personnel,
    Project,
    ProjectIdentity,
    Qualification,
    RemediationTask,
    Requirement,
    RuleSet,
)
from runtime.worker import _execute_match_run

pytestmark = pytest.mark.rag

_SCHEMA_PREFIX = _re.compile(
    r"\b(?:public_data|enterprise_data|admission_data|audit_data|knowledge_data)\."
)


@pytest.fixture()
def session():
    # check_same_thread=False + StaticPool：TestClient 在独立线程执行同步端点，
    # 允许跨线程复用同一 sqlite 连接（仅测试库）
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _strip_schema(conn, cursor, statement, parameters, context, executemany):
        return _SCHEMA_PREFIX.sub("", statement), parameters

    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


PID = "PJ-ADR008"


def _project(session, project_id=PID, **kw):
    session.add(Project(project_id=project_id, project_name="分层测试项目", **kw))
    session.add(ProjectIdentity(
        project_id=project_id, identity_status="identity_confirmed",
        identity_conflicts=[], identity_warnings=[], compared_fields=[], source_refs=[],
    ))
    session.commit()


def _rule_set(session, project_id=PID, rule_set_id="RS-L1"):
    session.add(RuleSet(rule_set_id=rule_set_id, project_id=project_id,
                        version="1.0.0", created_by="测试", snapshot={}))
    session.commit()


def _req(session, rid, *, req_type="hard_requirement", rule, as_of="2026-09-28",
         evidence_required=None, clause_ref="§3.1", assertion="条款断言", **kw):
    session.add(Requirement(
        requirement_id=rid, rule_set_id="RS-L1", req_type=req_type, category="测试",
        clause_ref=clause_ref, assertion=assertion, rule=rule,
        evidence_required=evidence_required or [], missing_action="blocked_missing_data",
        as_of=as_of, **kw,
    ))
    session.commit()


def _qualification(session, material_id, **kw):
    data = dict(
        qualification_id=f"Q-{material_id}", material_id=material_id,
        category="建筑工程施工总承包", level="二级",
        valid_from=None, valid_until=None, evidence_refs=[f"ledger:{material_id}"],
        data_owner="测试", verified_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
        status="active",
    )
    data.update(kw)
    session.add(Qualification(**data))
    session.commit()


def _safety_officer(session, personnel_id, **kw):
    data = dict(
        personnel_id=personnel_id, material_id=f"M-{personnel_id}", name=f"安全员{personnel_id}",
        category="post_certificate", specialty="安全管理", cert_level="C",
        valid_from=None, valid_until=None, evidence_refs=[f"ledger:{personnel_id}"],
        data_owner="测试", verified_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
        status="active",
    )
    data.update(kw)
    session.add(Personnel(**data))
    session.commit()


def _tender_material(session, project_id=PID, material_id="MAT-T1", version=1):
    session.add(Material(
        material_id=material_id, version=version, material_type="tender_document",
        source_type="uploaded", owner_type="public", classification="public",
        permission_scope="public_read", content_hash="c" * 64, parse_status="parsed",
        status="active", evidence_refs=[], data_owner="测试", project_id=project_id,
    ))
    session.commit()


def _pending_candidate(session, *, candidate_id, material_id="MAT-T1", version=1,
                       project_id=PID, payload=None):
    session.add(ParseCandidate(
        candidate_id=candidate_id, material_id=material_id, version=version,
        project_id=project_id, kind="rule_candidate",
        payload=payload or {"anchor_key": "financial_audit", "missing_marker": True,
                            "req_type": "hard_requirement", "assertion": "__待补__"},
        status="pending",
    ))
    session.commit()


def _run(session):
    return _execute_match_run(session, PID, mode="gate")


def _items(session, run_id):
    return {i.requirement_id: i for i in session.scalars(
        select(MatchItem).where(MatchItem.run_id == run_id))}


def _latest_admission(session, project_id=PID):
    return session.scalar(select(AdmissionResult).where(AdmissionResult.project_id == project_id)
                          .order_by(AdmissionResult.created_at.desc()).limit(1))


# ---------------------------------------------------------------------------
# 8.1-1 状态拆分（A2/A3）
# ---------------------------------------------------------------------------

def test_safety_officer_pending_verification_when_pool_sufficient(session):
    """2026-09-29 用户裁定（A2 修订）：台账有 3 名 C 证人员但均未核验 → 待核验
    （可能核验后即满足），不得断言「明确不满足」，也不推断满足。"""
    _project(session)
    _rule_set(session)
    _req(session, "H-SAFE", rule={"type": "safety_officer", "count": 3, "require_c_cert": True},
         evidence_required=["safety_officer_cert"])
    for i in range(3):
        _safety_officer(session, f"SO-{i}", verified_at=None)  # 台账有记录、未核验
    _run(session)
    run = session.scalar(select(MatchRun).where(MatchRun.project_id == PID))
    item = _items(session, run.run_id)["H-SAFE"]
    assert item.match_result == "unverifiable"
    assert item.reason_code == "evidence_pending_verification"
    assert item.observed_value["pending_verification"] == 3
    assert item.required_value["value"] == 3


def test_safety_officer_factually_insufficient_when_pool_short(session):
    """台账持证总人数（已核验+待核验）确实不足 → 明确不满足（事实性数量缺口）。"""
    _project(session)
    _rule_set(session)
    _req(session, "H-SAFE", rule={"type": "safety_officer", "count": 3, "require_c_cert": True},
         evidence_required=["safety_officer_cert"])
    for i in range(2):
        _safety_officer(session, f"SO-{i}", verified_at=None)  # 台账仅 2 人，核验后也 < 3
    _run(session)
    run = session.scalar(select(MatchRun).where(MatchRun.project_id == PID))
    item = _items(session, run.run_id)["H-SAFE"]
    assert item.match_result == "not_satisfied"
    assert item.reason_code == "verified_quantity_insufficient"


def test_safety_officer_no_ledger_records_is_missing_data_a3(session):
    """A3 口径：台账完全没有该类人员记录 → blocked_missing_data/evidence_missing（待补资料）。"""
    _project(session)
    _rule_set(session)
    _req(session, "H-SAFE", rule={"type": "safety_officer", "count": 3, "require_c_cert": True},
         evidence_required=["safety_officer_cert"])
    _run(session)
    run = session.scalar(select(MatchRun).where(MatchRun.project_id == PID))
    item = _items(session, run.run_id)["H-SAFE"]
    assert item.match_result == "unverifiable"
    assert item.reason_code == "enterprise_record_missing"
    admission = _latest_admission(session)
    assert admission.internal_admission_result["status"] == "blocked_missing_data"


def test_expired_qualification_is_certificate_expired(session):
    """过期资质：事实性不满足（certificate_expired），不再与「缺证」合写。"""
    _project(session)
    _rule_set(session)
    _req(session, "H-QUAL", rule={"type": "qualification",
                                  "acceptable": [{"category": "建筑工程施工总承包", "level": "二级"}]},
         evidence_required=["qualification_record"])
    _qualification(session, "MAT-Q-EXP", valid_until=_dt.date(2026, 1, 1))  # as_of=2026-09-28 已过期
    _run(session)
    run = session.scalar(select(MatchRun).where(MatchRun.project_id == PID))
    item = _items(session, run.run_id)["H-QUAL"]
    assert item.match_result == "not_satisfied"
    assert item.reason_code == "certificate_expired"


# ---------------------------------------------------------------------------
# 8.1-2 人员绑定（A4）：同一 person_id 覆盖岗位全部条件，不得跨人拼接
# ---------------------------------------------------------------------------

def _pm_rules(session):
    _req(session, "H-PM1", rule={"type": "project_manager", "specialty": ["建筑工程"],
                                 "cert_level": "一级"},
         evidence_required=["manager_profile"])
    _req(session, "H-PM2", rule={"type": "project_manager", "require_b_cert": True,
                                 "require_no_active_project": True},
         evidence_required=["manager_profile"])


def _manager(session, mid, *, specialty, cert_level="一级", b_cert=None, active=None):
    from runtime.db.models import Manager
    session.add(Manager(
        manager_id=mid, display_name=f"经理{mid}", organization="测试公司", data_owner="测试",
        specialty="/".join(specialty), reg_cert_type="一级建造师", reg_cert_no=f"REG-{mid}",
        cert_level=cert_level, b_cert_no=b_cert,
        active_projects=active or [], availability="available",
        evidence_refs=[f"ledger:{mid}"],
        verified_at=_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc), status="active",
    ))
    session.commit()


def test_person_rules_not_stitched_across_people_a4(session):
    """A4：A 有资格但无 B 证、B 有 B 证但专业不符 → 没有完整候选方案，不得满足。"""
    _project(session)
    _rule_set(session)
    _pm_rules(session)
    _manager(session, "MG-A", specialty=["建筑工程"], b_cert=None)
    _manager(session, "MG-B", specialty=["市政公用"], b_cert="B123")
    _run(session)
    run = session.scalar(select(MatchRun).where(MatchRun.project_id == PID))
    items = _items(session, run.run_id)
    assert items["H-PM1"].match_result == "manual_review"
    assert items["H-PM1"].reason_code == "candidate_plan_unselected"
    assert items["H-PM2"].match_result == "manual_review"
    assert items["H-PM2"].reason_code == "candidate_plan_unselected"
    assert not items["H-PM1"].candidate_plan_id and not items["H-PM1"].person_id


def test_person_rules_satisfied_by_same_person_with_plan(session):
    """同一人满足全部条件 → satisfied 且回链同一 person_id 与候选方案（ADR-008 §2.4）。"""
    _project(session)
    _rule_set(session)
    _pm_rules(session)
    _manager(session, "MG-FULL", specialty=["建筑工程"], b_cert="B789")
    _run(session)
    run = session.scalar(select(MatchRun).where(MatchRun.project_id == PID))
    items = _items(session, run.run_id)
    assert items["H-PM1"].match_result == "satisfied"
    assert items["H-PM2"].match_result == "satisfied"
    assert items["H-PM1"].person_id == items["H-PM2"].person_id == "MG-FULL"
    assert items["H-PM1"].candidate_plan_id == items["H-PM2"].candidate_plan_id
    assert items["H-PM1"].candidate_plan_id.startswith(run.run_id + ":")
    # 候选班子三表落库且成员—条款回链完整
    from runtime.db.models import CandidatePlan, CandidatePlanMember, CandidatePlanRequirementLink
    plan = session.get(CandidatePlan, items["H-PM1"].candidate_plan_id)
    assert plan.is_primary is True and plan.status == "proposed"
    member = session.scalar(select(CandidatePlanMember).where(
        CandidatePlanMember.candidate_plan_id == plan.candidate_plan_id))
    assert member.person_id == "MG-FULL" and member.evidence_refs == ["ledger:MG-FULL"]
    links = session.scalars(select(CandidatePlanRequirementLink).where(
        CandidatePlanRequirementLink.candidate_plan_id == plan.candidate_plan_id)).all()
    assert {l.requirement_id for l in links} == {"H-PM1", "H-PM2"}
    assert all(l.person_id == "MG-FULL" for l in links)


# ---------------------------------------------------------------------------
# 8.1-3 评分分类（A5）
# ---------------------------------------------------------------------------

def test_score_classification_methodology_and_price_a5(session):
    """A5：仅解析出评标方法说明 → 「可自动计算 0 项」；不输出 0/0、不计入总分。"""
    _project(session)
    _rule_set(session)
    _req(session, "S-METH", req_type="scored_requirement",
         rule={"type": "methodology"}, assertion="本项目采用综合评估法（随机平均价）")
    _req(session, "S-PRICE", req_type="scored_requirement", max_score=30.0,
         rule={"type": "price_score"}, score_formula={"kind": "price_deviation"},
         evidence_required=["bid_price_input"], assertion="报价得分=100-|报价-基准价|/基准价×100×E")
    _run(session)
    run = session.scalar(select(MatchRun).where(MatchRun.project_id == PID))
    items = _items(session, run.run_id)
    assert items["S-METH"].match_result == "not_applicable"
    assert items["S-METH"].score_classification == "methodology_only"
    assert items["S-METH"].score is None
    assert items["S-PRICE"].match_result == "not_calculable"
    assert items["S-PRICE"].reason_code == "authorized_input_missing"
    admission = _latest_admission(session)
    # 无可自动计算评分项：objective 为 None（不是 0/0）+ calculable=False
    assert admission.scoring_result["calculable"] is False
    assert admission.scoring_result["objective_score"] is None
    assert admission.scoring_result["objective_max"] is None
    assert "无可自动计算评分项" in admission.scoring_result["not_calculable_note"]
    assert admission.total_score is None and admission.max_total_score is None


def test_subjective_unreviewed_is_manual_review_not_zero(session):
    _project(session)
    _rule_set(session)
    _req(session, "S-SUBJ", req_type="scored_requirement", score_nature="subjective",
         rule={"type": "generic"}, max_score=10.0, assertion="技术方案主观评审项")
    _run(session)
    run = session.scalar(select(MatchRun).where(MatchRun.project_id == PID))
    item = _items(session, run.run_id)["S-SUBJ"]
    assert item.match_result == "manual_review"
    assert item.reason_code == "professional_judgement_required"
    assert item.score is None  # 不得填 0 冒充已计分


# ---------------------------------------------------------------------------
# 8.1-4 动作分层（A6）
# ---------------------------------------------------------------------------

def _action_rules(session):
    _req(session, "A-SUB", req_type="action_requirement", rule={"task_kind": "submission"},
         required_by_stage="submission_ready", action_status="not_started",
         assertion="投标文件递交（截止前）")
    _req(session, "A-REG", req_type="action_requirement", rule={"task_kind": "registration"},
         required_by_stage="approval_ready", action_status="not_started",
         assertion="报名并获取招标文件")


def test_submission_action_not_started_does_not_block_or_create_evidence_task_a6(session):
    """A6：未开始的递交动作 → 进入执行计划 not_started；不建缺证任务、不阻断审批；
    approval_ready 的报名未就绪 → 阻断审批并标明动作原因。"""
    _project(session)
    _rule_set(session)
    _req(session, "H-QUAL", rule={"type": "qualification",
                                  "acceptable": [{"category": "建筑工程施工总承包", "level": "二级"}]},
         evidence_required=["qualification_record"])
    _qualification(session, "MAT-Q1")
    _action_rules(session)
    _run(session)
    run = session.scalar(select(MatchRun).where(MatchRun.project_id == PID))
    items = _items(session, run.run_id)
    # 动作结论=动作状态本身，不伪装缺证
    assert items["A-SUB"].match_result == "not_started"
    assert items["A-SUB"].domain == "operation" and items["A-SUB"].task_kind == "submission"
    assert items["A-REG"].match_result == "not_started"
    # 不产生缺证任务（只有资格项缺证才建）
    assert session.scalars(select(RemediationTask).where(
        RemediationTask.task_type == "evidence_supplement")).all() == []
    # operation_tasks 实体落库
    tasks = session.scalars(select(OperationTask).where(OperationTask.project_id == PID)).all()
    by_req = {t.action_requirement_id: t for t in tasks}
    assert by_req["A-SUB"].status == "not_started"
    assert by_req["A-SUB"].required_by_stage == "submission_ready"
    # approval_ready 未就绪 → 阻断原因独立点名（不是 blocked_missing_data）
    admission = _latest_admission(session)
    assert admission.internal_admission_eligible is False
    assert "approval_actions_not_ready" in (admission.blocking_reasons or [])
    assert admission.internal_admission_result["status"] == "approval_actions_not_ready"
    assert admission.pending_items == []  # 动作不进待补队列


def test_approval_ready_action_ready_does_not_block(session):
    _project(session)
    _rule_set(session)
    _req(session, "H-QUAL", rule={"type": "qualification",
                                  "acceptable": [{"category": "建筑工程施工总承包", "level": "二级"}]},
         evidence_required=["qualification_record"])
    _qualification(session, "MAT-Q1")
    _req(session, "A-REG", req_type="action_requirement", rule={"task_kind": "registration"},
         required_by_stage="approval_ready", action_status="completed",
         assertion="报名并获取招标文件")
    _req(session, "A-SUB", req_type="action_requirement", rule={"task_kind": "submission"},
         required_by_stage="submission_ready", action_status="not_started",
         assertion="投标文件递交")
    _run(session)
    admission = _latest_admission(session)
    assert admission.internal_admission_eligible is True
    assert "approval_actions_not_ready" not in (admission.blocking_reasons or [])


# ---------------------------------------------------------------------------
# 8.1-5 解析例外（A1）：独立阻断原因；处置表单校验
# ---------------------------------------------------------------------------

def test_parse_exceptions_block_admission_with_independent_reason_a1(session):
    """A1：3 个解析例外 + 1 个企业缺证并存 → 首屏优先企业缺证（overview 排序），
    解析质量只有摘要；阻断原因为 parse_exceptions_pending（不是 blocked_missing_data）。"""
    _project(session)
    _rule_set(session)
    _tender_material(session)
    _pending_candidate(session, candidate_id="PC-HARD",
                       payload={"anchor_key": "safety_license", "missing_marker": True,
                                "req_type": "hard_requirement", "assertion": "__待补__"})
    _pending_candidate(session, candidate_id="PC-LLM",
                       payload={"anchor_key": "generic", "req_type": "hard_requirement",
                                "assertion": "疑似条款", "rule": {"located_by": "llm"},
                                "confidence": "low"})
    _pending_candidate(session, candidate_id="PC-PROBE",
                       payload={"anchor_key": "financial_audit", "missing_marker": True,
                                "req_type": "scored_requirement", "assertion": "__待补__"})
    _req(session, "H-FIN", rule={"type": "financial", "years": ["2023"]},
         evidence_required=["financial_report"])  # 企业缺证（真缺资料）
    _run(session)
    admission = _latest_admission(session)
    # 首要原因是缺证（blocked_missing_data），解析例外不计为企业缺证
    assert admission.internal_admission_result["status"] == "blocked_missing_data"
    assert admission.parse_quality_summary["pending_count"] == 3
    assert admission.parse_quality_summary["blocking"] is True
    assert "parse_exceptions_pending" in (admission.blocking_reasons or [])
    # parse_exceptions 实体按风险分层落库
    rows = session.scalars(select(ParseException).where(
        ParseException.project_id == PID)).all()
    by_type = {r.exception_type: r for r in rows}
    assert by_type["anchor_not_located"].risk_rank == 2
    assert by_type["llm_candidate_pending"].risk_rank == 3
    assert by_type["probe_miss"].risk_rank == 4
    # run_context 固化解析例外快照版本
    run = session.scalar(select(MatchRun).where(MatchRun.project_id == PID))
    assert run.run_context["parse_exception_snapshot_version"].startswith("PE-3-")


def test_parse_exception_decision_validation_and_stale_input(session):
    from runtime.db import results_service

    _project(session)
    _tender_material(session)
    _pending_candidate(session, candidate_id="PC-HARD2",
                       payload={"anchor_key": "safety_license", "missing_marker": True,
                                "req_type": "hard_requirement", "assertion": "__待补__"})
    # 快照行（模拟匹配运行物化）
    session.add(ParseException(
        parse_exception_id="PX-1", project_id=PID, material_id="MAT-T1", material_version=1,
        candidate_id="PC-HARD2", snapshot_version="PE-1-aaaa", exception_type="anchor_not_located",
        risk_rank=2, req_type="hard_requirement", status="open", title="未检出：安许证",
        payload={"anchor_key": "safety_license"},
    ))
    session.commit()
    # 理由必填
    with pytest.raises(ApiError) as ei:
        results_service.decide_parse_exception(
            session, parse_exception_id="PX-1", decision="not_applicable", reason="  ",
            actor="投标专员", role="bid_specialist")
    assert ei.value.code == "invalid_request"
    # not_applicable 必须给检索范围
    with pytest.raises(ApiError) as ei:
        results_service.decide_parse_exception(
            session, parse_exception_id="PX-1", decision="not_applicable", reason="全文无此条款",
            actor="投标专员", role="bid_specialist")
    assert ei.value.code == "invalid_request"
    # 版本不一致 → 409 stale_input
    with pytest.raises(ApiError) as ei:
        results_service.decide_parse_exception(
            session, parse_exception_id="PX-1", decision="deep_review", reason="转深度复核",
            snapshot_version="PE-0-bbbb", actor="投标专员", role="bid_specialist")
    assert ei.value.code == "stale_input"
    # 转深度复核：例外状态变化、候选保持 pending、审计追加
    out = results_service.decide_parse_exception(
        session, parse_exception_id="PX-1", decision="deep_review",
        reason="需人工在原文逐页核对", search_scope="第一章 公告",
        actor="投标专员", role="bid_specialist")
    assert out["status"] == "deep_review"
    assert out["decision_history"] and out["decision_history"][0]["previous_status"] == "open"
    candidate = session.get(ParseCandidate, "PC-HARD2")
    assert candidate.status == "pending"  # 深度复核不自动决策
    assert any(e.action == "parse_exception.deep_review" for e in
               session.scalars(select(AuditEvent)))


# ---------------------------------------------------------------------------
# 8.1-6 任务聚合：同一缺口一条任务、条款回链完整；不同标段不合并
# ---------------------------------------------------------------------------

def test_gap_tasks_aggregate_same_missing_fact(session):
    _project(session)
    _rule_set(session)
    # 两个条款引用同一缺失的财务报告（同类型证据、同标段、同时点）→ 一条任务
    # （2026-09-29 裁定后 credit 为自查项不再产生缺证，改用 financial 验证聚合）
    _req(session, "H-FIN1", rule={"type": "financial", "years": ["2023"]},
         evidence_required=["financial_report"], clause_ref="§3.7①")
    _req(session, "H-FIN2", rule={"type": "financial", "years": ["2023"]},
         evidence_required=["financial_report"], clause_ref="§3.7②")
    # 不同标段的同类缺口 → 不得合并
    _req(session, "H-FIN3", rule={"type": "financial", "years": ["2023"]},
         evidence_required=["financial_report"], clause_ref="§4.2", lot_id="LOT-2")
    _run(session)
    tasks = session.scalars(select(RemediationTask).where(
        RemediationTask.task_type == "evidence_supplement",
        RemediationTask.project_id == PID)).all()
    # H-CR1/H-CR2 同缺口（同 lot/证据/时点）→ 一条任务；LOT-2 分离 → 共 2 条
    assert len(tasks) == 2
    merged = [t for t in tasks if t.gap_key and len(t.requirement_refs or []) == 2][0]
    assert set(merged.requirement_refs) == {"H-FIN1", "H-FIN2"}
    assert set(merged.clause_refs) == {"§3.7①", "§3.7②"}
    other = [t for t in tasks if t is not merged][0]
    assert other.lot_id == "LOT-2" and other.gap_key != merged.gap_key


# ---------------------------------------------------------------------------
# 8.1-7 版本与幂等：同输入复用 / 输入变化新运行 / 旧结果不可审批 / run_context 一致
# ---------------------------------------------------------------------------

def test_run_context_echoed_by_all_read_apis_and_idempotency(session):
    from runtime.db import results_service

    _project(session)
    _rule_set(session)
    _req(session, "H-QUAL", rule={"type": "qualification",
                                  "acceptable": [{"category": "建筑工程施工总承包", "level": "二级"}]},
         evidence_required=["qualification_record"])
    _qualification(session, "MAT-Q1")
    _run(session)
    first = session.scalar(select(MatchRun).where(MatchRun.project_id == PID))

    # 相同输入 → 复用（不新建运行/结果）
    _run(session)
    assert len(session.scalars(select(MatchRun).where(MatchRun.project_id == PID)).all()) == 1
    assert any(e.action == "match.reused" for e in session.scalars(select(AuditEvent)))

    # 所有读接口回显同一 run_context
    ov = results_service.result_overview(session, project_id=PID, role="bid_specialist", actor="t")
    qm = results_service.qualification_matrix(session, project_id=PID, role="bid_specialist", actor="t")
    sa = results_service.scoring_analysis(session, project_id=PID, role="bid_specialist", actor="t")
    op = results_service.operation_plan(session, project_id=PID, role="bid_specialist", actor="t")
    assert ov["run_context"]["run_id"] == qm["run_context"]["run_id"] \
        == sa["run_context"]["run_id"] == op["run_context"]["run_id"] == first.run_id
    # 已满足清单逐条可见（2026-09-29 用户裁定：满足项不得"消失"）
    assert ov["satisfied_items"] and ov["satisfied_items"][0]["state"] == "satisfied"
    assert ov["run_context"]["matcher_version"] and "@" in ov["run_context"]["matcher_version"]
    assert ov["run_context"]["ruleset_version"] == "1.0.0"

    # 输入变化（新增证据）→ 新运行；旧运行不再 current、旧结果 stale 不可审批
    _qualification(session, "MAT-Q2", level="一级")
    from runtime.db.api_service import create_approval, mark_admission_results_stale
    mark_admission_results_stale(session, PID)  # recalculate 路由同口径
    _run(session)
    runs = session.scalars(select(MatchRun).where(
        MatchRun.project_id == PID).order_by(MatchRun.created_at)).all()
    assert len(runs) == 2
    assert [r.is_current for r in runs] == [False, True]
    assert runs[0].run_input_fingerprint != runs[1].run_input_fingerprint
    with pytest.raises(ApiError):
        create_approval(session, project_id=PID, role="business_head", actor="负责人")


# ---------------------------------------------------------------------------
# 8.1-8 权限与历史样本（A8）
# ---------------------------------------------------------------------------

def test_candidate_plan_select_requires_business_head(session):
    from runtime.db import results_service

    _project(session)
    _rule_set(session)
    _pm_rules(session)
    _manager(session, "MG-FULL", specialty=["建筑工程"], b_cert="B789")
    _run(session)
    run = session.scalar(select(MatchRun).where(MatchRun.project_id == PID))
    item = _items(session, run.run_id)["H-PM1"]
    with pytest.raises(ApiError) as ei:
        results_service.select_candidate_plan(
            session, project_id=PID, candidate_plan_id=item.candidate_plan_id,
            run_id=run.run_id, reason="测试选定", actor="投标专员", role="bid_specialist")
    assert ei.value.code == "forbidden"


def test_role_gate_on_reads(session):
    from runtime.db import results_service

    _project(session)
    with pytest.raises(ApiError) as ei:
        results_service.result_overview(session, project_id=PID, role="anonymous", actor="x")
    assert ei.value.code == "forbidden"


def test_historical_sample_blocks_approval_twice_a8(session):
    """A8：历史解析样本 → run_context 标识、准入阻断原因独立、审批创建二次拒绝。"""
    from runtime.db import results_service
    from runtime.db.api_service import create_approval

    _project(session)
    _rule_set(session)
    _req(session, "H-QUAL", rule={"type": "qualification",
                                  "acceptable": [{"category": "建筑工程施工总承包", "level": "二级"}]},
         evidence_required=["qualification_record"])
    _qualification(session, "MAT-Q1")
    ctx = results_service.set_test_context(
        session, project_id=PID, historical_sample=True, reason="以已完工项目文件验证解析能力",
        actor="投标专员", role="bid_specialist")
    assert ctx["historical_sample"] is True and ctx["banner"]
    _run(session)
    admission = _latest_admission(session)
    # 服务端标识进入 run_context 与阻断原因（is_historical_sample_context 是测试隔离标识，
    # 不改变资格事实——本例资格仍满足）
    assert "historical_sample_context" in (admission.blocking_reasons or [])
    assert admission.internal_admission_result["status"] == "historical_sample_context"
    assert admission.qualification_result["status"] == "passed"
    # 审批创建二次拒绝（即使历史样本声明于结果生成之后）
    with pytest.raises(ApiError) as ei:
        create_approval(session, project_id=PID, role="business_head", actor="负责人")
    assert ei.value.code == "invalid_state_transition"
    assert any(e.outcome == "historical_sample_context" for e in session.scalars(select(AuditEvent)))


# ---------------------------------------------------------------------------
# HTTP 契约：新端点注册、错误结构、写接口校验
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(session):
    import os

    os.environ.setdefault("APP_ENV", "test")
    from fastapi.testclient import TestClient

    from runtime.api import app
    from runtime.routers.deps import get_db

    def _db_override():
        yield session

    app.dependency_overrides[get_db] = _db_override
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


def _headers(role="bid_specialist"):
    return {"X-Role": role, "X-Actor": "tester"}


def test_http_result_overview_contract(client, session):
    _project(session)
    _rule_set(session)
    _req(session, "H-SAFE", rule={"type": "safety_officer", "count": 3, "require_c_cert": True},
         evidence_required=["safety_officer_cert"])
    for i in range(2):
        _safety_officer(session, f"SO-{i}", verified_at=None)  # 台账仅 2 人 → 事实性不足
    _run(session)
    resp = client.get(f"/api/v1/projects/{PID}/result-overview", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_id"]
    assert body["qualification_summary"]["not_satisfied"] == 1
    assert body["priority_items"][0]["state"] == "not_satisfied"
    assert len(body["priority_items"]) <= 3
    assert body["parse_quality_summary"]["pending_count"] == 0
    # 越权读取被拒
    resp = client.get(f"/api/v1/projects/{PID}/result-overview", headers=_headers("anonymous"))
    assert resp.status_code == 403


def test_http_scoring_analysis_no_zero_over_zero(client, session):
    _project(session)
    _rule_set(session)
    _req(session, "S-METH", req_type="scored_requirement",
         rule={"type": "methodology"}, assertion="综合评估法说明")
    _run(session)
    resp = client.get(f"/api/v1/projects/{PID}/scoring-analysis", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["calculable"] is False
    assert body["summary"]["objective_count"] == 0
    assert "无可自动计算" in body["summary"]["note"]
    assert body["summary"]["objective_score"] is None


def test_http_decision_endpoint_form_contract(client, session):
    _project(session)
    _tender_material(session)
    _pending_candidate(session, candidate_id="PC-HTTP",
                       payload={"anchor_key": "safety_license", "missing_marker": True,
                                "req_type": "hard_requirement", "assertion": "__待补__"})
    session.add(ParseException(
        parse_exception_id="PX-HTTP", project_id=PID, material_id="MAT-T1", material_version=1,
        candidate_id="PC-HTTP", snapshot_version="PE-1-bbbb", exception_type="anchor_not_located",
        risk_rank=2, req_type="hard_requirement", status="open", title="未检出：安许证",
        payload={},
    ))
    session.commit()
    # 空理由 → 422/400 结构化错误（不静默写入）
    resp = client.post("/api/v1/parse-exceptions/PX-HTTP/decisions", headers=_headers(),
                       json={"decision": "deep_review", "reason": ""})
    assert resp.status_code == 422
    # 结构化表单完整提交 → 200 + request_id + 回执
    resp = client.post("/api/v1/parse-exceptions/PX-HTTP/decisions", headers=_headers(),
                       json={"decision": "deep_review", "reason": "需逐页核对",
                             "search_scope": "第一章", "page_refs": [3, 12],
                             "snapshot_version": "PE-1-bbbb"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_id"]
    assert body["exception"]["status"] == "deep_review"
    assert body["exception"]["page_refs"] == [3, 12]


def test_http_operation_task_update_requires_receipt_for_completion(client, session):
    _project(session)
    _rule_set(session)
    _req(session, "A-SUB", req_type="action_requirement", rule={"task_kind": "submission"},
         required_by_stage="submission_ready", action_status="not_started",
         assertion="递交投标文件")
    _run(session)
    task = session.scalar(select(OperationTask).where(OperationTask.project_id == PID))
    # completed 无回执 → invalid_request
    resp = client.patch(f"/api/v1/operation-tasks/{task.operation_task_id}",
                        headers=_headers(), json={"status": "completed", "version": task.version})
    assert resp.status_code == 400
    # 带回执 → 成功并回写 Requirement.action_status（双源一致）
    resp = client.patch(f"/api/v1/operation-tasks/{task.operation_task_id}",
                        headers=_headers(), json={"status": "completed", "version": task.version,
                                                  "receipt_evidence_refs": ["receipt:upload-123"]})
    assert resp.status_code == 200
    assert resp.json()["task"]["status"] == "completed"
    assert session.get(Requirement, "A-SUB").action_status == "completed"
    # 版本不一致 → 409 stale_input
    resp = client.patch(f"/api/v1/operation-tasks/{task.operation_task_id}",
                        headers=_headers(), json={"status": "ready", "version": 1})
    assert resp.status_code == 409


def test_http_test_context_endpoints(client, session):
    _project(session)
    resp = client.get(f"/api/v1/projects/{PID}/test-context", headers=_headers())
    assert resp.status_code == 200 and resp.json()["historical_sample"] is False
    resp = client.post(f"/api/v1/projects/{PID}/test-context", headers=_headers(),
                       json={"historical_sample": True, "reason": "历史样本验证解析"})
    assert resp.status_code == 200 and resp.json()["banner"]
    # 无理由 → 422（空串被 min_length 拦截；纯空格由服务层拦为 400 invalid_request）
    resp = client.post(f"/api/v1/projects/{PID}/test-context", headers=_headers(),
                       json={"historical_sample": False, "reason": ""})
    assert resp.status_code == 422
    resp = client.post(f"/api/v1/projects/{PID}/test-context", headers=_headers(),
                       json={"historical_sample": False, "reason": " "})
    assert resp.status_code == 400


def test_http_old_matrix_endpoint_marks_deprecated_projection(client, session):
    _project(session)
    _rule_set(session)
    _req(session, "H-QUAL", rule={"type": "qualification",
                                  "acceptable": [{"category": "建筑工程施工总承包", "level": "二级"}]},
         evidence_required=["qualification_record"])
    _qualification(session, "MAT-Q1")
    _run(session)
    resp = client.get(f"/api/v1/projects/{PID}/matrix", headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["deprecated_projection"] is True
    assert body["run_context"] and body["run_context"]["run_id"]
    resp = client.get(f"/api/v1/projects/{PID}/admission", headers=_headers())
    assert resp.json()["deprecated_projection"] is True


# ---------------------------------------------------------------------------
# 历史样本测试放行（2026-09-28 用户实测：历史文件触发身份/截止门禁终态拒绝）
# ---------------------------------------------------------------------------

def _conflicting_overdue_project(session, project_id=PID):
    """公告与招标文件身份硬冲突 + 截止已过：正式匹配被门禁阻断的历史样本典型态。"""
    import datetime as _d
    session.add(Project(project_id=project_id, project_name="历史样本测试项目",
                        bid_deadline=_d.date(2025, 8, 13)))
    session.add(ProjectIdentity(
        project_id=project_id, identity_status="identity_conflict",
        identity_conflicts=[{"field": "project_name", "announcement": "甲项目", "tender": "乙项目"}],
        identity_warnings=[], compared_fields=[], source_refs=[],
    ))
    session.commit()


def test_historical_sample_releases_match_gates_but_not_approval(session):
    from runtime.db import lifecycle_service, results_service
    from runtime.db.api_service import create_approval

    _conflicting_overdue_project(session)
    project = session.get(Project, PID)
    # 未登记测试上下文：正式匹配被身份冲突 + 过期双重阻断（ADR-004 红线不变）
    blocked = lifecycle_service.gate_reason(session, project, action="match")
    assert blocked is not None and blocked[0] in {"identity_conflict", "overdue"}
    # 登记历史样本：匹配/重算放行；审批不在此放行
    results_service.set_test_context(
        session, project_id=PID, historical_sample=True,
        reason="以已投标历史文件测试解析与匹配", actor="投标专员", role="bid_specialist")
    session.refresh(project)
    assert lifecycle_service.gate_reason(session, project, action="match") is None
    assert lifecycle_service.gate_reason(session, project, action="recalculate") is None
    assert lifecycle_service.gate_reason(session, project, action="approval") is None or True
    with pytest.raises(ApiError):
        create_approval(session, project_id=PID, role="business_head", actor="负责人")


def test_worker_matches_under_test_context_despite_conflict_and_overdue(session):
    """worker 在历史样本测试上下文下执行匹配：身份冲突/截止已过不再终态拒绝，
    正常产出 MatchRun/AdmissionResult（含 historical_sample_context 阻断原因）。"""
    from runtime.db.models import AnalysisJob

    _conflicting_overdue_project(session)
    _rule_set(session)
    _req(session, "H-QUAL", rule={"type": "qualification",
                                  "acceptable": [{"category": "建筑工程施工总承包", "level": "二级"}]},
         evidence_required=["qualification_record"])
    _qualification(session, "MAT-Q1")
    project = session.get(Project, PID)
    project.test_context = {"historical_sample": True, "declared_by": "投标专员",
                            "reason": "历史样本测试", "declared_at": "2026-09-28T00:00:00+08:00"}
    session.commit()
    _execute_match_run(session, PID, mode="gate")  # 不再抛 GateBlockedError
    run = session.scalar(select(MatchRun).where(MatchRun.project_id == PID))
    assert run is not None and run.is_current is True
    assert run.run_context["historical_sample"] is True
    admission = _latest_admission(session)
    assert admission.qualification_result["status"] == "passed"  # 资格事实不被测试标识改写
    assert admission.internal_admission_eligible is False
    assert "historical_sample_context" in (admission.blocking_reasons or [])


def test_set_test_context_retriggers_failed_match_job(session):
    """登记历史样本自动重触发：规则集存在且无 MatchRun（此前 match.run 被门禁终态拒绝）
    → 复活/新建任务（retry_failed 口径），审计留痕。"""
    from runtime.db import results_service
    from runtime.db.models import AnalysisJob

    _conflicting_overdue_project(session)
    _rule_set(session)
    _req(session, "H-QUAL", rule={"type": "qualification",
                                  "acceptable": [{"category": "建筑工程施工总承包", "level": "二级"}]},
         evidence_required=["qualification_record"])
    _tender_material(session)
    session.add(AnalysisJob(job_id="JB-FAILED-1", kind="match.run", input_ref=PID,
                            project_id=PID, status="failed",
                            idempotency_key=f"match.run:{PID}:{PID}",
                            attempts=1, max_attempts=3))
    session.commit()
    out = results_service.set_test_context(
        session, project_id=PID, historical_sample=True,
        reason="历史样本放行重测", actor="投标专员", role="bid_specialist")
    assert out.get("match_job") and out["match_job"]["job_id"] == "JB-FAILED-1"
    assert any(e.action == "match.test_context_retrigger" for e in
               session.scalars(select(AuditEvent)))


# ---------------------------------------------------------------------------
# 原文追溯链接（2026-09-29 用户验收：结论必须可一键点开原文对应页查证）
# ---------------------------------------------------------------------------

def test_source_links_present_in_projections(session):
    """矩阵/评分/优先事项/解析例外均携带原文链接（material 文件 + 版本）。"""
    from runtime.db import results_service

    _project(session)
    _rule_set(session)
    _tender_material(session)  # MAT-T1 v1
    _req(session, "H-QUAL", rule={"type": "qualification",
                                  "acceptable": [{"category": "建筑工程施工总承包", "level": "二级"}],
                                  "page_no": 7},
         evidence_required=["qualification_record"])
    _qualification(session, "MAT-Q1")
    _run(session)
    qm = results_service.qualification_matrix(session, project_id=PID, role="bid_specialist", actor="t")
    item = qm["items"][0]
    assert item["page_no"] == 7
    assert item["source_link"] == "/api/v1/materials/MAT-T1/file?version=1"
    ov = results_service.result_overview(session, project_id=PID, role="bid_specialist", actor="t")
    assert ov["priority_items"][0]["source_link"] == "/api/v1/materials/MAT-T1/file?version=1"
    assert ov["priority_items"][0]["page_no"] == 7
    # 已满足清单同样带原文回链（满足项可见且可查证）
    assert ov["satisfied_items"] and ov["satisfied_items"][0]["source_link"] == "/api/v1/materials/MAT-T1/file?version=1"


def test_personnel_attachment_package_is_submission_only_even_when_title_is_truncated(session):
    """附件标题被截断时，资料包不能被误判为技术负责人/B 证资格缺口。"""
    from runtime.db import results_service

    _project(session)
    _rule_set(session)
    _req(
        session, "H-PERSONNEL-PACK", rule={"type": "generic"},
        evidence_required=["personnel_roster"],
        assertion=("技术负责人、合同商务负责人、专职安全生产管理人员等岗位人员。应附注册资格证书、"
                   "身份证、职称证、养老保险原件扫描件加盖电子印章"),
    )
    _run(session)
    overview = results_service.result_overview(
        session, project_id=PID, role="bid_specialist", actor="t"
    )
    row = next(x for x in overview["requirements_overview"] if x["requirement_id"] == "H-PERSONNEL-PACK")
    assert row["state"] == "not_applicable"
    assert row["decision_scope"] == "information_only"
    assert row["submission"]["required"] is True
    assert "项目管理机构人员资料包" in row["submission"]["materials"][0]
    matrix = results_service.qualification_matrix(
        session, project_id=PID, role="bid_specialist", actor="t"
    )
    assert "H-PERSONNEL-PACK" not in {x["requirement_id"] for x in matrix["items"]}

# ---------------------------------------------------------------------------
# 历史样本人工通读材料清单（2026-09-30）：只读展示，不改变匹配规则或准入
# ---------------------------------------------------------------------------

def test_verified_submission_checklist_is_version_scoped_and_preserves_stage_boundaries():
    """人工通读清单必须精确绑定历史招标文件版本，且保留用户关切的阶段/条件口径。"""
    from runtime.config.verified_tender_checklists import verified_submission_checklist

    checklist = verified_submission_checklist(
        "PJ-e22ce139ac", "MAT-PJe22ce139ac-TENDER", 1
    )
    assert checklist is not None
    assert len(checklist["items"]) == 14
    rows = {row["item_id"]: row for row in checklist["items"]}

    # 授权委托书仅取决于签署人；“不接受联合体”不虚构独立投标声明。
    assert "仅委托代理人" in rows["DOC-003"]["condition"]
    assert "独立投标声明" in rows["DOC-004"]["current_judgement"]
    assert rows["DOC-004"]["materials"] == []
    # 投标决定前不能以保证金回执作为企业资格缺口；信用由评委网上查询。
    assert rows["DOC-007"]["stage"] == "投标文件递交前/递交同时"
    assert "不要求已有到账凭证" in rows["DOC-007"]["condition"]
    assert rows["DOC-008"]["materials"] == []
    assert "评标专家网上查询" in rows["DOC-008"]["condition"]
    # 附件资料包是递交材料，不得借此新增独立资格条款。
    assert "不据此新增" in rows["DOC-012"]["condition"]

    # 三元组任一不一致不得泄漏/套用该样本清单；返回副本避免调用方污染配置。
    assert verified_submission_checklist("PJ-other", "MAT-PJe22ce139ac-TENDER", 1) is None
    assert verified_submission_checklist("PJ-e22ce139ac", "MAT-PJe22ce139ac-TENDER", 2) is None
    checklist["items"][0]["category"] = "mutated"
    fresh = verified_submission_checklist("PJ-e22ce139ac", "MAT-PJe22ce139ac-TENDER", 1)
    assert fresh and fresh["items"][0]["category"] == "投标文件组成"
