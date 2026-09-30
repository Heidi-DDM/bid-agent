# ADR-007（2026-09-25）解析自动确认与例外驱动复核回归：
# - 系统自动通过仅限「pending + 已定位 + high/medium + 确定性锚点」；
#   模型定位/发现（located_by=llm）与 missing 永不自动决策（红线）
# - 例外式规则集：pending 例外随快照留档、不写 Requirement
# - 例外清零 → 增量重确认（快照链 -r2：既有要求复制 + 新决策写入 + NA 并入；幂等）
# - grouped_requirements 透出 pending_exceptions；pending_exception_count 供满分门禁
from __future__ import annotations

import re as _re

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from runtime.db import parse_service
from runtime.db.models import Base, Material, ParseCandidate, Project, Requirement, RuleSet
from runtime.db.parse_service import (
    CANDIDATE_KIND_FIELD,
    CANDIDATE_KIND_RULE,
    CANDIDATE_KIND_TERM,
    STATUS_APPROVED,
    STATUS_NOT_APPLICABLE,
    STATUS_PENDING,
)

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
        s.add(Project(project_id="PJ-A7", project_name="ADR-007 测试项目"))
        s.add(Material(
            material_id="MAT-A7", version=1, material_type="tender_document",
            source_type="uploaded", owner_type="public", classification="public",
            permission_scope="public_read", content_hash="a7" + "0" * 62,
            parse_status="manual_review", status="active", data_owner="x", project_id="PJ-A7",
        ))
        s.commit()
        yield s


def _rule_cand(cid: str, *, confidence="high", llm=False, missing=False, category="资质",
               assertion="具备市政公用工程施工总承包壹级资质") -> dict:
    rule = {"type": "qualification", "anchor_key": "qualification_grade"}
    if llm:
        rule["located_by"] = "llm"
    return {"requirement_id": cid, "req_type": "hard_requirement", "category": category,
            "clause_ref": "3.1", "assertion": parse_service.MISSING if missing else assertion,
            "page_no": None if missing else 3, "rule": rule, "evidence_required": [],
            "confidence": confidence, "missing_marker": missing}


def _seed(session) -> None:
    parse_service.store_candidates(session, project_id="PJ-A7", material_id="MAT-A7", version=1,
                                   kind=CANDIDATE_KIND_RULE, candidates=[
        _rule_cand("MAT-A7-H-001-draft"),                                  # 锚点高置信 → 自动通过
        _rule_cand("MAT-A7-H-002-draft", llm=True, confidence="low"),      # 模型定位 → 例外
        _rule_cand("MAT-A7-H-003-draft", missing=True, confidence="low"),  # 缺失 → 例外
    ])
    parse_service.store_candidates(session, project_id="PJ-A7", material_id="MAT-A7", version=1,
                                   kind=CANDIDATE_KIND_FIELD, candidates=[
        {"field_key": "region", "value": "中兴大街等13条街道", "clause": "招标公告",
         "page_no": 7, "assertion": "建设地点：中兴大街…", "confidence": "high",
         "missing_marker": False},
        {"field_key": "tenderee", "value": parse_service.MISSING, "clause": "招标公告",
         "page_no": None, "assertion": "", "confidence": "low", "missing_marker": True},
    ])
    parse_service.store_candidates(session, project_id="PJ-A7", material_id="MAT-A7", version=1,
                                   kind=CANDIDATE_KIND_TERM, candidates=[
        {"field_key": "retention_money", "value": "3%", "clause": "x", "page_no": 11,
         "assertion": "质量保证金3%", "confidence": "medium", "missing_marker": False},
    ])


def test_auto_confirm_only_deterministic_located_high_confidence(session):
    _seed(session)
    out = parse_service.auto_confirm_anchor_candidates(
        session, project_id="PJ-A7", material_id="MAT-A7", version=1)
    # 锚点高置信规则 + region 主卡 + retention 条款 = 3 条自动通过
    assert out["approved"] == 3 and out["remaining_pending"] == 3
    by_id = {r.candidate_id: r for r in session.scalars(select(ParseCandidate)).all()}
    assert by_id["MAT-A7-H-001-draft"].status == STATUS_APPROVED
    assert by_id["MAT-A7-H-001-draft"].reviewer == parse_service.AUTO_REVIEWER
    assert "ADR-007" in (by_id["MAT-A7-H-001-draft"].review_note or "")
    # 红线：模型定位与缺失项保持 pending
    assert by_id["MAT-A7-H-002-draft"].status == STATUS_PENDING
    assert by_id["MAT-A7-H-003-draft"].status == STATUS_PENDING
    assert by_id["MAT-A7:tenderee"].status == STATUS_PENDING
    # 二次调用幂等（无可自动通过项）
    again = parse_service.auto_confirm_anchor_candidates(
        session, project_id="PJ-A7", material_id="MAT-A7", version=1)
    assert again["approved"] == 0


def test_exception_ruleset_and_reconfirm_chain(session):
    _seed(session)
    parse_service.auto_confirm_anchor_candidates(
        session, project_id="PJ-A7", material_id="MAT-A7", version=1)
    confirmed = parse_service.confirm_rules_from_approved(
        session, project_id="PJ-A7", material_id="MAT-A7", version=1,
        as_of="2026-08-13", created_by="system.parse_auto", allow_pending_exceptions=True)
    rs = session.get(RuleSet, confirmed["rule_set_id"])
    assert rs is not None
    assert confirmed["created_requirements"] == 1                     # 仅锚点项
    exc_ids = {e["candidate_id"] for e in rs.snapshot["pending_exceptions"]}
    # 快照例外 = 规则类 pending（字段类缺失由 pending_exception_count/风险页口径覆盖）
    assert exc_ids == {"MAT-A7-H-002-draft", "MAT-A7-H-003-draft"}
    # 例外未清零 → 不得满分口径的计数 > 0
    assert parse_service.pending_exception_count(session, project_id="PJ-A7") == 3

    # 人工处置例外：模型项通过、缺失项确认本文件无此条款、tenderee 修正
    parse_service.decide_candidate(session, candidate_id="MAT-A7-H-002-draft",
                                   material_id="MAT-A7", version=1, decision="approved",
                                   reviewer="toubiao")
    parse_service.decide_candidate(session, candidate_id="MAT-A7-H-003-draft",
                                   material_id="MAT-A7", version=1,
                                   decision="not_applicable", reviewer="toubiao",
                                   review_note="全文检索无审计报告要求")
    parse_service.decide_candidate(session, candidate_id="MAT-A7:tenderee",
                                   material_id="MAT-A7", version=1, decision="rejected",
                                   reviewer="toubiao", review_note="未识别：扫描页无法辨认")

    r1 = parse_service.reconfirm_rules_after_exceptions(
        session, project_id="PJ-A7", material_id="MAT-A7", version=1, actor="toubiao")
    assert r1 is not None and r1["rule_set_id"] == "RS-MAT-A7-v1-r2"
    assert r1["copied"] == 1 and r1["created"] == 1                  # 旧要求复制 + 模型项新写入
    reqs = session.scalars(select(Requirement)
                           .where(Requirement.rule_set_id == "RS-MAT-A7-v1-r2")).all()
    assert {r.requirement_id for r in reqs} == {
        "MAT-A7-H-001-draft-r2", "MAT-A7-H-002-draft-r2"}
    rs2 = session.get(RuleSet, "RS-MAT-A7-v1-r2")
    assert rs2.snapshot["reconfirm_of"] == "RS-MAT-A7-v1"
    assert rs2.snapshot["as_of"] == "2026-08-13"                     # as_of 沿用基础快照
    assert any(e["candidate_id"] == "MAT-A7-H-003-draft"
               for e in rs2.snapshot["not_applicable"])               # NA 并入
    assert parse_service.pending_exception_count(session, project_id="PJ-A7") == 0
    # 幂等：例外已清零、快照链已生成 → 再次调用无增量
    assert parse_service.reconfirm_rules_after_exceptions(
        session, project_id="PJ-A7", material_id="MAT-A7", version=1, actor="toubiao") is None


def test_grouped_view_exposes_pending_exceptions(session):
    _seed(session)
    parse_service.auto_confirm_anchor_candidates(
        session, project_id="PJ-A7", material_id="MAT-A7", version=1)
    parse_service.confirm_rules_from_approved(
        session, project_id="PJ-A7", material_id="MAT-A7", version=1,
        as_of="2026-08-13", created_by="system.parse_auto", allow_pending_exceptions=True)
    data = parse_service.grouped_requirements(session, project_id="PJ-A7")
    assert data["source"] == "confirmed"                              # 规则集已存在
    pend = data["pending_exceptions"]
    assert {p["id"] for p in pend} == {"MAT-A7-H-002-draft", "MAT-A7-H-003-draft", "MAT-A7:tenderee"}
    by_id = {p["id"]: p for p in pend}
    assert by_id["MAT-A7-H-003-draft"]["missing"] is True
    assert by_id["MAT-A7-H-003-draft"]["locate_hints"]                # 缺失项带检索关键词
    assert by_id["MAT-A7-H-002-draft"]["assertion"]                   # 模型项带原文摘录


def test_manual_confirm_still_blocks_pending_without_flag(session):
    """旧人工路径语义不变：不带 allow_pending_exceptions 时 pending 仍阻断（F021 §2.7）。"""
    _seed(session)
    with pytest.raises(parse_service.ParseServiceError):
        parse_service.confirm_rules_from_approved(
            session, project_id="PJ-A7", material_id="MAT-A7", version=1,
            as_of="2026-08-13", created_by="toubiao")
