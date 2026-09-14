# 2026-09-14 锚点扩充批次（招标文件侧）：ANCHORS 新增 16 项 + TERM_ANCHORS 13 项 + 复核分组登记
# 合成 ParsedPage（房建/市政招标文件常见措辞），无外部工具依赖。
from __future__ import annotations

from runtime.db import parse_service
from runtime.parsing.extractor import (
    ANCHORS,
    OPTIONAL_ANCHORS,
    TERM_ANCHORS,
    extract_rule_candidates,
    extract_term_candidates,
)
from runtime.rag.chunker import ParsedPage


def _page(page_no: int, *paragraphs: str) -> ParsedPage:
    return ParsedPage(page_no=page_no, paragraphs=list(paragraphs))


_PAGES = [
    _page(1, "某市某路道路工程施工", "招标文件"),
    _page(2,
          "3.1 投标人须具有独立承担民事责任的能力，具有有效的营业执照；",
          "3.5 业绩要求：近三年内完成过至少一项类似市政道路工程业绩（以竣工验收报告为准）；",
          "3.7 技术负责人：具有市政工程相关专业中级及以上职称；拟派项目经理近五年担任过一项类似市政工程项目经理业绩；",
          "3.11 参加本次招标活动前三年内在经营活动中没有重大违法记录；未被列入重大税收违法失信主体名单、",
          "政府采购严重违法失信行为记录名单；",
          "3.13 ③提供依法缴纳税收和社会保障资金的相关材料；",
          "3.14 资格审查方式：资格后审。"),
    _page(3,
          "第三章 评标办法 评标办法：综合评估法。",
          "(1)价格分：评标基准价为有效投标报价的算术平均值，价格分满分40分；",
          "(2)施工组织设计评分标准：施工方案完整合理得15分；",
          "(6)项目经理：具有一级注册建造师并有类似业绩的得5分；",
          "(4)企业荣誉：近三年获鲁班奖或国家优质工程奖的每项加2分；",
          "(7)拟投入本工程的主要施工机械设备满足需要得3分。"),
    _page(4,
          "1.9 现场踏勘：招标人不组织现场踏勘，投标人自行踏勘。",
          "1.10 投标人对招标文件有异议的，应在答疑截止时间2026年9月28日前提出。"),
    _page(5,
          "第四章 合同条款 计划工期：360日历天 质量标准：合格",
          "合同类型：固定单价合同。付款方式：按月进度付款，支付至已完工程量的80%。",
          "工程预付款为合同价款的20%；履约保证金为签约合同价的10%；质量保证金按结算价的3%预留；",
          "保修期：24个月。暂列金额：150万元。下浮率不得低于5%。安全文明施工费不参与竞争。",
          "技术标准和要求：执行现行国家及行业标准、规范。本工程不允许分包。"),
]


def _by_anchor(cands):
    return {(c.rule or {}).get("anchor_key"): c for c in cands}


def test_new_hard_anchors_hit_with_verbatim_assertion():
    cands = extract_rule_candidates(_PAGES, project_id="P", material_id="MAT-X", content_hash="h")
    by = _by_anchor(cands)
    for key, must in {
        "business_license": "独立承担民事责任",
        "similar_performance_hard": "类似市政道路工程业绩",
        "no_major_violation": "没有重大违法记录",
        "credit_blacklist": "重大税收违法失信主体名单",
        "tax_social_proof": "依法缴纳税收和社会保障资金",
        "prequalification_method": "资格后审",
        "tech_lead": "中级及以上职称",
        "pm_similar_performance": "类似市政工程项目经理业绩",
    }.items():
        c = by.get(key)
        assert c is not None and not c.missing_marker, f"{key} 应命中"
        assert c.req_type == "hard_requirement"
        assert must in c.assertion.replace(" ", ""), f"{key} 摘录应含 {must!r}: {c.assertion!r}"
        assert c.page_no == 2


def test_new_scored_and_action_anchors_hit():
    cands = extract_rule_candidates(_PAGES, project_id="P", material_id="MAT-X", content_hash="h")
    by = _by_anchor(cands)
    assert "综合评估法" in by["evaluation_method"].assertion
    assert by["evaluation_method"].req_type == "scored_requirement"
    assert "40分" in by["scoring_price"].assertion.replace(" ", "")
    assert "15分" in by["scoring_construction_plan"].assertion.replace(" ", "")
    assert "5分" in by["scoring_pm"].assertion
    assert "鲁班奖" in by["scoring_enterprise_honor"].assertion
    assert "3分" in by["scoring_equipment"].assertion
    assert by["action_site_visit"].req_type == "action_requirement" and "踏勘" in by["action_site_visit"].assertion
    assert "2026年9月28日" in by["action_q_and_a"].assertion.replace(" ", "")


def test_optional_anchors_do_not_emit_missing_noise():
    """扩充批次锚点未命中不产出 missing 候选——复核队列不被"本文件无此要求"淹没；基线 21 项语义不变。"""
    pages = [_page(1, "与任何锚点无关的正文。")]
    cands = extract_rule_candidates(pages, project_id="P", material_id="MAT-E", content_hash="h")
    keys = {(c.rule or {}).get("anchor_key") for c in cands}
    assert not (keys & OPTIONAL_ANCHORS), f"可选锚点不应产出 missing：{keys & OPTIONAL_ANCHORS}"
    # 基线必查项仍报缺
    assert "qualification_grade" in keys and "consortium" in keys


def test_every_anchor_registered_in_review_group_and_title():
    """新增锚点必须登记分组与标题，否则前端归"其他"且标题退化（parse_service 契约）。"""
    for key in ANCHORS:
        assert key in parse_service.ANCHOR_GROUP, f"{key} 未登记 ANCHOR_GROUP"
        assert key in parse_service.ANCHOR_TITLE, f"{key} 未登记 ANCHOR_TITLE"
    for key in TERM_ANCHORS:
        assert key in parse_service.TERM_TITLE, f"{key} 未登记 TERM_TITLE"


def test_term_candidates_values_and_pages():
    terms = {t.field_key: t for t in extract_term_candidates(_PAGES)}
    expect = {
        "duration": "360日历天",
        "quality_standard": "合格",
        "contract_type": "固定单价合同",
        "advance_payment": "20%",
        "performance_bond": "10%",
        "retention_money": "3%",
        "warranty": "24个月",
        "provisional_sum": "150万元",
        "downward_rate": "5%",
        "subcontract": "不允许分包",
    }
    for key, val in expect.items():
        assert key in terms, f"{key} 未产出"
        assert terms[key].value == val, f"{key}: {terms[key].value!r} != {val!r}"
        assert terms[key].page_no == 5
        assert terms[key].clause == "合同/商务条款"
    assert "80%" in terms["payment_terms"].value
    assert "不参与竞争" in terms["safety_fee"].value
    assert "国家及行业标准" in terms["tech_standard"].value


def test_term_candidates_absent_when_no_clause():
    """条款未载明不产出（不是缺失、不推断）。"""
    assert extract_term_candidates([_page(1, "无关正文")]) == []


def test_term_kind_review_group_and_title():
    payload = {"field_key": "performance_bond", "value": "10%"}
    assert parse_service._review_group(payload, kind=parse_service.CANDIDATE_KIND_TERM) == parse_service.GROUP_ACTION
    assert parse_service._review_title(payload, kind=parse_service.CANDIDATE_KIND_TERM) == "履约保证金 / 担保"
    assert parse_service._review_title({"field_key": "zzz"}, kind=parse_service.CANDIDATE_KIND_TERM).endswith("待复核）")


def test_subcontract_polarity_from_quote_not_from_keyword():
    """分包极性 = f(命中原文)：「允许将非主体工作分包」→ 允许；「不得分包」→ 不允许。"""
    allow = extract_term_candidates([_page(1, "允许将非主体、非关键性工作分包。")])
    deny = extract_term_candidates([_page(1, "本工程不得分包。")])
    assert {t.field_key: t.value for t in allow}["subcontract"] == "允许分包"
    assert {t.field_key: t.value for t in deny}["subcontract"] == "不允许分包"
