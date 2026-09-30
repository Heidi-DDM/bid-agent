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
    assert len(cands) >= 20  # 基线 21 anchors 全部命中
    missing = [c for c in cands if c.missing_marker]
    # v1.5：类似业绩硬性要求改为必查——农大文件资格审查无该条款（类似业绩是评分项），
    # 须以可见的 missing 候选交复核人确认「本文件无此条款」，不得静默消失（F021 §2.1 v1.5）
    assert [(c.rule or {}).get("anchor_key") for c in missing] == ["similar_performance_hard"], \
        f"缺失项应仅为 similar_performance_hard: {missing}"
    # 类型分布（hard/scored/action 都有）
    types = {c.req_type for c in cands}
    assert "hard_requirement" in types and "scored_requirement" in types and "action_requirement" in types
    # 每条已定位候选有页码与原文断言（可回跳）
    for c in cands:
        if c.missing_marker:
            continue
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


# EPC 工程总承包公告措辞（取自 2026-09-09 唐山三友电子化学品污水处理装置项目，唐山市公共资源交易中心）：
# 资质按「资质要求：…；」整句列设计+施工多项资质（无「施工总承包」）、建造师等级「贰级」、B 证「取得」、
# 无在建「未在其他在施…担任项目经理」、社保「养老保险缴费证明」、信用「未被…列入」、报名「下载电子招标文件」。
# 2026-09-10 前这些锚点只认农大房建措辞，全部误判 __待补__。
_EPC_PAGES = [
    _page(1, "2.1.1 项目名称：唐山三友电子化学品有限责任公司年产3000吨电子级盐酸项目污水处理装置工程总承包",
          "2.1.3 建设地点：河北省唐山市曹妃甸区南堡经济开发区 2.1.5 计划工期：150日历天。"),
    _page(2,
          "3.1.1 资质要求：投标人须具有独立法人资格，持有工商行政管理部门登记的有效企业法人营业执照，具备住房城乡建设主管部门颁发的工程设计综合资质",
          "或具有建设行政主管部门核发的环境工程（水污染防治）专项设计甲级及以上资质，同时具有建设行政主管部门颁发的环保工程专业承包壹级资质，",
          "建筑机电安装工程专业承包二级及以上资质，有效的安全生产许可证；",
          "3.1.2 项目经理资格要求：须具备有效期内机电工程专业贰级及以上注册建造师执业资格，同时取得安全生产考核合格证书（B类）。",
          "且未在其他在施建设工程项目中担任项目经理。",
          "注册建造师证书所记载的聘用单位必须为该投标单位并提供其在本单位连续缴纳近三个月的养老保险缴费证明；",
          "3.1.5 信誉要求：（1）投标人未被工商行政管理机关列入严重违法失信企业名单；（2）投标人未被最高人民法院在“信用中国”网站",
          "（www.creditchina.gov.cn）或各级信用信息共享平台列入失信被执行人名单；",
          "3.2 本次招标不接受联合体投标。",
          "4.1 凡有意参加投标者，请于2026年9月10日09:00至2026年9月16日17:00（北京时间，下同），登录河北省公共资源交易平台下载电子招标文件。",
          "5.1 投标文件递交的截止时间（投标截止时间，下同）为2026年9月30日09时00分。"),
]


def test_extract_rule_candidates_epc_wording():
    cands = extract_rule_candidates(_EPC_PAGES, project_id="PJ", material_id="MAT-EPC", content_hash="h")
    by_anchor = {c.rule.get("anchor_key"): c for c in cands}

    def hit(key: str) -> RuleCandidate:
        c = by_anchor[key]
        assert not c.missing_marker, f"{key} 不应缺失"
        return c

    q = hit("qualification_grade")
    assert q.assertion.startswith("资质要求：") and "环境工程（水污染防治）专项设计甲级" in q.assertion
    assert q.page_no == 2
    assert hit("safety_license").assertion == "有效的安全生产许可证"
    assert "机电工程专业贰级及以上注册建造师执业资格" in hit("pm_registered_builder").assertion
    assert hit("pm_b_cert").assertion == "同时取得安全生产考核合格证书（B类）"
    assert hit("pm_no_active").assertion == "未在其他在施建设工程项目中担任项目经理"
    assert hit("pm_social_security").assertion == "连续缴纳近三个月的养老保险缴费证明"
    credit = hit("credit_no_loser").assertion
    assert credit.startswith("未被最高人民法院") and credit.endswith("列入失信被执行人名单")
    assert hit("consortium").assertion == "不接受联合体投标"
    assert hit("action_file_acquisition").assertion.endswith("下载电子招标文件")
    assert "2026年9月30日09时00分" in hit("action_deadline_bid").assertion
    # 公告未载明的项（安全员/技术团队/财务/有效期/保证金/限价/评分）保持 __待补__，不推断
    for key in ("safety_officer", "tech_team", "financial_audit", "bid_validity",
                "bid_bond", "ceiling_price", "scoring_tech", "scoring_similar_performance"):
        assert by_anchor[key].missing_marker is True, key
        assert by_anchor[key].assertion == MISSING


def test_extract_main_card_epc_wording():
    fields = {c.field_key: c for c in extract_main_card(_EPC_PAGES)}
    assert fields["project_name"].value == "唐山三友电子化学品有限责任公司年产3000吨电子级盐酸项目污水处理装置工程总承包"
    assert fields["region"].value == "河北省唐山市曹妃甸区"  # 省前缀地址
    assert fields["deadline_bid"].value == "2026-09-30 09:00"
    assert fields["deadline_signup"].value == "2026-09-10 09:00"
    # 公告无「招标人为…」→ 必填字段缺失标记，不从项目名推断招标人
    assert fields["tenderee"].missing_marker is True and fields["tenderee"].value == MISSING


# ── 2026-09-11 P0（docs/10 附录 A extractor #1/#3）：联合体极性 = f(命中原文)；
#    「资质要求：…；…」的"；"并列子项不截断、下一条款收尾 ──
def _by_anchor(pages):
    return {c.rule.get("anchor_key"): c for c in extract_rule_candidates(
        pages, project_id="PJ", material_id="MAT-P0", content_hash="h")}


def test_consortium_polarity_derived_not_keyword_presence():
    # 农大勾选式「（□接受/☑不接受）联合体投标」→ 只认已勾选项
    nd = _by_anchor(_PAGES)["consortium"]
    assert nd.missing_marker is False and nd.rule["accepts_consortium"] is False
    # EPC 叙述式
    epc = _by_anchor(_EPC_PAGES)["consortium"]
    assert epc.rule["accepts_consortium"] is False and epc.assertion == "不接受联合体投标"
    # 河北工大表单式「是否接受联合体投标：否」：旧锚点会命中标签片段「接受联合体投标」
    form = _by_anchor([_page(1, "是否接受联合体投标：否 是否专门面向中小企业：否")])["consortium"]
    assert form.missing_marker is False
    assert form.assertion == "是否接受联合体投标：否" and form.rule["accepts_consortium"] is False
    # 公告写「接受联合体」：旧锚点只认「不接受」→ 误进 missing 复核队列
    acc = _by_anchor([_page(1, "3.3 本项目接受联合体投标，联合体各方应签署共同投标协议。")])["consortium"]
    assert acc.missing_marker is False and acc.rule["accepts_consortium"] is True
    assert acc.assertion == "接受联合体投标"


def test_qualification_label_style_keeps_semicolon_subitems_stops_at_next_clause():
    pages = [_page(2,
                   "3.1.1 资质要求：具备建筑工程施工总承包三级及以上资质或市政公用工程施工总承包三级及以上资质；",
                   "具有建设行政主管部门颁发的有效期内的安全生产许可证；",
                   "3.1.2 项目经理资格要求：须具备二级及以上注册建造师执业资格。")]
    q = _by_anchor(pages)["qualification_grade"]
    assert q.assertion.startswith("资质要求：具备建筑工程施工总承包三级及以上资质")
    assert q.assertion.endswith("有效期内的安全生产许可证")  # "；"后子项保留
    assert "项目经理" not in q.assertion                       # 下一条款不吞并
    # "；（2）业绩要求：" 亦收尾（编号后紧跟 label）
    pages2 = [_page(2, "资质要求：（1）具备市政资质壹级；（2）具有安全生产许可证；（2）业绩要求：近三年一项。")]
    q2 = _by_anchor(pages2)["qualification_grade"]
    assert q2.assertion == "资质要求：（1）具备市政资质壹级；（2）具有安全生产许可证"


# ── 2026-09-24 邢台排水管网实测回归（region/ceiling 正则过约束 + 表头 FP）──
# 实测材料：MAT-PJ1d7fadae0b-TENDER（115 页），三个用户可见 bug：
#   ① region 只认「省?市…区/县」行政区划版式，原文「建设地点：中兴大街(钢铁路-滨江路、
#      襄都路-高速路下道口)等 13 条街道。」是街道描述 → 每次都 missing；
#   ② ceiling_price 单位写死「万元」，原文「最高投标限价328447260元」为元计 → 报缺失；
#   ③ pm_similar_performance 把「近年完成的类似项目情况表」表头（项目经理 技术负责人
#      项目描述 备注…）当成项目经理业绩要求（「技术负责人」里的「负责」被当谓语）。

_XT_PAGES = [
    _page(1, "邢台市市区部分街道排水管网提升改造项目施工招标公告"),
    _page(7,
          "1.2.1 本招标项目的建设地点：中兴大街(钢铁路-滨江路、襄都路-高速路下道口)等 13 条街道。",
          "2.2 本次招标范围和内容：审定施工图纸及工程量清单范围内的全部内容。本次招标最高投标限价328447260元。"),
    _page(105, "（二）近年完成的类似项目情况表", "项目名称", "项目所在地", "合同价格", "开工日期",
          "竣工日期", "工程质量", "项目经理", "技术负责人", "项目描述", "备注",
          "备注：1、类似项目指市政公用工程。"),
]


def test_region_street_description_not_admin_division():
    fields = {c.field_key: c for c in extract_main_card(_XT_PAGES)}
    assert fields["region"].missing_marker is False
    assert fields["region"].value == "中兴大街(钢铁路-滨江路、襄都路-高速路下道口)等13条街道"
    assert fields["region"].page_no == 7


def test_region_admin_division_wording_unchanged():
    # 行政区划式（EPC 既有口径）不受兜底分支影响
    pages = [_page(1, "2.1.3 建设地点：河北省唐山市曹妃甸区南堡经济开发区 2.1.5 计划工期：150日历天。")]
    fields = {c.field_key: c for c in extract_main_card(pages)}
    assert fields["region"].value == "河北省唐山市曹妃甸区"


def test_region_back_reference_skipped_and_fulltext_fallback():
    # 公告区只有「见投标人须知前附表」回引 → 跳过；真实地点在 12 页之外时扩全文
    pages = [
        _page(2, "1.1.5 建设地点：见投标人须知前附表。"),
        _page(13, "1.1.2 本工程施工场地(现场)具体地理位置如下：建设地点：襄都区泉北大街东段。"),
    ]
    fields = {c.field_key: c for c in extract_main_card(pages)}
    assert fields["region"].missing_marker is False
    assert fields["region"].value == "襄都区泉北大街东段"
    assert fields["region"].page_no == 13


def test_ceiling_price_accepts_yuan_unit():
    fields = {c.field_key: c for c in extract_main_card(_XT_PAGES)}
    assert fields["ceiling_price"].value == "328447260元"
    anchor = _by_anchor(_XT_PAGES)["ceiling_price"]
    assert anchor.missing_marker is False
    assert anchor.assertion == "最高投标限价328447260元"
    # 万元口径（农大）不回归
    nd = _by_anchor(_PAGES)["ceiling_price"]
    assert nd.missing_marker is False and "12471.590889万元" in nd.assertion


def test_pm_similar_performance_form_table_header_not_matched():
    # 修复前：表头「项目经理 技术负责人 项目描述 备注…备注：1、类似项目…」被当成
    # 项目经理类似业绩要求（assertion 抓成整行表头）；pm_similar_performance 是可选
    # 锚点，0 命中应不产出候选
    cands = extract_rule_candidates(_XT_PAGES, project_id="PJ", material_id="M", content_hash="h")
    hits = [c for c in cands if (c.rule or {}).get("anchor_key") == "pm_similar_performance"]
    assert hits == []
    # 真实条款措辞仍命中
    real = _by_anchor([_page(3, "3.7 拟派项目经理近五年内担任过一项类似市政公用工程的施工项目负责人。")])
    c = real["pm_similar_performance"]
    assert c.missing_marker is False and "担任" in c.assertion


# ── 2026-09-24 解析优化四步之二：版面层表格兜底（pdftotext 拍平前附表后文本流
#    错行的字段，由 ParsedPage.tables 的 label→右邻单元格兜底；可选能力零副作用）──
def _tpage(page_no: int, *paragraphs: str, tables=None):
    return ParsedPage(page_no=page_no, paragraphs=list(paragraphs), tables=tables or [])


def test_main_card_table_fallback_region_and_tenderee():
    # 文本流被邻列内容错行（无「建设地点：值」连写、招标人栏只留名称截断）——版面层兜底
    pages = [
        _tpage(11,
               "1.1.4 项目名称 邢台市市区部分街道排水管网提升改造项目施工",
               "资金来源及 市政府投资、100%",
               "1.2.1 比例",
               tables=[["条款号", "条款名称", "编 列 内 容"],
                       ["1.1.2", "招标人", "名称：邢台交建全过程工程管理有限公司地址：河北省邢台市"],
                       ["1.1.5", "建设地点", "中兴大街(钢铁路-滨江路、襄都路-高速路下道口)等 13 条街道。"]]),
    ]
    fields = {c.field_key: c for c in extract_main_card(pages)}
    assert fields["region"].missing_marker is False
    assert fields["region"].value == "中兴大街(钢铁路-滨江路、襄都路-高速路下道口)等13条街道。".rstrip("。")
    assert fields["region"].page_no == 11
    assert fields["tenderee"].missing_marker is False
    assert fields["tenderee"].value == "邢台交建全过程工程管理有限公司"  # 剥「名称：」、地址处收尾
    # 招标人文本流原口径（「招标人为：…」）不受表格兜底影响
    plain = {c.field_key: c for c in extract_main_card(
        [_tpage(3, "招标人为：河北农业大学，委托代理机构为河北悦中工程项目管理有限公司。")])}
    assert plain["tenderee"].value == "河北农业大学"


def test_main_card_table_fallback_amount_and_backref():
    pages = [
        _tpage(11,
               "投标人须知前附表",
               tables=[["条款号", "条款名称", "编 列 内 容"],
                       ["1.1.5", "建设地点", "见投标人须知前附表"],   # 回引不算地点
                       ["2.2", "最高投标限价", "328447260元（其中含税）"]]),
    ]
    fields = {c.field_key: c for c in extract_main_card(pages)}
    assert fields["ceiling_price"].value == "328447260元"  # 金额格内取数字+单位
    assert fields["region"].missing_marker is True          # 表格里也只有回引 → 如实缺失


def test_table_fallback_absent_tables_zero_effect():
    # 无 tables（缺 pdfplumber / 无表格页）：与从前行为完全一致
    fields = {c.field_key: c for c in extract_main_card(
        [_page(1, "无任何字段的页面")])}
    assert fields["region"].missing_marker is True
    assert fields["tenderee"].missing_marker is True


def test_router_attaches_tables(monkeypatch):
    from runtime.parsing import layout, router

    monkeypatch.setattr(layout, "extract_pdf_tables",
                        lambda path, max_pages=20: {11: [["1.1.5", "建设地点", "襄都区泉北大街"]]})
    pages = [router.ParsedPage(page_no=11, paragraphs=["1.1.5 建设地点 襄都区泉北大街"])]
    router._attach_page_tables("/dev/null", pages)
    assert pages[0].tables and pages[0].tables[0][1] == "建设地点"
    # 版面层抛异常不阻断主链
    def boom(path, max_pages=20):
        raise RuntimeError("boom")
    monkeypatch.setattr(layout, "extract_pdf_tables", boom)
    router._attach_page_tables("/dev/null", pages)
    assert pages[0].page_no == 11

def test_bid_bond_amount_bare_wan_not_parsed_as_zero():
    """2026-09-29 修复：「人民币50万元」的裸「万元」不得被大写金额解析为 0.0 短路阿拉伯解析。"""
    from runtime.parsing.extractor import _cn_amount, structure_rule_params

    assert _cn_amount("人民币50万元（不超过50万元）") is None  # 裸单位不是大写金额
    assert _cn_amount("人民币叁拾万元整") == 300000.0
    p = structure_rule_params(
        "bid_bond",
        "要求提交投标保证金1.金额：人民币50万元（不得超过项目估算价的2%）2.缴纳方式：银行保函")
    assert p["amount"] == 500000.0 and p["forms"] == ["银行保函"]
