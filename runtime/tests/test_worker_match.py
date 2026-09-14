# docs/07 方案 §3.5：worker 匹配执行器链路测试（sqlite 内存库，rag 标记）
# 覆盖：缺规则集/无 as_of 可解释失败（不伪造成功）、
# RAG 候选核验 → 规则引擎 → MatchRun/MatchItem 快照落库、
# 候选核验 blocked 记录剔除、索引未就绪降级为结构化快照判定。
from __future__ import annotations

import datetime as _dt
import re as _re

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from runtime.core.matching import MatchNotRunnableError
from runtime.core.errors import ApiError
from runtime.db.models import (
    AdmissionResult,
    AuditEvent,
    Base,
    KnowledgeChunk,
    MatchItem,
    MatchRun,
    Project,
    Qualification,
    Requirement,
    RuleSet,
)
from runtime.worker import _execute_match_run

pytestmark = pytest.mark.rag

# sqlite 不支持 PostgreSQL 多 schema（public_data 等）：把 schema 前缀从
# DDL/DML 中剥离（仅测试库；生产仍为 PG 四层 schema）。
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


def _project(session, project_id="ND-2025") -> None:
    session.add(Project(project_id=project_id, project_name="农大项目"))
    session.commit()


def _rule_set(session, project_id="ND-2025", rule_set_id="RS-1") -> None:
    session.add(RuleSet(rule_set_id=rule_set_id, project_id=project_id,
                        version="1.0.0", created_by="测试", snapshot={}))
    session.commit()


def _requirement(session, requirement_id="NQ-H-001", as_of="2025-10-30", **kw) -> None:
    rule = kw.pop("rule", {"type": "qualification", "qualification_type": "建筑工程施工总承包",
                           "level": "二级"})
    failure_effect = kw.pop("failure_effect", "not_qualified")
    session.add(Requirement(
        requirement_id=requirement_id, rule_set_id="RS-1", req_type="hard_requirement",
        category="资质", clause_ref="第三章四(1)", assertion="具备建筑工程施工总承包二级及以上资质",
        rule=rule, evidence_required=["qualification_record"],
        missing_action="blocked_missing_data", failure_effect=failure_effect,
        as_of=as_of, **kw,
    ))
    session.commit()


def _qualification(session, material_id="MAT-Q-1", **kw) -> None:
    data = dict(
        qualification_id=f"Q-{material_id}", material_id=material_id,
        category="建筑工程施工总承包", level="二级",
        valid_from=None, valid_until=None, evidence_refs=[],
        data_owner="数据管理员甲", verified_at=_dt.datetime(2025, 1, 10, tzinfo=_dt.timezone.utc),
        status="active",
    )
    data.update(kw)
    session.add(Qualification(**data))
    session.commit()


def _chunk(session, chunk_id="CH-1", material_id="MAT-Q-1", layer="L3_enterprise") -> None:
    session.add(KnowledgeChunk(
        chunk_id=chunk_id, chunk_seq=1, knowledge_layer=layer,
        material_id=material_id, material_version=1, content_hash="a" * 64,
        text="具备建筑工程施工总承包二级及以上资质", page_no=1, paragraph_no=1,
        project_id="ND-2025", owner_type="enterprise", permission_scope="enterprise_read",
        classification="internal", verification_status="active",
        embedding_model="test-embed", index_version="iv-1", index_status="current",
    ))
    session.commit()


def _fake_evaluate(requirements, evidence, *, as_of, mode="gate", lot_id=None):
    return {
        "matrix": [{
            "requirement_id": requirements[0]["requirement_id"],
            "req_type": "hard_requirement", "clause_ref": requirements[0]["clause_ref"],
            "match_result": "satisfied", "match_reason": "核验通过", "score": None,
        }],
        "coverage": {"executed": len(requirements), "declared": len(requirements),
                     "complete": True},
        "blocked": [], "pending": [], "review": [],
        "qualification_result": "passed", "scoring_result": "full",
        "operational_readiness": "ready", "internal_admission_eligible": True,
    }


def _fake_retrieve(session, request, vis, *, role=""):
    from runtime.rag.schemas import ChunkLocation, KnowledgeChunkDTO, SearchResponse

    return SearchResponse(
        request_id="req-1", retrieval_run_id="rr-1", candidate_only=True,
        insufficient_evidence=False, index_version="iv-1",
        items=[KnowledgeChunkDTO(
            chunk_id="CH-1", knowledge_layer="L3_enterprise", material_id="MAT-Q-1",
            material_version=1, content_hash="a" * 64, text="具备建筑工程施工总承包二级及以上资质",
            location=ChunkLocation(page_no=1, paragraph=1), verification_status="active",
            permission_scope="enterprise_read", project_id="ND-2025",
            retrieval_score=0.5, citation="MAT-Q-1:v1:p1",
        )],
    )


# ---------- 不可执行（不伪造成功） ----------

def test_match_run_requires_rule_set(session):
    _project(session)
    with pytest.raises(MatchNotRunnableError):
        _execute_match_run(session, "ND-2025")


def test_match_run_requires_as_of(session):
    _project(session)
    _rule_set(session)
    _requirement(session, as_of=None)
    with pytest.raises(MatchNotRunnableError):
        _execute_match_run(session, "ND-2025")


# ---------- 完整链路：核验 → 引擎 → 快照 ----------

def test_match_run_writes_snapshot(session):
    _project(session)
    _rule_set(session)
    _requirement(session)
    _qualification(session)
    _chunk(session)

    _execute_match_run(session, "ND-2025", evaluate_fn=_fake_evaluate, retrieve_fn=_fake_retrieve)

    run = session.scalar(select(MatchRun).where(MatchRun.project_id == "ND-2025"))
    assert run is not None
    assert run.status == "completed"
    assert run.coverage == {"executed": 1, "declared": 1, "complete": True}
    assert run.retrieval_run_id == "rr-1"
    assert run.index_version == "iv-1"
    assert run.candidate_chunk_ids == ["CH-1"]
    assert run.evidence_snapshot_hash and len(run.evidence_snapshot_hash) == 64
    verification = run.structured_verification
    assert verification["rag_degraded"] is None
    assert verification["checked"][0]["requirement_id"] == "NQ-H-001"
    assert verification["checked"][0]["passed"][0]["material_id"] == "MAT-Q-1"
    assert verification["removed_material_ids"] == []

    item = session.scalar(select(MatchItem).where(MatchItem.run_id == run.run_id))
    assert item is not None
    assert item.match_result == "satisfied"
    assert item.match_reason["retrieval_run_id"] == "rr-1"
    assert item.evidence_refs == ["MAT-Q-1:v1:p1"]

    events = session.scalars(select(AuditEvent)).all()
    assert any(e.action == "match.completed" for e in events)


def test_match_run_removes_blocked_candidates(session):
    _project(session)
    _rule_set(session)
    _requirement(session)
    # 记录 active 但等级无法判定 → 候选核验 blocked → 从判定输入剔除（unverifiable 语义）
    _qualification(session, level="待核实")
    _chunk(session)

    _execute_match_run(session, "ND-2025", evaluate_fn=_fake_evaluate, retrieve_fn=_fake_retrieve)

    run = session.scalar(select(MatchRun).where(MatchRun.project_id == "ND-2025"))
    assert run.structured_verification["removed_material_ids"] == ["MAT-Q-1"]
    assert run.structured_verification["checked"][0]["blocked"][0]["material_id"] == "MAT-Q-1"
    # 被剔除记录不得进入规则输入：判定证据快照为空
    from runtime.core import matching

    assert run.evidence_snapshot_hash == matching.snapshot_hash({})
    # 未通过核验的候选不得引用为证据
    item = session.scalar(select(MatchItem).where(MatchItem.run_id == run.run_id))
    assert item.evidence_refs is None


# ---------- 可解释降级 ----------

def test_match_run_degrades_without_index(session):
    _project(session)
    _rule_set(session)
    _requirement(session)
    _qualification(session)
    # 无任何 chunk：hybrid_search 抛 KnowledgeNotReadyError → 降级为结构化快照判定
    _execute_match_run(session, "ND-2025", evaluate_fn=_fake_evaluate)

    run = session.scalar(select(MatchRun).where(MatchRun.project_id == "ND-2025"))
    assert run is not None
    assert run.status == "completed"  # 降级完成，但快照如实记录降级原因
    assert "rag_degraded" in run.structured_verification
    assert run.structured_verification["rag_degraded"] is not None
    assert run.candidate_chunk_ids == []
    assert run.retrieval_run_id is None


# ---------- 审批门禁（方案 §3.6：缺 RAG 快照/stale 不得创建审批） ----------

def _full_score_result(session, result_id="AR-1", run_id="MR-1", freshness="current") -> None:
    from runtime.db.models import AdmissionResult

    session.add(AdmissionResult(
        result_id=result_id, run_id=run_id, project_id="ND-2025", rule_set_id="RS-1",
        qualification_result={"status": "passed"}, scoring_result={},
        operational_readiness={}, internal_admission_result={},
        internal_admission_eligible=True, result_freshness=freshness, state="final",
    ))
    session.commit()


def _rag_complete_run(session, run_id="MR-1") -> None:
    session.add(MatchRun(
        run_id=run_id, project_id="ND-2025", rule_set_id="RS-1", as_of="2025-10-30",
        mode="gate", coverage={"executed": 1, "declared": 1, "complete": True},
        status="completed", retrieval_run_id="rr-1", index_version="iv-1",
        candidate_chunk_ids=["CH-1"],
        structured_verification={"rag_degraded": None, "checked": [], "removed_material_ids": []},
        evidence_snapshot_hash="e" * 64,
    ))
    session.commit()


def test_approval_denied_without_rag_snapshot(session):
    from runtime.core import rbac
    from runtime.db import api_service

    _project(session)
    _full_score_result(session, run_id="MR-NO-RAG")
    # 满分结果但匹配运行无 RAG 快照 → 拒绝创建审批（F024 §2）
    with pytest.raises(ApiError) as excinfo:
        api_service.create_approval(
            session, project_id="ND-2025", role=rbac.BUSINESS_HEAD, actor="经营负责人"
        )
    assert excinfo.value.code == "invalid_state_transition"
    assert "RAG 快照" in excinfo.value.message
    events = session.scalars(select(AuditEvent)).all()
    assert any(e.action == "approval.create_denied" and e.outcome.startswith("missing_rag_snapshot")
               for e in events)


def test_approval_denied_with_stale_result(session):
    from runtime.core import rbac
    from runtime.db import api_service

    _project(session)
    _rag_complete_run(session)
    _full_score_result(session, freshness="stale")
    with pytest.raises(ApiError) as excinfo:
        api_service.create_approval(
            session, project_id="ND-2025", role=rbac.BUSINESS_HEAD, actor="经营负责人"
        )
    assert excinfo.value.code == "invalid_state_transition"
    assert "过期" in excinfo.value.message


def test_approval_allowed_with_rag_snapshot(session):
    from runtime.core import rbac
    from runtime.db import api_service

    _project(session)
    _rag_complete_run(session)
    _full_score_result(session)
    data = api_service.create_approval(
        session, project_id="ND-2025", role=rbac.BUSINESS_HEAD, actor="经营负责人"
    )
    assert data["state"] == "pending_bid_approval"
    assert data["admission_result_ref"].endswith("AR-1")


# ---------- R024：admission_results 生成执行器（F023 §2 第 6 步 / F008 §4.6） ----------

def _fake_evaluate_factory(hard_results: dict[str, str]):
    """按 requirement_id → match_result 映射产出 engine_result（全 hard 场景）。"""

    def fn(requirements, evidence, *, as_of, mode="gate", lot_id=None):
        matrix, blocked, pending, review = [], [], [], []
        for item in requirements:
            result = hard_results.get(item["requirement_id"], "satisfied")
            reason = {
                "satisfied": "核验通过",
                "not_satisfied": "证据充分但明确不满足",
                "unverifiable": "关键证据缺失或无法核验",
                "manual_review": "需人工复核",
            }[result]
            entry = {
                "requirement_id": item["requirement_id"], "req_type": item["req_type"],
                "clause_ref": item["clause_ref"], "match_result": result,
                "match_reason": reason, "score": None,
            }
            matrix.append(entry)
            if result == "not_satisfied":
                blocked.append(entry)
            elif result == "unverifiable":
                pending.append(entry)
            elif result == "manual_review":
                review.append(entry)
        hard = [m for m in matrix if m["req_type"] == "hard_requirement"]
        hard_ids = {m["requirement_id"] for m in hard}
        qualification = (
            "failed" if any(m["requirement_id"] in hard_ids for m in blocked)
            else ("pending" if any(m["requirement_id"] in hard_ids for m in pending)
                  else "passed")
        )
        return {
            "as_of": as_of, "mode": mode, "lot_id": lot_id, "matrix": matrix,
            "coverage": {"executed": len(matrix), "declared": len(requirements),
                         "complete": len(matrix) == len(requirements)},
            "blocked": blocked, "pending": pending, "review": review,
            "qualification_result": qualification,
            "scoring_result": "full", "operational_readiness": "ready",
            "internal_admission_eligible": (
                not blocked and not pending and not review
                and len(matrix) == len(requirements)
            ),
        }

    return fn


def _admissions(session, project_id="ND-2025"):
    return session.scalars(
        select(AdmissionResult)
        .where(AdmissionResult.project_id == project_id)
        .order_by(AdmissionResult.created_at)
    ).all()


def test_match_run_generates_full_score_admission(session):
    _project(session)
    _rule_set(session)
    _requirement(session)
    _qualification(session)
    _chunk(session)

    _execute_match_run(session, "ND-2025", evaluate_fn=_fake_evaluate, retrieve_fn=_fake_retrieve)

    results = _admissions(session)
    assert len(results) == 1
    ar = results[0]
    assert ar.run_id.startswith("MR-")
    assert ar.internal_admission_eligible is True
    assert ar.result_freshness == "current"
    assert ar.qualification_result["status"] == "passed"
    assert ar.qualification_result["satisfied"] == 1
    assert ar.qualification_result["total"] == 1
    assert ar.internal_admission_result["status"] == "qualified_full_score"
    assert ar.blocked_items == [] and ar.pending_items == [] and ar.review_items == []
    # 项目准入状态迁移：matching 前态 None → qualified_full_score（ADR-001 §2.2）
    project = session.get(Project, "ND-2025")
    assert project.admission_status == "qualified_full_score"
    events = session.scalars(select(AuditEvent)).all()
    assert any(
        e.action == "admission.generated" and e.outcome.startswith("eligible=True")
        for e in events
    )


def test_match_run_blocked_missing_maps_project_state(session):
    _project(session)
    _rule_set(session)
    _requirement(session)
    _qualification(session)

    _execute_match_run(
        session, "ND-2025",
        evaluate_fn=_fake_evaluate_factory({"NQ-H-001": "unverifiable"}),
    )

    results = _admissions(session)
    assert len(results) == 1
    ar = results[0]
    assert ar.internal_admission_eligible is False
    assert ar.internal_admission_result["status"] == "blocked_missing_data"
    assert ar.pending_items and ar.pending_items[0]["req"] == "NQ-H-001"
    assert ar.blocked_items == []
    project = session.get(Project, "ND-2025")
    assert project.admission_status == "blocked_missing_data"


def test_match_run_hard_failure_splits_qualification_vs_response(session):
    # 资格性失败（failure_effect=not_qualified）→ not_qualified
    _project(session)
    _rule_set(session)
    _requirement(session, requirement_id="NQ-H-001", failure_effect="not_qualified")
    _qualification(session)
    _execute_match_run(
        session, "ND-2025",
        evaluate_fn=_fake_evaluate_factory({"NQ-H-001": "not_satisfied"}),
    )
    ar = _admissions(session)[0]
    assert ar.internal_admission_result["status"] == "not_qualified"
    assert session.get(Project, "ND-2025").admission_status == "not_qualified"

    # 响应性硬失败（failure_effect=blocked_hard_requirement）→ blocked_hard_requirement
    _project(session, project_id="ND-2025-B")
    _rule_set(session, project_id="ND-2025-B", rule_set_id="RS-B")
    session.add(Requirement(
        requirement_id="NQ-R-001", rule_set_id="RS-B", req_type="hard_requirement",
        category="响应性", clause_ref="第三章四(1)",
        assertion="响应性硬要求", rule={"type": "response", "evidence_type": "response_document"},
        evidence_required=["response_document"], missing_action="blocked_hard_requirement",
        failure_effect="blocked_hard_requirement", as_of="2025-10-30",
    ))
    session.commit()
    _execute_match_run(
        session, "ND-2025-B",
        evaluate_fn=_fake_evaluate_factory({"NQ-R-001": "not_satisfied"}),
    )
    ar_b = _admissions(session, project_id="ND-2025-B")[0]
    assert ar_b.internal_admission_result["status"] == "blocked_hard_requirement"
    assert session.get(Project, "ND-2025-B").admission_status == "blocked_hard_requirement"


def test_match_run_recalculate_marks_previous_stale(session):
    _project(session)
    _rule_set(session)
    _requirement(session)
    _qualification(session)

    _execute_match_run(session, "ND-2025", evaluate_fn=_fake_evaluate)
    _execute_match_run(session, "ND-2025", evaluate_fn=_fake_evaluate)

    results = _admissions(session)
    assert len(results) == 2
    assert results[0].result_freshness == "stale"      # 旧快照失效（F023 §4）
    assert results[1].result_freshness == "current"
    events = session.scalars(select(AuditEvent)).all()
    assert any(e.action == "admission.mark_stale" for e in events)


def test_full_score_admission_enters_pending_bid_approval_and_decides(session):
    """满分生成 → 审批队列（pending_bid_approval）→ 审批通过（approved_for_bidding）全链。"""
    from runtime.core import rbac
    from runtime.db import api_service

    _project(session)
    _rule_set(session)
    _requirement(session)
    _qualification(session)
    _chunk(session)
    _execute_match_run(session, "ND-2025", evaluate_fn=_fake_evaluate, retrieve_fn=_fake_retrieve)

    ar = _admissions(session)[0]
    assert ar.internal_admission_eligible is True
    project = session.get(Project, "ND-2025")
    assert project.admission_status == "qualified_full_score"

    data = api_service.create_approval(
        session, project_id="ND-2025", role=rbac.BUSINESS_HEAD, actor="经营负责人",
        admission_result_ref=f"admission:ND-2025:{ar.result_id}",
    )
    assert data["state"] == "pending_bid_approval"
    session.expire_all()
    assert session.get(Project, "ND-2025").admission_status == "pending_bid_approval"

    decided = api_service.decide_approval(
        session, project_id="ND-2025", role=rbac.BUSINESS_HEAD, actor="经营负责人",
        decision="approved", comment="资料齐备，同意投标",
    )
    assert decided["outcome"] == "approved_for_bidding"
    assert session.get(Project, "ND-2025").admission_status == "approved_for_bidding"


# ---------- R024 生成器白盒：结论结构/分流（engine_result 直接注入） ----------

def _mk_run(session, run_id="MR-G1") -> MatchRun:
    session.add(MatchRun(
        run_id=run_id, project_id="ND-2025", rule_set_id="RS-1", as_of="2025-10-30",
        mode="gate", coverage={"executed": 1, "declared": 1, "complete": True},
        status="completed", retrieval_run_id="rr-1", index_version="iv-1",
        candidate_chunk_ids=["CH-1"],
        structured_verification={"rag_degraded": None, "checked": [], "removed_material_ids": []},
        evidence_snapshot_hash="e" * 64,
    ))
    session.flush()
    return session.get(MatchRun, run_id)


def _req_dict(requirement_id="NQ-H-001", *, req_type="hard_requirement", max_score=None,
              failure_effect="not_qualified", rule=None):
    return {
        "requirement_id": requirement_id, "req_type": req_type, "category": "资质",
        "clause_ref": "第三章四(1)", "assertion": f"{requirement_id} 的条款断言",
        "rule": rule or {"type": "qualification", "qualification_type": "建筑工程施工总承包",
                         "level": "二级"},
        "evidence_required": ["qualification_record"], "failure_effect": failure_effect,
        "as_of": "2025-10-30", "max_score": max_score, "score_nature": None,
        "score_formula": None, "action_status": None,
    }


def _hard_entry(requirement_id="NQ-H-001", result="satisfied", reason="核验通过"):
    return {
        "requirement_id": requirement_id, "req_type": "hard_requirement",
        "clause_ref": "第三章四(1)", "match_result": result, "match_reason": reason,
        "score": None,
    }


def _engine_result(matrix, *, blocked=None, pending=None, review=None):
    blocked = [e for e in matrix if e["match_result"] == "not_satisfied"] if blocked is None else blocked
    pending = [e for e in matrix if e["match_result"] == "unverifiable"] if pending is None else pending
    review = [e for e in matrix if e["match_result"] == "manual_review"] if review is None else review
    hard = [e for e in matrix if e["req_type"] == "hard_requirement"]
    hard_ids = {e["requirement_id"] for e in hard}
    qualification = (
        "failed" if any(e["requirement_id"] in hard_ids for e in blocked)
        else ("pending" if any(e["requirement_id"] in hard_ids for e in pending) else "passed")
    )
    return {
        "matrix": matrix,
        "coverage": {"executed": len(matrix), "declared": len(matrix), "complete": True},
        "blocked": blocked, "pending": pending, "review": review,
        "qualification_result": qualification, "scoring_result": "full",
        "operational_readiness": "ready",
        "internal_admission_eligible": not blocked and not pending and not review,
    }


def test_generate_admission_whitelist_full_score(session):
    from runtime.db.admission_service import generate_admission_result

    _project(session)
    run = _mk_run(session)
    req = _req_dict()
    ar = generate_admission_result(
        session, run=run, engine_result=_engine_result([_hard_entry()]),
        requirements=[req], evidence={},
    )
    session.commit()
    assert ar.result_id.startswith("AR-")
    assert ar.internal_admission_eligible is True
    assert ar.qualification_result == {
        "status": "passed", "satisfied": 1, "total": 1,
        "items": [_queue_item_like(_hard_entry(), req)],
    }
    assert ar.internal_admission_result["status"] == "qualified_full_score"
    assert ar.manager_matches == []
    assert len(ar.explanation) >= 6


def _queue_item_like(entry, req):
    # F020 §2.2.5 v1.7（09-优化方案 §3.4.3）：队列 DTO 扩展字段同步断言
    required = list(req.get("evidence_required") or [])
    return {
        "req": entry["requirement_id"],
        "requirement_id": entry["requirement_id"],
        "clause": entry["clause_ref"],
        "clause_ref": entry["clause_ref"],
        "text": req["assertion"],
        "match_result": entry["match_result"],
        "req_type": entry["req_type"],
        "failure_effect": req["failure_effect"],
        "missing_field": None,
        "reason": entry.get("match_reason"),
        "purpose": None,
        "recommended_material_types": list(required) if required else None,
        "owner_role": "bid_specialist:upload → data_admin:verify",
        "due_at": None,
    }


def test_generate_admission_review_only_stays_matching(session):
    """manual_review-only 结果：不谎报为缺失/失败，项目态保持 matching 待人工复核。"""
    from runtime.db.admission_service import generate_admission_result

    _project(session)
    run = _mk_run(session)
    req = _req_dict()
    entry = _hard_entry(result="manual_review", reason="规则类型尚未实现，需人工复核")
    ar = generate_admission_result(
        session, run=run,
        engine_result=_engine_result([entry], blocked=[], pending=[], review=[entry]),
        requirements=[req], evidence={},
    )
    session.commit()
    assert ar.internal_admission_eligible is False
    assert ar.internal_admission_result["status"] == "manual_review"
    assert ar.review_items and ar.review_items[0]["req"] == "NQ-H-001"
    project = session.get(Project, "ND-2025")
    assert project.admission_status == "matching"  # 不谎报为缺失或失败


def test_generate_admission_scored_aggregation(session):
    """计分项聚合：total/max/gap/internal_full_score_ready（F008 §4.6 可计算口径）。"""
    from runtime.db.admission_service import generate_admission_result

    _project(session)
    run = _mk_run(session)
    scored_req = _req_dict(
        "NQ-S-001", req_type="scored_requirement", max_score=5.0,
        rule={"type": "similar_performance", "subject": "bidder", "count": 1},
    )
    scored_entry = {
        "requirement_id": "NQ-S-001", "req_type": "scored_requirement",
        "clause_ref": "第三章四(1)", "match_result": "satisfied",
        "match_reason": "业绩满足", "score": 3.0,
    }
    ar = generate_admission_result(
        session, run=run,
        engine_result=_engine_result([scored_entry]),
        requirements=[scored_req], evidence={},
    )
    session.commit()
    assert ar.total_score == 3.0
    assert ar.max_total_score == 5.0
    assert len(ar.score_gap_items) == 1
    assert ar.score_gap_items[0]["gap"] == 2.0
    assert ar.scoring_result["objective_score"] == 3.0
    assert ar.scoring_result["objective_max"] == 5.0
    assert ar.scoring_result["internal_full_score_ready"] is False


def test_generate_admission_diagnostic_mode_rejected(session):
    from runtime.core.errors import ApiError
    from runtime.db.admission_service import generate_admission_result

    _project(session)
    run = _mk_run(session)
    run.mode = "diagnostic"
    session.flush()
    with pytest.raises(ValueError):
        generate_admission_result(
            session, run=run, engine_result=_engine_result([_hard_entry()]),
            requirements=[_req_dict()], evidence={},
        )

# ---------- R023-4：结果版本对比（match-runs/compare） ----------

def _mk_run_v2(session, run_id="MR-2", created_ts=None, snapshot_hash="f" * 64) -> None:
    from datetime import timedelta

    ts = created_ts or (_dt.datetime.now(_dt.timezone.utc) - timedelta(minutes=1))
    session.add(MatchRun(
        run_id=run_id, project_id="ND-2025", rule_set_id="RS-1", as_of="2025-10-30",
        mode="gate", coverage={"executed": 20, "declared": 20, "complete": True},
        status="completed", retrieval_run_id="rr-2", index_version="iv-2",
        candidate_chunk_ids=["CH-1"], structured_verification={},
        evidence_snapshot_hash=snapshot_hash,
        created_at=ts,
    ))
    session.commit()


def _mk_item(session, run_id, requirement_id, result, score=None) -> None:
    from runtime.db.models import MatchItem

    session.add(MatchItem(
        item_id=f"MI-{run_id}-{requirement_id}", run_id=run_id,
        requirement_id=requirement_id, match_result=result, score=score,
        match_reason={"text": f"{requirement_id}: {result}"},
        evaluated_at=_dt.datetime.now(_dt.timezone.utc),
    ))
    session.commit()


def test_match_runs_compare_diff_and_summary(session):
    """R023-4：补录后重算 → compare 显示变化（unverifiable→satisfied）与摘要。"""
    from runtime.db import api_service

    _project(session)
    # base run：无企业资料 → NQ-H-001 unverifiable
    _mk_run_v2(session, run_id="MR-BASE")
    _mk_item(session, "MR-BASE", "NQ-H-001", "unverifiable")
    _mk_item(session, "MR-BASE", "NQ-A-001", "satisfied")
    # target run：补录后 → NQ-H-001 satisfied（快照 hash 变化）
    _mk_run_v2(session, run_id="MR-TGT", snapshot_hash="g" * 64)
    _mk_item(session, "MR-TGT", "NQ-H-001", "satisfied")
    _mk_item(session, "MR-TGT", "NQ-A-001", "satisfied")

    data = api_service.match_runs_compare(session, "ND-2025", base_run_id="MR-BASE",
                                          target_run_id="MR-TGT")
    assert data["base"]["run_id"] == "MR-BASE"
    assert data["target"]["run_id"] == "MR-TGT"
    assert data["summary"]["base_satisfied"] == 1
    assert data["summary"]["target_satisfied"] == 2
    assert data["summary"]["target_unverifiable"] == 0
    assert data["summary"]["changed_count"] == 1
    assert data["summary"]["input_snapshot_identical"] is False
    changes = data["changes"]
    assert changes[0]["requirement_id"] == "NQ-H-001"
    assert changes[0]["from"] == "unverifiable" and changes[0]["to"] == "satisfied"
    # matrix 逐条对照
    by_req = {r["requirement_id"]: r for r in data["matrix"]}
    assert by_req["NQ-H-001"]["base"] == "unverifiable"
    assert by_req["NQ-H-001"]["target"] == "satisfied"
    assert by_req["NQ-H-001"]["changed"] is True
    assert by_req["NQ-A-001"]["changed"] is False


def test_match_runs_compare_identical_snapshot_flags(session):
    """同快照重跑（无实质变化）→ input_snapshot_identical=True。"""
    from runtime.db import api_service

    _project(session)
    _mk_run_v2(session, run_id="MR-1", snapshot_hash="same")
    _mk_item(session, "MR-1", "NQ-H-001", "satisfied")
    _mk_run_v2(session, run_id="MR-2", snapshot_hash="same")
    _mk_item(session, "MR-2", "NQ-H-001", "satisfied")
    data = api_service.match_runs_compare(session, "ND-2025",
                                          base_run_id=None, target_run_id=None)
    assert data["summary"]["input_snapshot_identical"] is True
    assert data["summary"]["changed_count"] == 0


def test_match_runs_compare_no_runs_404(session):
    from runtime.core.errors import ApiError
    from runtime.db import api_service

    _project(session)
    with pytest.raises(ApiError):
        api_service.match_runs_compare(session, "ND-2025", base_run_id=None,
                                       target_run_id=None)

def test_default_match_retrieval_injects_query_embedding(session, monkeypatch):
    """修复：自动匹配不得因遗漏 embed_query_fn 而退化为 keyword-only。"""
    _project(session)
    _rule_set(session)
    _requirement(session)
    _qualification(session)
    _chunk(session)
    captured = {}

    def fake_hybrid(_session, _request, _vis, *, role="", embed_query_fn=None, **_kwargs):
        captured["embed_query_fn"] = embed_query_fn
        return _fake_retrieve(_session, _request, _vis, role=role)

    def fake_embed(text):
        return [float(len(text))]

    monkeypatch.setattr("runtime.rag.retriever.hybrid_search", fake_hybrid)
    monkeypatch.setattr("runtime.rag.indexer._embed", fake_embed)
    _execute_match_run(session, "ND-2025", evaluate_fn=_fake_evaluate)

    assert captured["embed_query_fn"] is fake_embed
