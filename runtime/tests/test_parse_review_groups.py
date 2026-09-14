# F021 §2.1 v1.3 / 09-优化方案 §3.3-3.4：解析结果聚合与复核语义测试（sqlite 内存库）
# 覆盖：
# - decide_candidate：rejected 必带结构化原因枚举（扫描不清/条款冲突/未识别/需业务解释）；
#   revised 必带 revised_payload；approved 不带 payload
# - grouped_requirements candidates 视图：六组确定性映射 + main_card_field → 项目基本信息；
#   不可判定锚点归“其他”
# - grouped_requirements confirmed 视图：RuleSet 写入后 source=confirmed（Requirement+FieldTrace）
from __future__ import annotations

import re as _re

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from runtime.db import parse_service
from runtime.db.models import (
    Base,
    FieldTrace,
    Material,
    ParseCandidate,
    Project,
    Requirement,
    RuleSet,
)
from runtime.db.parse_service import (
    CANDIDATE_KIND_FIELD,
    CANDIDATE_KIND_RULE,
    GROUP_ACTION,
    GROUP_BASIC,
    GROUP_EVIDENCE,
    GROUP_OTHER,
    GROUP_PERSONNEL,
    GROUP_QUALIFICATION,
    GROUP_RESOURCES,
    grouped_requirements,
)

# sqlite 不支持多 schema：剥离前缀（与 test_worker_match.py 同法）
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


def _material(session: Session, material_id: str = "MAT-TEST", version: int = 1) -> Material:
    m = Material(
        material_id=material_id,
        version=version,
        material_type="tender_document",
        source_type="uploaded",
        owner_type="public",
        classification="public",
        permission_scope="public_read",
        content_hash="9c42" + "0" * 60,
        parse_status="manual_review",
        status="active",
        data_owner="x",
        project_id="PJ-TEST",
    )
    session.add(m)
    return m


def _rule_candidate(session: Session, cand_id: str, *, anchor_key: str, req_type: str,
                    category: str, status: str = "pending") -> ParseCandidate:
    c = ParseCandidate(
        candidate_id=cand_id,
        material_id="MAT-TEST",
        version=1,
        project_id="PJ-TEST",
        kind=CANDIDATE_KIND_RULE,
        payload={
            "requirement_id": cand_id,
            "req_type": req_type,
            "category": category,
            "clause_ref": f"招标公告 §3.{cand_id[-1]}",
            "assertion": f"原文摘录-{cand_id}",
            "page_no": 5,
            "rule": {"type": "qualification", "anchor_key": anchor_key},
            "confidence": "high",
            "missing_marker": False,
        },
        status=status,
    )
    session.add(c)
    return c


def _field_candidate(session: Session, cand_id: str, *, field_key: str) -> ParseCandidate:
    c = ParseCandidate(
        candidate_id=cand_id,
        material_id="MAT-TEST",
        version=1,
        project_id="PJ-TEST",
        kind=CANDIDATE_KIND_FIELD,
        payload={
            "field_key": field_key,
            "value": "某某建设项目施工",
            "clause": "招标公告",
            "page_no": 4,
            "assertion": "原文-项目名称",
            "confidence": "high",
            "missing_marker": False,
        },
        status="pending",
    )
    session.add(c)
    return c


# ── 复核语义（F021 §2.1 v1.3） ──────────────────────────────────

def test_rejected_requires_structured_reason(session):
    _rule_candidate(session, "RC-1", anchor_key="qualification_grade",
                    req_type="hard_requirement", category="资质")
    session.commit()
    with pytest.raises(parse_service.ParseServiceError, match="结构化原因"):
        parse_service.decide_candidate(
            session, candidate_id="RC-1", material_id="MAT-TEST", version=1,
            decision="rejected", reviewer="投标专员", review_note="字迹不清",
        )
    # 枚举值 + “原因：补充”两种合法格式
    r1 = parse_service.decide_candidate(
        session, candidate_id="RC-1", material_id="MAT-TEST", version=1,
        decision="rejected", reviewer="投标专员", review_note="扫描不清",
    )
    assert r1["status"] == "rejected"


def test_revised_requires_payload(session):
    _rule_candidate(session, "RC-2", anchor_key="qualification_grade",
                    req_type="hard_requirement", category="资质")
    session.commit()
    with pytest.raises(parse_service.ParseServiceError, match="revised_payload"):
        parse_service.decide_candidate(
            session, candidate_id="RC-2", material_id="MAT-TEST", version=1,
            decision="revised", reviewer="投标专员",
        )
    r = parse_service.decide_candidate(
        session, candidate_id="RC-2", material_id="MAT-TEST", version=1,
        decision="revised", reviewer="投标专员",
        revised_payload={"assertion": "修正后的原文", "value": "二级及以上"},
    )
    assert r["status"] == "revised"
    assert r["revised_payload"]["value"] == "二级及以上"


# ── 聚合：candidates 视图分组 ──────────────────────────────────

def test_grouped_requirements_candidates_view(session):
    session.add(Project(project_id="PJ-TEST", project_name="测试项目",
                        tender_document_ref="MAT-TEST"))
    _material(session)
    _rule_candidate(session, "RC-Q", anchor_key="qualification_grade",
                    req_type="hard_requirement", category="资质")
    _rule_candidate(session, "RC-P", anchor_key="pm_b_cert",
                    req_type="hard_requirement", category="人员")
    _rule_candidate(session, "RC-F", anchor_key="financial_audit",
                    req_type="hard_requirement", category="财务")
    _rule_candidate(session, "RC-C", anchor_key="consortium",
                    req_type="hard_requirement", category="联合体")
    _rule_candidate(session, "RC-A", anchor_key="action_deadline_bid",
                    req_type="action_requirement", category="递交")
    _field_candidate(session, "MAT-TEST:project_name", field_key="project_name")
    session.commit()

    data = grouped_requirements(session, project_id="PJ-TEST")
    assert data["source"] == "candidates"
    by_group = {g["group"]: g["items"] for g in data["groups"]}
    # 主卡字段 → 项目基本信息
    assert [i["title"] for i in by_group[GROUP_BASIC]] == ["项目名称"]
    assert by_group[GROUP_BASIC][0]["value"] == "某某建设项目施工"
    # 规则锚点映射：资质/人员/财务/联合体/动作 分属正确组
    assert {i["id"] for i in by_group[GROUP_QUALIFICATION]} == {"RC-Q"}
    assert {i["id"] for i in by_group[GROUP_PERSONNEL]} == {"RC-P"}
    assert {i["id"] for i in by_group[GROUP_EVIDENCE]} == {"RC-F"}
    assert {i["id"] for i in by_group[GROUP_RESOURCES]} == {"RC-C"}
    assert {i["id"] for i in by_group[GROUP_ACTION]} == {"RC-A"}
    # 内部编码只出现在 audit 折叠，不作为主展示字段
    for g in data["groups"]:
        for item in g["items"]:
            assert "material_id" in item["audit"]
            assert "content_hash" in item["audit"]
    # 可读标题来自锚点登记表
    assert by_group[GROUP_QUALIFICATION][0]["title"] == "施工总承包资质等级要求"


def test_grouped_requirements_unknown_anchor_goes_other(session):
    session.add(Project(project_id="PJ-TEST", project_name="测试项目",
                        tender_document_ref="MAT-TEST"))
    _material(session)
    _rule_candidate(session, "RC-X", anchor_key="future_anchor_未登记",
                    req_type="hard_requirement", category="未来类别")
    session.commit()
    data = grouped_requirements(session, project_id="PJ-TEST")
    by_group = {g["group"]: g["items"] for g in data["groups"]}
    assert {i["id"] for i in by_group[GROUP_OTHER]} == {"RC-X"}
    assert by_group[GROUP_OTHER][0]["title"].endswith("（待复核）")


def test_grouped_requirements_confirmed_view(session):
    session.add(Project(project_id="PJ-TEST", project_name="测试项目",
                        tender_document_ref="MAT-TEST"))
    m = _material(session)
    m.parse_status = "parsed"
    session.add(RuleSet(rule_set_id="RS-MAT-TEST-v1", project_id="PJ-TEST",
                        version="v1", created_by="投标专员",
                        snapshot={"source": "material=MAT-TEST:v1"}))
    session.add(Requirement(
        requirement_id="RC-Q", rule_set_id="RS-MAT-TEST-v1",
        req_type="hard_requirement", category="资质",
        clause_ref="招标公告 §3.2", assertion="须具备二级及以上",
        rule={"type": "qualification", "anchor_key": "qualification_grade"},
        evidence_required=[], as_of="2026-09-04",
    ))
    session.add(FieldTrace(
        trace_id="FT-1", object_id="MAT-TEST:v1", field_key="project_name",
        clause="招标公告", assertion="某某建设项目施工", confidence="high",
        source_hash="9c42" + "0" * 60,
    ))
    session.commit()

    data = grouped_requirements(session, project_id="PJ-TEST")
    assert data["source"] == "confirmed"
    by_group = {g["group"]: g["items"] for g in data["groups"]}
    assert len(by_group[GROUP_QUALIFICATION]) == 1
    assert by_group[GROUP_QUALIFICATION][0]["review_status"] == "confirmed"
    assert by_group[GROUP_QUALIFICATION][0]["assertion"] == "须具备二级及以上"
    # FieldTrace 主卡字段仍归“项目基本信息”（确认后溯源视图）
    assert [i["title"] for i in by_group[GROUP_BASIC]] == ["项目名称"]
    assert by_group[GROUP_BASIC][0]["value"] == "某某建设项目施工"