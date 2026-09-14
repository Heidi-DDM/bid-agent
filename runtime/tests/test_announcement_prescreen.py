# -*- coding: utf-8 -*-
# R004/F004：公告正文初筛结构化抽取单测
# 纯函数（无网络/DB），验证确定性锚点能抽出投标专员初筛字段 + 溯源 assertion +
# 缺失字段标注（missing），版式干扰（空格/☑/换行/分隔符号）下仍可判定。
from __future__ import annotations

from runtime.parsing.announcement_prescreen import extract_prescreen


# 真实公告正文（取自 2026-09-03 惠招标候选：太行冷链物流园山体冷库施工招标）
REAL_BODY = """2.1项目概况：
本项目总投资 1000 万元，其中建安工程估算价/万元，建设规模：
1.去氨化制冷工艺升级，淘汰液氨制冷设备;
2.制冷机房相关作业配套设施进行优化完善，包括制冷系统、地面、防水、电气、通信、消防、门窗等。
建设地点： 河北省石家庄市鹿泉区西北物流园太行街1号。
2.2本次招标最高投标限价： 842.002736 万元，其中：（1）设备购置费最高限价为 248.233729 万元；（2）工程费最高限价为： 593.769007 万元。
2.3计划工期： 45 日历天。
2.4质量标准：合格。
2.5其他：本工程共计划分1个标段。
3.投标人资格要求
3.2 具备在建主管部门核发的建筑工程施工总承包三级及以上资质或机电安装工程施工总承包三级及以上资质，并具有有效的安全生产许可证。
3.3 本次招标 ☑不接受联合体投标。
3.7 拟派项目经理具有注册在投标单位的机电工程一级注册建造师执业资格，同时具有某对应的安全生产考核合格证书（B证）。
3.10 专职安全生产管理人员具有安全生产考核合格证书，配备人数 1 个。
3.11 未被列入失信被执行人名单。
"""

REAL_TITLE = "太行智慧冷链物流园山体冷库项目施工招标公告"


def _val(res, key):
    f = res.get(key)
    return f.value if f is not None else None


def test_extract_fills_main_screen_fields():
    res = extract_prescreen(REAL_TITLE, REAL_BODY)
    assert _val(res, "region") and "鹿泉" in _val(res, "region")
    assert _val(res, "budget_amount") and "1000" in _val(res, "budget_amount")
    assert _val(res, "ceiling_price") and "842" in _val(res, "ceiling_price")
    assert _val(res, "equip_amount") and "248" in _val(res, "equip_amount")
    assert _val(res, "work_amount") and "593" in _val(res, "work_amount")
    assert _val(res, "duration") == "45 日历天"
    assert _val(res, "quality") == "合格"
    assert _val(res, "section_count") == "1"
    assert _val(res, "qualification") and "机电安装工程施工总承包" in _val(res, "qualification")
    assert _val(res, "pm_registered") and "注册建造师" in _val(res, "pm_registered")
    assert _val(res, "project_type") and "施工招标" in _val(res, "project_type")

    # 溯源断言存在
    assert res["ceiling_price"].assertion != ""
    assert res["region"].assertion != ""


def test_missing_fields_flagged_not_dropped():
    res = extract_prescreen("某项目施工招标公告", "本次招标范围：/。")
    assert res["region"].missing is True
    assert res["duration"].missing is True
    assert res["quality"].missing is True
    assert res["deadline_bid"].missing is True  # 必查时间字段未命中同样占位，不静默丢


def test_joint_venture_recognized():
    res = extract_prescreen(REAL_TITLE, REAL_BODY)
    # ☑不接受联合体 → 判定不含「接受」，不被「接受/」前缀误导
    jv = _val(res, "joint_venture")
    assert jv is not None and "接受" in jv


# 真实 EPC 公告正文（取自 2026-09-09 唐山市公共资源交易中心候选：唐山三友电子化学品
# 污水处理装置工程总承包）。与房建公告的措辞差异：资质按「资质要求：…；」整句列出设计+施工
# 多项资质（无「施工总承包」字样）、建造师等级用大写「贰级」、B 证用「取得」、信用用「未被…列入」、
# 质量用「质量要求」、报名窗口日期后紧跟时间。2026-09-09 前这些字段全部误判 missing。
EPC_BODY = """2.1.3 建设地点：河北省唐山市曹妃甸区南堡经济开发区 2.1.4 建设规模：年产3000吨电子级盐酸（具体以实际为准） 。
2.1.5 计划工期：150日历天。 2.1.6 设计质量标准：满足工程各方面的需求，符合国家和地方现行设计规范要求； 2.1.7 质量要求：合格。
3. 投标人资格要求 3.1 本次招标对投标人的资格要求如下：
3.1.1 资质要求：投标人须具有独立法人资格，持有工商行政管理部门登记的有效企业法人营业执照，具备住房城乡建设主管部门颁发的工程设计综合资质或具有建设行政主管部门核发的环境工程（水污染防治）专项设计甲级及以上资质，同时具有建设行政主管部门颁发的环保工程专业承包壹级资质，建筑机电安装工程专业承包二级及以上资质，有效的安全生产许可证；
3.1.2 项目经理资格要求：须具备有效期内机电工程专业贰级及以上注册建造师执业资格，同时取得安全生产考核合格证书（B类）。且未在其他在施建设工程项目中担任项目经理。
3.1.5 信誉要求：（1）投标人未被工商行政管理机关在全国企业信用信息公示系统（www.gsxt.gov.cn）列入严重违法失信企业名单；（2）投标人未被最高人民法院在“信用中国”网站（www.creditchina.gov.cn）或各级信用信息共享平台列入失信被执行人名单；
3.2 本次招标不接受联合体投标。 4. 招标文件的获取 4.1 凡有意参加投标者，请于2026年9月10日09:00至2026年9月16日17:00（北京时间，下同），登录河北省公共资源交易平台下载电子招标文件。
其他要求：特别提醒：投标人需完成市场主体注册（需在投标截止时间之前完成市场主体注册登记）。
5. 投标文件的递交 5.1 投标文件递交的截止时间（投标截止时间，下同）为2026年9月30日09时00分。
"""

EPC_TITLE = "唐山三友电子化学品有限责任公司年产3000吨电子级盐酸项目污水处理装置工程总承包招标公告"


def test_epc_wording_extracts_requirements_without_construction_general_contracting_phrase():
    res = extract_prescreen(EPC_TITLE, EPC_BODY)
    q = _val(res, "qualification")
    assert q and "环境工程（水污染防治）专项设计甲级" in q and "环保工程专业承包壹级" in q
    assert q.endswith("有效的安全生产许可证")  # 整句原文到「；」为止，不截断在首个「资质」
    pm = _val(res, "pm_registered")
    assert pm and "机电工程专业贰级及以上注册建造师" in pm
    assert _val(res, "pm_b_cert") == "同时取得安全生产考核合格证书（B类）"
    assert _val(res, "credit_ok") and _val(res, "credit_ok").endswith("列入失信被执行人名单")
    assert _val(res, "quality") == "合格"  # 「设计质量标准：满足…」不被误取，命中「质量要求：合格」
    assert _val(res, "duration") == "150日历天"
    assert _val(res, "region") == "河北省唐山市曹妃甸区"
    assert _val(res, "deadline_signup") == "2026年9月10日至2026年9月16日"
    # 「需在投标截止时间之前完成…」无日期不误取；命中 5.1「递交的截止时间（…下同）为2026年9月30日」
    assert _val(res, "deadline_bid") == "2026年9月30日"
    assert _val(res, "project_type") and "总承包招标" in _val(res, "project_type")
    # 公告正文未载明限价/总投资 → 必查字段保持 missing，不得推断填充
    assert res["ceiling_price"].missing is True
    assert res["budget_amount"].missing is True


def test_credit_anchor_stops_at_clause_boundary():
    # 「未被…列入严重违法失信企业名单；」不得跨「；」与后句拼成失信被执行人命中
    res = extract_prescreen("x施工招标公告", "投标人未被列入严重违法失信企业名单；其他要求略。")
    assert "credit_ok" not in res


# ── 2026-09-10 输出质量重构：条款级摘录（句界对齐）+ 完整谓语值 + 新字段 ──
# 用户实测样例（询比采购公告，政采类措辞）：旧版摘录出现「程地点：…」「…质量要求及采」
# 式断句（命中点前后硬切 16 字），且质量值只取「合格」丢掉附加要求。
INQUIRY_BODY = """2.5建设地点：采购人指定地点。
2.6质量要求：合格，且需符合国家及行业质量要求及采购人的要求。
3.供应商资格要求
3.1依法设立且满足如下要求：
(1）资质要求：供应商具有独立法人资格，具备有效的营业执照，具有建设行政主管部门核发的有效的施工劳务不分等级资质证书，具有有效的安全生产许可证，并在人员、设备、资金等方面具有相应的施工能力。
(2）业绩要求：近3年内（2023年9月10日至2026年9月9日）完成过一项类似项目业绩。
4.1有意参加询比采购活动的单位，请于2026年9月11日9：00分至2026年9月13日17：00分（北京时间，下同），登录平台报名。
采购人：某某集团有限公司。
"""


def test_inquiry_wording_full_clause_values_and_snippets():
    res = extract_prescreen("某公司劳务服务询比采购公告", INQUIRY_BODY)
    # 质量要求取完整谓语到句界（不再是首个命中词「合格」）
    assert _val(res, "quality") == "合格，且需符合国家及行业质量要求及采购人的要求"
    # 资质要求整句完整（独立法人资格→施工能力，不截断在「资质证书」）
    q = _val(res, "qualification")
    assert q.startswith("供应商具有独立法人资格") and q.endswith("具有相应的施工能力")
    assert "施工劳务不分等级资质证书" in q and "安全生产许可证" in q
    # 新字段：业绩要求 / 采购人
    assert _val(res, "performance") == "近3年内（2023年9月10日至2026年9月9日）完成过一项类似项目业绩"
    assert _val(res, "purchaser") == "某某集团有限公司"
    assert _val(res, "deadline_signup") == "2026年9月11日至2026年9月13日"
    # 摘录条款级对齐：以条款编号开头、以句读结尾，不再出现截半句
    qa = res["qualification"].assertion
    assert qa.startswith("(1）资质要求") and qa.endswith("施工能力。")
    pa = res["performance"].assertion
    assert pa.startswith("(2）业绩要求") and pa.endswith("业绩。")


def test_snippet_is_clause_aligned_not_hard_cut():
    res = extract_prescreen(REAL_TITLE, REAL_BODY)
    # 摘录起点=条款编号（句读后紧跟的编号属本条款），终点=句读——不出现「程地点」截字
    qa = res["quality"].assertion
    assert qa.startswith("2.4质量标准") and qa.endswith("。")
    ra = res["region"].assertion
    assert ra.startswith("建设地点") and ra.endswith("。") and "鹿泉" in ra
    ca = res["ceiling_price"].assertion
    assert ca.startswith("2.2本次招标最高投标限价")


def test_scale_and_purchaser_new_anchors():
    res = extract_prescreen("某项目施工招标公告",
                            "建设规模：年产3000吨电子级盐酸。采购人：某集团有限公司。")
    assert _val(res, "scale") == "年产3000吨电子级盐酸"
    assert _val(res, "purchaser") == "某集团有限公司"

# ── 2026-09-11 P0：河北工业大学（二次）案例（docs/10 §1）——用户实测三个 bug：
#    ① 资质要求在"；"截断丢「安全生产许可证」子项；② 表单式「是否接受联合体投标：否」被读成
#    「接受」；③ 联合体摘录横跨到相邻字段「是否专门面向中小企业」。正文按用户提供的公告原文
#    片段构造（政采类表单式版式）。 ──
HGD_TITLE = "河北工业大学室外下水疏通掏挖清淤服务采购项目（二次）公开招标公告"
HGD_BODY = """项目名称：河北工业大学室外下水疏通掏挖清淤服务采购项目（二次） 采购需求：室外下水疏通掏挖清淤服务。
三、投标人的资格要求：投标人应具备建设行政主管部门核发的建筑工程施工总承包三级及以上资质或市政公用工程施工总承包三级及以上资质；具有建设行政主管部门颁发的有效期内的安全生产许可证。本项目的特定资格要求：略。
是否接受联合体投标：否 是否专门面向中小企业：否
"""


def test_hgd_qualification_keeps_semicolon_parallel_subitems():
    res = extract_prescreen(HGD_TITLE, HGD_BODY)
    q = _val(res, "qualification")
    assert q.startswith("具备建设行政主管部门核发的建筑工程施工总承包三级及以上资质")
    assert "市政公用工程施工总承包三级及以上资质" in q
    # "；"后的并列子项不丢（旧版在首个"；"截断）
    assert q.endswith("具有建设行政主管部门颁发的有效期内的安全生产许可证")
    # 摘录覆盖值的全部子项，且不吞并下一条款
    qa = res["qualification"].assertion
    assert qa.startswith("投标人应具备") and qa.endswith("安全生产许可证。")
    assert "特定资格要求" not in qa


def test_hgd_joint_venture_form_style_polarity_and_snippet():
    res = extract_prescreen(HGD_TITLE, HGD_BODY)
    assert _val(res, "joint_venture") == "不接受联合体投标"
    # 摘录 = 本字段 label+值，不横跨到「是否专门面向中小企业」
    assert res["joint_venture"].assertion == "是否接受联合体投标：否"


def test_joint_venture_polarity_derived_from_quote_across_layouts():
    cases = [
        ("是否接受联合体投标：是", "接受联合体投标"),
        ("本项目（是/否）接受联合体投标：否", "不接受联合体投标"),
        # 政采网表单真实版式（河北工大（二次）实抓正文）：0=否 1=是——旧锚点把标签片段
        # 「接受联合体投标」当成值，正是用户报告的极性反转
        ("本项目（是/否）接受联合体投标： 0", "不接受联合体投标"),
        ("本项目（是/否）接受联合体投标： 1", "接受联合体投标"),
        ("本次招标 ☑不接受联合体投标。", "不接受联合体投标"),
        ("3.3 本次招标（□接受/☑不接受）联合体投标。", "不接受联合体投标"),
        ("本项目接受联合体投标。", "接受联合体投标"),
        ("本次招标不接受联合体投标。", "不接受联合体投标"),
        ("联合体投标：不接受", "不接受联合体投标"),
        # 表格单元格转行后再折叠为空格（parsers.html_to_text td 转行）
        ("是否接受联合体投标 否 是否专门面向中小企业 否", "不接受联合体投标"),
    ]
    for body, expect in cases:
        assert _val(extract_prescreen("x施工招标公告", body), "joint_venture") == expect, body
    # 判不出极性（两列表头与两列值分离）→ 不产出，不推断
    res = extract_prescreen("x施工招标公告", "是否接受联合体投标 是否专门面向中小企业 否 否")
    assert "joint_venture" not in res


def test_label_tail_stops_at_next_label_in_form_layout():
    # 表单式公告字段间无句读：值与摘录以下一个 label 起点为界，不再吞并相邻字段
    res = extract_prescreen(
        "x施工招标公告",
        "采购人：河北某某大学 建设地点：石家庄市新华区 计划工期：180日历天 质量标准：合格。")
    assert _val(res, "purchaser") == "河北某某大学"
    assert _val(res, "quality") == "合格"
    assert res["purchaser"].assertion == "采购人：河北某某大学"


def test_label_style_qualification_continues_over_semicolon_but_stops_at_next_clause():
    body = ("3.1.1 资质要求：具备建筑工程施工总承包三级及以上资质；具有有效的安全生产许可证；"
            "3.1.2 项目经理资格要求：须具备二级及以上注册建造师执业资格。")
    q = _val(extract_prescreen("x施工招标公告", body), "qualification")
    assert q == "具备建筑工程施工总承包三级及以上资质；具有有效的安全生产许可证"
    # "；（2）业绩要求：" 亦为下一条款（编号后紧跟 label）
    body2 = "资质要求：（1）具备市政公用工程施工总承包三级资质；（2）具有安全生产许可证；（3）业绩要求：近三年一项。"
    q2 = _val(extract_prescreen("x施工招标公告", body2), "qualification")
    assert q2 == "（1）具备市政公用工程施工总承包三级资质；（2）具有安全生产许可证"
