# F021 §2.1-2.3 v1.5（2026-09-14 用户实测五问整改）回归：
# - 复核决策：approved 对 missing 拒绝；not_applicable 仅 missing + 必带说明；revised 逐字校验并合并 payload；
#   旧版遗留「已通过的 missing」可重新决策、阻断确认、计入 needs_redecision
# - 逐字校验纯函数：精确页 / 填错一页回报真实页 / 跨页（剥页脚页码）/ 伪造拒绝
# - as_of 建议链：已确认主卡 deadline_bid → 日期；无则 None
# - 聚合视图：missing 规则 clause_ref=None + locate_hints；source_link；as_of_suggestion；历史「X（X）」条款折叠
# - 确认写入：not_applicable 进 snapshot 不产 Requirement；页码随规则固化
# - 抽取器 v1.5：业绩「自 X 年 X 月 X 日以来」分支（必查）、EPC 设计/施工负责人、勾选框 □ 抑制、条款不重复拼接、
#   项目名称/类型新版式
from __future__ import annotations

import re as _re

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from runtime.db import parse_service
from runtime.db.models import Base, Material, ParseCandidate, Project, Requirement, RuleSet
from runtime.db.parse_service import (
    CANDIDATE_KIND_FIELD,
    CANDIDATE_KIND_RULE,
    GROUP_BASIC,
    GROUP_EVIDENCE,
    GROUP_PERSONNEL,
    _verbatim_in_pages,
    grouped_requirements,
)
from runtime.parsing.extractor import (
    ANCHORS,
    LOCATE_HINTS,
    OPTIONAL_ANCHORS,
    extract_main_card,
    extract_rule_candidates,
)
from runtime.rag.chunker import ParsedPage

_SCHEMA_PREFIX = _re.compile(
    r"\b(?:public_data|enterprise_data|admission_data|audit_data|knowledge_data)\."
)
OK_CHECK = lambda *a, **k: {"ok": True, "check": "verified", "page_no": k.get("page_no"), "reason": None}  # noqa: E731


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _strip_schema(conn, cursor, statement, parameters, context, executemany):
        return _SCHEMA_PREFIX.sub("", statement), parameters

    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Project(project_id="PJ-T", project_name="测试项目"))
        s.add(Material(
            material_id="MAT-T", version=1, material_type="tender_document", source_type="uploaded",
            owner_type="public", classification="public", permission_scope="public_read",
            content_hash="9c42" + "0" * 60, parse_status="manual_review", status="active",
            data_owner="x", project_id="PJ-T",
        ))
        s.commit()
        yield s


def _rule(session, cid, *, anchor, missing=False, status="pending", req_type="hard_requirement",
          category="资质", page_no=5, clause="招标公告 §3.2"):
    payload = {
        "requirement_id": cid, "req_type": req_type, "category": category,
        "clause_ref": clause, "assertion": "__待补__" if missing else f"原文-{cid}",
        "page_no": None if missing else page_no,
        "rule": {"type": "missing" if missing else "qualification", "anchor_key": anchor},
        "confidence": "low" if missing else "high", "missing_marker": missing,
        "note": "锚点未命中" if missing else None,
    }
    row = ParseCandidate(candidate_id=cid, material_id="MAT-T", version=1, project_id="PJ-T",
                         kind=CANDIDATE_KIND_RULE, payload=payload, status=status)
    session.add(row)
    session.commit()
    return row


def _field(session, key, *, value, status="pending", page_no=5, missing=False):
    payload = {"field_key": key, "value": "__待补__" if missing else value, "clause": "招标公告",
               "page_no": None if missing else page_no, "assertion": value, "confidence": "high",
               "missing_marker": missing}
    row = ParseCandidate(candidate_id=f"MAT-T:{key}", material_id="MAT-T", version=1, project_id="PJ-T",
                         kind=CANDIDATE_KIND_FIELD, payload=payload, status=status)
    session.add(row)
    session.commit()
    return row


def _decide(session, cid, decision, **kw):
    return parse_service.decide_candidate(
        session, candidate_id=cid, material_id="MAT-T", version=1, decision=decision,
        reviewer="投标专员", **kw)


# ── 复核决策语义 ─────────────────────────────────────────────────────

def test_approve_missing_candidate_is_rejected(session):
    _rule(session, "R-1", anchor="financial_audit", missing=True, category="财务")
    with pytest.raises(parse_service.ParseServiceError, match="不能「通过」"):
        _decide(session, "R-1", "approved")
    # 已定位候选可通过
    _rule(session, "R-2", anchor="qualification_grade")
    assert _decide(session, "R-2", "approved")["status"] == "approved"


def test_not_applicable_only_for_missing_and_needs_note(session):
    _rule(session, "R-1", anchor="financial_audit", missing=True, category="财务")
    _rule(session, "R-2", anchor="qualification_grade")
    with pytest.raises(parse_service.ParseServiceError, match="人工核查说明"):
        _decide(session, "R-1", "not_applicable")
    with pytest.raises(parse_service.ParseServiceError, match="仅适用于未定位"):
        _decide(session, "R-2", "not_applicable", review_note="x")
    r = _decide(session, "R-1", "not_applicable", review_note="全文检索“审计报告”无资格条款")
    assert r["status"] == "not_applicable"
    # 终态：不可再决策
    with pytest.raises(parse_service.CandidateAlreadyDecided):
        _decide(session, "R-1", "rejected", review_note="扫描不清")


def test_revised_missing_requires_page_and_merges_payload(session):
    _rule(session, "R-1", anchor="similar_performance_hard", missing=True, category="业绩")
    with pytest.raises(parse_service.ParseServiceError, match="page_no"):
        _decide(session, "R-1", "revised", revised_payload={"assertion": "自2020年9月1日以来完成过一项业绩"})
    with pytest.raises(parse_service.ParseServiceError, match="逐字摘录"):
        _decide(session, "R-1", "revised", revised_payload={"assertion": "", "page_no": 4})
    # 校验器回报真实页 3（用户填 4）→ 落库 3
    checker = lambda *a, **k: {"ok": True, "check": "verified", "page_no": 3, "reason": None}  # noqa: E731
    r = _decide(session, "R-1", "revised", review_note="§3.5",
                revised_payload={"assertion": "自2020年9月1日以来完成过一项业绩", "page_no": 4, "clause_ref": "3.5"},
                verbatim_checker=checker)
    rp = r["revised_payload"]
    assert rp["missing_marker"] is False and rp["page_no"] == 3 and rp["clause_ref"] == "3.5"
    assert rp["confidence"] == "low" and rp["rule"]["located_by"] == "human"
    assert rp["rule"]["type"] == "similar_performance"          # missing → 锚点真实规则类型
    assert rp["req_type"] == "hard_requirement" and rp["category"] == "业绩"
    assert rp["verbatim_check"] == "verified" and rp["note"] is None


def test_revised_verbatim_failure_rejected(session):
    _rule(session, "R-1", anchor="qualification_grade")
    bad = lambda *a, **k: {"ok": False, "check": "failed", "page_no": None, "reason": "摘录不是第 5 页原文的逐字子串"}  # noqa: E731
    with pytest.raises(parse_service.ParseServiceError, match="逐字子串"):
        _decide(session, "R-1", "revised", review_note="改",
                revised_payload={"assertion": "我改写的句子"}, verbatim_checker=bad)


def test_field_revised_merges_value_only(session):
    _field(session, "region", value="", missing=True)
    with pytest.raises(parse_service.ParseServiceError, match="value"):
        _decide(session, "MAT-T:region", "revised", revised_payload={"assertion": "x"})
    r = _decide(session, "MAT-T:region", "revised", revised_payload={"value": "河北省保定市"})
    assert r["revised_payload"]["value"] == "河北省保定市"
    assert r["revised_payload"]["field_key"] == "region" and r["revised_payload"]["missing_marker"] is False


def test_legacy_approved_missing_blocks_confirm_and_can_be_redecided(session):
    _rule(session, "R-1", anchor="financial_audit", missing=True, category="财务", status="approved")
    _rule(session, "R-2", anchor="qualification_grade", status="approved")
    prog = parse_service.pending_summary(session, project_id="PJ-T", material_id="MAT-T")
    assert prog["pending"] == 0 and prog["needs_redecision"] == 1
    assert parse_service.material_parse_status(session, project_id="PJ-T", material_id="MAT-T") == "manual_review"
    with pytest.raises(parse_service.ParseServiceError, match="旧版遗留"):
        parse_service.confirm_rules_from_approved(
            session, project_id="PJ-T", material_id="MAT-T", version=1, as_of="2025-12-19", created_by="x")
    # 遗留行允许重新决策（其他终态不允许）
    r = _decide(session, "R-1", "not_applicable", review_note="本文件无财务审计要求")
    assert r["status"] == "not_applicable"
    with pytest.raises(parse_service.CandidateAlreadyDecided):
        _decide(session, "R-2", "rejected", review_note="扫描不清")
    prog = parse_service.pending_summary(session, project_id="PJ-T", material_id="MAT-T")
    assert prog["needs_redecision"] == 0


# ── 逐字校验纯函数 ───────────────────────────────────────────────────

_PAGES_TXT = [
    (3, "3.2 具备 行政主管部门核发的工程设计综合甲级资质或工程设计市政行业乙级及以上资\n3"),
    (4, "质或工程设计市政行业（燃气工程、轨道交通工程除外）乙级及以上资质\n3.5 自 2020 年 9 月 1 日以来完成过一项 3000 万元及以上市政工程的施工业绩\n4"),
    (5, "5.1 投标文件递交的截止时间为 2025 年 12 月 19 日 09 时 00 分\n5"),
]


def test_verbatim_exact_offbyone_crosspage_fabricated():
    q = "自2020年9月1日以来完成过一项3000万元及以上市政工程的施工业绩"
    assert _verbatim_in_pages(_PAGES_TXT, q, 4) == (True, None, 4)
    assert _verbatim_in_pages(_PAGES_TXT, q, 5) == (True, None, 4)        # 填错一页 → 回报真实页
    cross = "工程设计综合甲级资质或工程设计市政行业乙级及以上资质或工程设计市政行业（燃气工程"
    assert _verbatim_in_pages(_PAGES_TXT, cross, 3) == (True, None, 3)    # 跨页且剥掉页脚页码「3」
    ok, reason, page = _verbatim_in_pages(_PAGES_TXT, "这句不在原文", 4)
    assert ok is False and "逐字子串" in reason and page is None
    ok, reason, _ = _verbatim_in_pages(_PAGES_TXT, q, 99)
    assert ok is False and "不存在" in reason


# ── as_of 建议 / 聚合视图 / 确认写入 ─────────────────────────────────

def test_suggested_as_of_from_confirmed_deadline_bid(session):
    assert parse_service.suggested_as_of(session, project_id="PJ-T", material_id="MAT-T", version=1) is None
    _field(session, "deadline_bid", value="2025-12-19 09:00")               # pending → 不算
    assert parse_service.suggested_as_of(session, project_id="PJ-T", material_id="MAT-T", version=1) is None
    _decide(session, "MAT-T:deadline_bid", "approved")
    s = parse_service.suggested_as_of(session, project_id="PJ-T", material_id="MAT-T", version=1)
    assert s == {"as_of": "2025-12-19", "source": "main_card:deadline_bid", "page_no": 5, "value": "2025-12-19 09:00"}


def test_grouped_candidates_view_v15_fields(session):
    _rule(session, "R-1", anchor="financial_audit", missing=True, category="财务",
          clause="招标公告 §3.13②")
    _rule(session, "R-2", anchor="qualification_grade", clause="招标公告 §3.2（招标公告 §3.2）")
    _rule(session, "R-3", anchor="design_lead", category="人员", clause="3.8（招标公告 §3.8）")
    _field(session, "deadline_bid", value="2025-12-19 09:00", status="approved")
    data = grouped_requirements(session, project_id="PJ-T")
    by = {g["group"]: g["items"] for g in data["groups"]}
    miss = next(i for i in by[GROUP_EVIDENCE] if i["id"] == "R-1")
    assert miss["missing"] is True and miss["clause_ref"] is None and miss["page_no"] is None
    assert "财务审计报告" in miss["locate_hints"] and "人工定位补录" in miss["issue"]
    assert miss["source_link"] == "/api/v1/materials/MAT-T/file?version=1"
    q = next(i for i in data["groups"][1]["items"] if i["id"] == "R-2")
    assert q["clause_ref"] == "招标公告 §3.2"                               # 历史「X（X）」折叠
    lead = next(i for i in by[GROUP_PERSONNEL] if i["id"] == "R-3")
    assert lead["title"] == "设计负责人执业资格（EPC）" and lead["clause_ref"] == "3.8（招标公告 §3.8）"
    assert data["as_of_suggestion"]["as_of"] == "2025-12-19"
    assert data["progress"]["needs_redecision"] == 0
    dl = next(i for i in by[GROUP_BASIC] if i["id"] == "MAT-T:deadline_bid")
    assert dl["review_status"] == "approved" and dl["page_no"] == 5


def test_confirm_writes_not_applicable_snapshot_and_page_no(session):
    _rule(session, "R-1", anchor="financial_audit", missing=True, category="财务", status="not_applicable")
    session.get(ParseCandidate, "R-1").review_note = "无此要求"
    _rule(session, "R-2", anchor="qualification_grade", status="approved", page_no=3)
    _rule(session, "R-3", anchor="similar_performance_hard", missing=True, category="业绩")
    _decide(session, "R-3", "revised", revised_payload={"assertion": "自2020年9月1日以来完成过一项业绩", "page_no": 4},
            verbatim_checker=OK_CHECK)
    session.commit()
    out = parse_service.confirm_rules_from_approved(
        session, project_id="PJ-T", material_id="MAT-T", version=1, as_of="2025-12-19", created_by="投标专员")
    assert out["created_requirements"] == 2 and out["not_applicable"] == 1 and out["rejected"] == 0
    rs = session.get(RuleSet, out["rule_set_id"])
    assert rs.snapshot["not_applicable"][0]["anchor_key"] == "financial_audit"
    assert rs.snapshot["not_applicable"][0]["note"] == "无此要求"
    reqs = {r.requirement_id: r for r in session.query(Requirement).all()}
    assert set(reqs) == {"R-2", "R-3"}
    assert reqs["R-2"].rule["page_no"] == 3 and reqs["R-3"].rule["page_no"] == 4
    assert reqs["R-3"].rule["located_by"] == "human" and reqs["R-3"].as_of == "2025-12-19"
    # confirmed 视图带页码与原文链接
    data = grouped_requirements(session, project_id="PJ-T")
    assert data["source"] == "confirmed"
    item = next(i for g in data["groups"] for i in g["items"] if i["id"] == "R-3")
    assert item["page_no"] == 4 and item["source_link"].endswith("/file?version=1")


# ── 抽取器 v1.5 ──────────────────────────────────────────────────────

def _page(n, *paras):
    return ParsedPage(page_no=n, paragraphs=list(paras))


_EPC_PAGES = [
    _page(1, "1"),
    _page(2, "目 录 第一章 招标公告 ... 3 第二章 投标人须知 ... 7"),
    _page(3,
          "第一章 招标公告",
          "河北大学七一路校区立体过街设施建设工程 工程总承包招标公告",
          "1.招标条件 河北大学七一路校区立体过街设施建设工程 已由 保定市发展和改革委员会批准建设",
          "3.1 具有独立企业法人资格、有效营业执照。"),
    _page(4,
          "3.3 本次招标（☑ 接受/□不接受）联合体投标。",
          "☑ 3.4 具备有效的企业安全生产许可证；",
          "3.5 自 2020 年 9 月 1 日以来完成过一项 3000 万元及以上市政工程的施工业绩或者 3000 万元及以上工程总承包业绩；",
          "□3.6 企业主要负责人具有对应有效的安全生产考核合格证书。",
          "（二）拟派项目经理、设计负责人、施工负责人（建造师）",
          "3.7 拟派项目经理具有注册在投标单位的市政公用工程专业壹级及以上注册建造师执业资格；☑ 同时具有对应有效的安全生产考核合格证书；",
          "3.8 拟派设计负责人具有 国家注册土木工程师（道路工程）执业资格。",
          "3.9 拟派施工负责人（建造师）具有 市政公用工程专业壹级及以上注册建造师 执业资格。"),
]


def test_similar_performance_since_date_variant_is_hard_and_required():
    assert "similar_performance_hard" not in OPTIONAL_ANCHORS
    by = {(c.rule or {}).get("anchor_key"): c for c in
          extract_rule_candidates(_EPC_PAGES, project_id="P", material_id="M", content_hash="h")}
    sp = by["similar_performance_hard"]
    assert not sp.missing_marker and sp.page_no == 4
    assert sp.assertion.startswith("自2020年9月1日以来完成过一项3000万元及以上市政工程的施工业绩")
    assert sp.clause_ref == "3.5（招标公告 §3.5）"
    # 无业绩条款的文件：必须产出 missing（可见），而不是静默消失
    by2 = {(c.rule or {}).get("anchor_key"): c for c in
           extract_rule_candidates([_page(1, "3.2 具备资质")], project_id="P", material_id="M", content_hash="h")}
    assert by2["similar_performance_hard"].missing_marker is True


def test_epc_leads_hit_real_clauses_not_section_header():
    by = {(c.rule or {}).get("anchor_key"): c for c in
          extract_rule_candidates(_EPC_PAGES, project_id="P", material_id="M", content_hash="h")}
    assert by["design_lead"].assertion.startswith("拟派设计负责人具有国家注册土木工程师")
    assert by["construction_lead"].assertion.startswith("拟派施工负责人（建造师）具有市政公用工程专业壹级")
    assert by["construction_lead"].clause_ref == "3.9（招标公告 §3.9）"
    # 项目经理锚点不再从章节标题「…施工负责人（建造师）3.7 拟派项目经理…」起匹配
    assert by["pm_registered_builder"].assertion.startswith("拟派项目经理具有注册在投标单位")
    # 追加锚点在字典末尾（候选序号稳定）；LOCATE_HINTS / 分组 / 标题已登记
    assert list(ANCHORS)[-2:] == ["design_lead", "construction_lead"]
    for k in ("design_lead", "construction_lead"):
        assert k in LOCATE_HINTS and k in parse_service.ANCHOR_GROUP and k in parse_service.ANCHOR_TITLE


def test_unchecked_box_clause_is_suppressed_but_checked_kept():
    by = {(c.rule or {}).get("anchor_key"): c for c in
          extract_rule_candidates(_EPC_PAGES, project_id="P", material_id="M", content_hash="h")}
    # ☑ 3.4 安许（已勾选）命中；「（☑ 接受/□不接受）联合体」含已勾选符号 → 保留
    assert not by["safety_license"].missing_marker
    assert not by["consortium"].missing_marker
    # pm_b_cert：□3.6 里的「具有对应有效的安全生产考核合格证书」不算；命中 3.7 的「☑ 同时具有…」
    assert by["pm_b_cert"].page_no == 4 and by["pm_b_cert"].assertion.startswith("同时具有对应有效的安全生产考核合格证书")
    # 只有 □ 版本时应被抑制为 missing
    only_unchecked = [_page(1, "□3.6 企业主要负责人同时具有对应有效的安全生产考核合格证书。")]
    by2 = {(c.rule or {}).get("anchor_key"): c for c in
           extract_rule_candidates(only_unchecked, project_id="P", material_id="M", content_hash="h")}
    assert by2["pm_b_cert"].missing_marker is True


def test_clause_ref_not_duplicated_when_no_clause_number():
    pages = [_page(1, "投标人具备有效的企业安全生产许可证。")]
    by = {(c.rule or {}).get("anchor_key"): c for c in
          extract_rule_candidates(pages, project_id="P", material_id="M", content_hash="h")}
    assert by["safety_license"].clause_ref == "招标公告 §3.4"      # 不再是「招标公告 §3.4（招标公告 §3.4）」


def test_main_card_project_name_and_type_from_announcement_title():
    mc = {m.field_key: m for m in extract_main_card(_EPC_PAGES)}
    assert mc["project_name"].value == "河北大学七一路校区立体过街设施建设工程" and mc["project_name"].page_no == 3
    assert mc["project_type"].value == "工程（总承包招标）"          # 图片封面 + 目录页 → 扩到前 6 页判类
    assert mc["region"].missing_marker is True                       # 无「建设地点」字段：如实缺失，不从招标人推断


def test_hebei_variants_bond_uppercase_noactive_officer_techbid():
    """河北大学 EPC 实测措辞（v1.5 放宽）：保证金大写金额且 ☑ 在片段内部 / 在施限制含「或施工负责人」/
    安全员「配备人数不少于1个」/ 技术标「（暗标）采用暗标方式」。"""
    pages = [_page(5, "3.10 投标人不得以拟派项目经理或施工负责人的身份参加本次投标；",
                   "☑3.11 专职安全生产管理人员具有对应有效的安全生产考核合格证书，配备人数不少于1个。"),
             _page(6, "9.1 本项目技术标（暗标）采用暗标方式编制及评审。"),
             _page(10, "3.4.1 投标有效期120日历天 □不要求提交投标保证金 ☑要求提交投标保证金 1.投标保证金的金额：人民币叁拾万元整，小写300000.00元。")]
    by = {(c.rule or {}).get("anchor_key"): c for c in
          extract_rule_candidates(pages, project_id="P", material_id="M", content_hash="h")}
    assert by["pm_no_active"].assertion == "不得以拟派项目经理或施工负责人的身份参加本次投标"
    assert by["safety_officer"].assertion.endswith("配备人数不少于1个") and by["safety_officer"].clause_ref.startswith("3.11")
    assert by["scoring_tech"].page_no == 6 and "暗标" in by["scoring_tech"].assertion
    assert by["bid_bond"].page_no == 10 and "人民币叁拾万元整" in by["bid_bond"].assertion
    # 只有 □ 不要求 的文件：保证金应缺失
    by2 = {(c.rule or {}).get("anchor_key"): c for c in extract_rule_candidates(
        [_page(10, "□要求提交投标保证金 1.投标保证金的金额：人民币叁拾万元整。")], project_id="P", material_id="M", content_hash="h")}
    assert by2["bid_bond"].missing_marker is True


def test_rejected_is_reopenable_after_finding_source(session):
    """2026-09-15：无法确认 = 搁置而非终局——找到原文后可改判人工定位（重开不抹审计）。"""
    _rule(session, "R-9", anchor="scoring_similar_performance", missing=True, category="业绩")
    _decide(session, "R-9", "rejected", review_note="扫描不清")
    r = _decide(session, "R-9", "revised", review_note="评标办法 p41 找到原文",
                revised_payload={"assertion": "除资格审查以外完成过一项业绩得5分", "page_no": 41},
                verbatim_checker=lambda *a, **k: {"ok": True, "check": "verified", "page_no": 41, "reason": None})
    assert r["status"] == "revised"
    assert r["revised_payload"]["page_no"] == 41 and r["revised_payload"]["missing_marker"] is False
    # approved / not_applicable 仍是终态
    _rule(session, "R-10", anchor="qualification_grade")
    _decide(session, "R-10", "approved")
    with pytest.raises(parse_service.CandidateAlreadyDecided):
        _decide(session, "R-10", "rejected", review_note="扫描不清")


# ── 2026-09-24 重解析刷新回归（邢台实测"每次都这样"的根因之一）──
# 此前重解析只增量新增候选、从不刷新仍是 pending 的旧行：抽取器修了 bug，
# 用户点「重新解析」仍看到旧 missing/错摘录；LLM 发现草稿 ID 是摘录哈希，跨轮
# 摘录措辞变化 → 新 ID 不断插入，旧 pending 不清 → 复核页重复行越积越多。
def _region_cand(value: str, missing: bool) -> dict:
    return {"field_key": "region", "value": value, "clause": "招标公告", "page_no": None,
            "assertion": "" if missing else f"建设地点：{value}", "confidence": "low" if missing else "high",
            "missing_marker": missing}


def test_reparse_refreshes_pending_but_never_decided(session):
    # 第一轮：region missing
    parse_service.store_candidates(session, project_id="PJ-T", material_id="MAT-T", version=1,
                                   kind=CANDIDATE_KIND_FIELD, candidates=[_region_cand("__待补__", True)])
    # 复核人已通过另一个字段（决策必须留痕，重解析不得覆盖）
    parse_service.store_candidates(session, project_id="PJ-T", material_id="MAT-T", version=1,
                                   kind=CANDIDATE_KIND_FIELD,
                                   candidates=[{"field_key": "tenderee", "value": "邢台交建",
                                                "clause": "招标公告", "page_no": 7,
                                                "assertion": "招标人为邢台交建", "confidence": "high",
                                                "missing_marker": False}])
    parse_service.decide_candidate(session, candidate_id="MAT-T:tenderee", material_id="MAT-T",
                                   version=1, decision="approved", reviewer="toubiao")
    # 第二轮：抽取器修复后 region 已定位（refresh_pending=True，与 worker 一致）
    parse_service.store_candidates(session, project_id="PJ-T", material_id="MAT-T", version=1,
                                   kind=CANDIDATE_KIND_FIELD,
                                   candidates=[_region_cand("中兴大街等13条街道", False),
                                               {"field_key": "tenderee", "value": "不应生效",
                                                "clause": "x", "page_no": 1, "assertion": "x",
                                                "confidence": "high", "missing_marker": False}],
                                   refresh_pending=True)
    rows = {r.candidate_id: r for r in session.scalars(
        __import__("sqlalchemy").select(ParseCandidate)).all()}
    assert rows["MAT-T:region"].status == "pending"
    assert rows["MAT-T:region"].payload["value"] == "中兴大街等13条街道"   # pending 被刷新
    assert rows["MAT-T:region"].payload["missing_marker"] is False
    assert rows["MAT-T:tenderee"].payload["value"] == "邢台交建"          # 已决策行不动


def test_prune_pending_llm_drafts_only_undiscovered_pending(session):
    from datetime import datetime, timezone

    def _row(cid: str, status: str, rule: dict) -> ParseCandidate:
        return ParseCandidate(
            candidate_id=cid, material_id="MAT-T", version=1, project_id="PJ-T",
            kind=CANDIDATE_KIND_RULE,
            payload={"requirement_id": cid, "req_type": "hard_requirement", "category": "人员",
                     "clause_ref": "3.6", "assertion": "…", "page_no": 8, "rule": rule,
                     "evidence_required": [], "confidence": "low", "missing_marker": False},
            status=status, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        )

    session.add_all([
        _row("MAT-T-LLM-aaaa-draft", "pending", {"type": "generic", "located_by": "llm", "discovered": True}),
        _row("MAT-T-LLM-bbbb-draft", "approved", {"type": "generic", "located_by": "llm", "discovered": True}),
        _row("MAT-T-H-003-draft", "pending", {"type": "project_manager", "anchor_key": "pm_registered_builder",
                                              "located_by": "llm"}),
    ])
    session.commit()
    pruned = parse_service.prune_pending_llm_drafts(session, material_id="MAT-T", version=1)
    assert pruned == 1
    left = {r.candidate_id for r in session.scalars(
        __import__("sqlalchemy").select(ParseCandidate)).all()}
    assert "MAT-T-LLM-aaaa-draft" not in left          # pending 的发现草稿被清
    assert "MAT-T-LLM-bbbb-draft" in left              # 已决策发现行留痕
    assert "MAT-T-H-003-draft" in left                 # 锚点行（ID 稳定）不走清理，由 refresh 覆盖


# ── 2026-09-24 解析优化四步之四：覆盖率报告 ──────────────────────────────
def test_coverage_from_candidates_pure_function():
    from runtime.parsing.extractor import RuleCandidate
    from runtime.db.parse_service import coverage_from_candidates

    def rc(anchor_key, *, missing=False, discovered=False, llm=False):
        rule = {"type": "generic", "anchor_key": anchor_key}
        if discovered:
            rule = {"type": "generic", "located_by": "llm", "discovered": True}
        elif llm:
            rule = {"type": "qualification", "anchor_key": anchor_key, "located_by": "llm"}
        return RuleCandidate(
            requirement_id=f"M-H-{anchor_key}-draft", req_type="hard_requirement",
            category="资质", clause_ref="3.1", assertion=parse_service.MISSING if missing else "具备资质",
            page_no=None if missing else 3, rule=rule, evidence_required=[], confidence="low",
            missing_marker=missing)

    fields = [
        type("F", (), {"missing_marker": False, "value": "中兴大街"})(),
        type("F", (), {"missing_marker": True, "value": parse_service.MISSING})(),
    ]
    cov = coverage_from_candidates(
        [rc("a"), rc("b", llm=True), rc("c", discovered=True), rc("d", missing=True)],
        fields, [type("T", (), {})])
    assert cov["located"] == {"anchor": 1, "llm_fallback": 1, "llm_discovery": 1}
    assert cov["located_total"] == 3 and cov["missing_pending"] == 1
    assert cov["main_card_located"] == 1 and cov["main_card_missing"] == 1
    assert cov["term_total"] == 1


def test_parse_coverage_db_and_grouped_passthrough(session):
    from runtime.db.parse_service import grouped_requirements, parse_coverage

    parse_service.store_candidates(session, project_id="PJ-T", material_id="MAT-T", version=1,
                                   kind=CANDIDATE_KIND_RULE, candidates=[{
                                       "requirement_id": "MAT-T-H-001-draft", "req_type": "hard_requirement",
                                       "category": "资质", "clause_ref": "3.1", "assertion": "具备市政资质壹级",
                                       "page_no": 3, "rule": {"type": "qualification", "anchor_key": "qualification_grade"},
                                       "evidence_required": [], "confidence": "high", "missing_marker": False}])
    parse_service.store_candidates(session, project_id="PJ-T", material_id="MAT-T", version=1,
                                   kind=CANDIDATE_KIND_RULE, candidates=[{
                                       "requirement_id": "MAT-T-LLM-abc-draft", "req_type": "hard_requirement",
                                       "category": "人员", "clause_ref": "3.6", "assertion": "企业负责人安全证",
                                       "page_no": 8, "rule": {"type": "generic", "located_by": "llm", "discovered": True},
                                       "evidence_required": [], "confidence": "low", "missing_marker": False}])
    parse_service.store_candidates(session, project_id="PJ-T", material_id="MAT-T", version=1,
                                   kind=CANDIDATE_KIND_FIELD, candidates=[
                                       {"field_key": "region", "value": "中兴大街等13条街道", "clause": "招标公告",
                                        "page_no": 7, "assertion": "建设地点：中兴大街…", "confidence": "high",
                                        "missing_marker": False},
                                       {"field_key": "tenderee", "value": parse_service.MISSING, "clause": "招标公告",
                                        "page_no": None, "assertion": "", "confidence": "low", "missing_marker": True}])
    cov = parse_coverage(session, project_id="PJ-T", material_id="MAT-T", version=1)
    assert cov["located"] == {"anchor": 1, "llm_fallback": 0, "llm_discovery": 1}
    assert cov["llm_pending"] == 1                      # 发现行仍 pending → 待人工确认数
    assert cov["main_card_located"] == 1 and cov["main_card_missing"] == 1
    assert cov["located_ratio"] == 1.0                  # 无缺失规则候选
    # 聚合视图透出（前端进度条数据源）
    data = grouped_requirements(session, project_id="PJ-T", material_id="MAT-T", version=1)
    assert data["coverage"]["rule_total"] == 2
