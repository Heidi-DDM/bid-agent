# R021/F021：条款/规则候选抽取器单测（合成 ParsedPage，无外部工具依赖）
# 覆盖：锚点抽取 21 类命中、跨行断言摘录、缺失标记、主卡必填字段、纯事实不推断。
from __future__ import annotations

from runtime.parsing.extractor import (
    MISSING,
    RuleCandidate,
    extract_main_card,
    extract_rule_candidates,
    req_type_to_code,
)
from runtime.rag.chunker import ParsedPage


def _page(page_no: int, *paragraphs: str) -> ParsedPage:
    return ParsedPage(page_no=page_no, paragraphs=list(paragraphs))


# 农大公告片段（模拟 p4-6，含跨行截断）
_PAGES = [
    _page(1, "河北农业大学东校区研究生宿舍建设项目施工", "招 标 文 件"),
    _page(2, "第一章 招标公告", "河北农业大学东校区研究生宿舍建设项目施工招标公告"),
    _page(3,
          "1.招标条件 河北农业大学东校区研究生宿舍建设项目已由河北省发展和改革委员会批复。",
          "招标人为河北农业大学，委托代理机构为河北悦中工程项目管理有限公司。",
          "建设地点：保定市莲池区灵雨寺街289号。",
          "2.2 本次招标最高投标限价 12471.590889 万元。",
          "3. 投标人资格要求（一）投标人"),
    _page(4,
          "3.2 具备建筑工程施工总承包二级及以上施工资质以及其他 / 资质。（不接受“资质预",
          "警”或“资质异常”企业投标）。",
          "3.3 本次招标（□接受/☑不接受）联合体投标。",
          "☑3.4 具备有效的企业安全生产许可证（联合体投标的，联合体成员均应提供）；",
          "3.7 拟派项目经理具有注册在投标单位的☑建筑工程专业二级及以上注册建造师执业资格；",
          "☑同时具有对应有效的安全生产考核合格证书；",
          "3.8 如拟派项目经理在投标截止日当日存在在其他在建合同工程担任项目经理的，不得以拟派",
          "项目经理的身份参加本次投标；",
          "☑3.9 提供 2025 年 01 月 01 日至投标截止时间内任意连续 3 个月在本单位缴纳社保的证明。",
          "☑3.10 专职安全生产管理人员具有对应有效的安全生产考核合格证书，配备人数 2 个；",
          "3.11 投标人及其拟派项目经理自 2022 年 9 月 1 日起至投标截止日止未被列入失信被执行人",
          "名单（以“信用中国网站…的信息为准）；",
          "3.13 ①拟派团队中…至少具备建筑工程、给排水、暖通、电气专业的技术人员各一名",
          "（以职称证或注册证显示专业为准）；②投标人财务状况良好，需提供 2022、2023、2024 年度",
          "经会计师事务所或第三方审计机构出具的财务审计报告…",
          "9.其他公示内容 本项目为“远程异地评标”项目…对技术标采取暗标评审。"),
    _page(5,
          "4.1 凡有意参加投标者，请于 2025 年 09 月 30 日 09 时 00 分至 2025 年 10 月 13 日",
          "17 时 00 分，登录冀招标全流程电子交易平台获取招标文件；",
          "5.1 投标文件递交的截止时间（投标截止时间，下同）为 2025 年 10 月 30 日 09 时 00 分。"),
    _page(6, "投标人须知", "3.3.1 投标有效期 120 日历天（从投标截止之日起算）",
          "☑要求提交投标保证金", "1.金额：人民币：20万元（贰拾万元整）。",
          "缴纳方式：银行汇票、银行电汇、支票、银行保函、电子保函、保证保险形式均可。",
          "投标保证金递交时间：在投标文件递交截止时间之前递交到以下账户，以到账时间为准。",
          "3.4.1 投标保证金 开户银行：保定银行股份有限公司直属支行",
          "4.1.5 开标时间：同投标截止时间"),
    _page(7, "第三章 评标办法", "四、评标程序", "(2)技术标采取暗标评审",
          "(3)(4)商务标采取明标评审；信用评分以 85 分为门槛",
          "(5)投标人具有一项及以上类似项目…单体建筑面积≥2万平方米的房屋建筑类业绩"),
]


def test_extract_rule_candidates_all_anchors_hit():
    cands = extract_rule_candidates(
        _PAGES, project_id="ND-2025", material_id="MAT-ND-TENDER",
        content_hash="abc123", as_of="2025-10-30",
    )
    assert len(cands) >= 20  # 21 anchors，全部命中（无 missing）
    missing = [c for c in cands if c.missing_marker]
    assert missing == [], f"不应有缺失: {missing}"
    # 类型分布（hard/scored/action 都有）
    types = {c.req_type for c in cands}
    assert "hard_requirement" in types and "scored_requirement" in types and "action_requirement" in types
    # 每条有页码与原文断言（可回跳）
    for c in cands:
        assert c.page_no is not None, c.requirement_id
        assert c.assertion, c.requirement_id


def test_extract_rule_candidates_key_values():
    cands = extract_rule_candidates(_PAGES, project_id="ND", material_id="M", content_hash="h")
    by_cat: dict[str, list[RuleCandidate]] = {}
    for c in cands:
        by_cat.setdefault(c.category, []).append(c)
    # 资质等级断言含原文
    qual = [c for c in cands if "建筑工程施工总承包二级及以上" in c.assertion]
    assert qual, "资质等级条款未命中"
    assert qual[0].page_no == 4
    # 人员社保（跨行摘录完整句）
    ss = [c for c in cands if "任意连续3个月" in c.assertion]
    assert ss and "缴纳社保" in ss[0].assertion
    # 保证金金额（20万原文）
    bond = by_cat.get("保证金", [])
    assert bond and any("20万元" in c.assertion for c in bond)
    # 递交截止（时间格式化由 rule 层处理，这里 assertion 保留原文）
    act = by_cat.get("递交", [])
    assert act and any("2025年10月30日09时00分" in c.assertion for c in act)
    # 评分/动作类型命中
    scored = [c for c in cands if c.req_type == "scored_requirement"]
    assert scored, "评分项未命中"


def test_extract_rule_candidates_cross_page_note():
    """跨行截断条款（3.8 无在建）应完整摘录。"""
    cands = extract_rule_candidates(_PAGES, project_id="ND", material_id="M", content_hash="h")
    no_active = [c for c in cands if "不得以拟派项目经理的身份" in c.assertion]
    assert no_active, "无在建条款未命中（跨行截断应被归一匹配捕获）"
    assert "参加本次投标" in no_active[0].assertion


def test_extract_rule_candidates_missing_when_no_match():
    """无匹配页 → hard/客观 scored 产出 missing_marker（人工复核），不推断。"""
    empty = [_page(1, "与招标无关的内容")]
    cands = extract_rule_candidates(empty, project_id="ND", material_id="M", content_hash="h")
    missing = [c for c in cands if c.missing_marker]
    assert missing, "无匹配时应产出缺失候选"
    for c in missing:
        assert c.assertion == MISSING
        assert c.note, "缺失候选必须带说明（禁止推断）"


def test_req_type_to_code():
    assert req_type_to_code("hard_requirement") == "H"
    assert req_type_to_code("scored_requirement") == "S"
    assert req_type_to_code("action_requirement") == "A"


def test_extract_main_card_required_fields():
    card = extract_main_card(_PAGES)
    fields = {c.field_key: c for c in card}
    # 必填字段全部有候选（值或缺失标记）
    for req in ("project_name", "tenderee", "region", "project_type", "deadline_bid"):
        assert req in fields, f"缺主卡字段 {req}"
    assert fields["project_name"].value == "河北农业大学东校区研究生宿舍建设项目施工"
    assert fields["deadline_bid"].value.startswith("2025-10-30")
    assert fields["deadline_bid"].value.endswith("09:00")
    assert fields["region"].value == "保定市莲池区" or "莲池区" in fields["region"].value
    # 没有字段值被推断成空以外的编造值
    for c in card:
        if c.missing_marker:
            assert c.value == MISSING
        else:
            assert c.value and c.value != MISSING


def test_extract_main_card_missing_markers():
    card = extract_main_card([_page(1, "无公告内容页")])
    by_key = {c.field_key: c for c in card}
    for req in ("project_name", "tenderee", "region", "deadline_bid"):
        assert by_key[req].missing_marker is True
        assert by_key[req].value == MISSING


def test_candidate_dict_shape():
    cands = extract_rule_candidates(_PAGES, project_id="ND", material_id="M", content_hash="h")
    d = cands[0].to_dict()
    for key in ("requirement_id", "req_type", "category", "clause_ref", "assertion",
                "page_no", "rule", "evidence_required", "confidence", "missing_marker", "note"):
        assert key in d, f"候选缺字段 {key}"
