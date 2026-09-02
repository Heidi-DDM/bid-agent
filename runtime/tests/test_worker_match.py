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
    session.add(Requirement(
        requirement_id=requirement_id, rule_set_id="RS-1", req_type="hard_requirement",
        category="资质", clause_ref="第三章四(1)", assertion="具备建筑工程施工总承包二级及以上资质",
        rule=rule, evidence_required=["qualification_record"],
        missing_action="blocked_missing_data", failure_effect="not_qualified",
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