# R021/F021 §4：招标文件条款/规则候选抽取器
# 把 Document Router 输出的 ParsedPage 转为"规则候选 JSON"（与 golden_requirements_nongda.json
# 同构的 RuleCandidate：requirement_id/req_type/category/clause_ref/assertion/page_no/rule/
# evidence_required/confidence/missing_marker）。
#
# 设计约束（冻结契约）：
# - 模型（DeepSeek）测试期关闭 → 本模块是"确定性锚点抽取"，产出候选供人工复核确认，
#   不做 LLM 条款分类；模型可用时替换/增强 clause 语义层，不改变候选 Schema（F021 §2.5-2.6）。
# - 纯事实：assertion 一律原文逐字摘录（可回跳页码）；找不到的字段输出 __待补__，不推断。
# - 客观评分项疑似漏项/字段无引用 → manual_review 标记（F005 §4.2.1 客观项无遗漏提取）。
# - 每条候选带 page_no + clause 定位；confidence 来自锚点强度（标题行=high、正文命中=medium、
#   推断定位=low；无锚点=不产出该候选或 __待补__）。
#
# 输入：list[ParsedPage]（router 产物）＋ project_id / material_id / content_hash。
# 输出：list[RuleCandidate]。
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from runtime.parsing.polarity import CONSORTIUM_ANY, classify_project_type, consortium_match

# 规则类型（F008 §4.1 / golden 文件同构）
REQ_TYPES = ("hard_requirement", "scored_requirement", "action_requirement")

# 置信度枚举（对齐 F005 §4.3：官方=confirmed/high/medium/low；这里锚点强度映射）
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

MISSING = "__待补__"


@dataclass
class RuleCandidate:
    """规则候选（人工复核确认后才写入 RuleSet/Requirement；历史版本不可覆盖）。"""

    requirement_id: str  # 草案 ID（如 NQ-H-001-draft）
    req_type: str
    category: str
    clause_ref: str
    assertion: str
    page_no: Optional[int]
    rule: dict
    evidence_required: list[str]
    confidence: str
    missing_marker: bool = False  # True=未能定位的必查项（进 manual_review）
    note: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "requirement_id": self.requirement_id,
            "req_type": self.req_type,
            "category": self.category,
            "clause_ref": self.clause_ref,
            "assertion": self.assertion,
            "page_no": self.page_no,
            "rule": self.rule,
            "evidence_required": self.evidence_required,
            "confidence": self.confidence,
            "missing_marker": self.missing_marker,
            "note": self.note,
        }


# ── 文本归一/辅助 ──────────────────────────────────────────────────────


def _norm(text: str) -> str:
    """归一空白（保留中文标点与数字），便于正则。"""
    return re.sub(r"\s+", "", text)


def _pages_text(pages: list) -> str:
    """全部页文本（带页码标记）——用于跨页匹配大结构；单条锚点用 _page_text。"""
    return "\n".join(f"@@PAGE {p.page_no}@@\n" + "\n".join(p.paragraphs) for p in pages)


def _page_fulltext(p: object) -> str:
    """整页原文（段落间加换行），用于跨行模式匹配；页码由 p.page_no 给出。"""
    return "\n".join(getattr(p, "paragraphs", []) or [])


def _find_in_pages(pages: list, pattern: re.Pattern) -> list[tuple[int, str, str]]:
    """在逐页文本中查找 pattern（支持跨行，re.S）；返回 [(page_no, 命中原文摘录, 归一匹配串)]。

    assertion 摘录 = 原文跨行片段（保留原文，清洗行内多余空白），可回跳页码。
    归一匹配串用于定位规则值（金额/日期等由调用方二次解析）。
    """
    hits: list[tuple[int, str, str]] = []
    for p in pages:
        joined = _page_fulltext(p)
        # 归一化用于匹配（去所有空白），同时保留原文偏移关系：逐字符映射归一位置 → 原文字符
        norm_chars: list[str] = []
        raw_chars: list[str] = []
        for ch in joined:
            if ch in " \t\r\n\f":
                continue
            norm_chars.append(ch)
            raw_chars.append(ch)
        norm_text = "".join(norm_chars)
        for m in pattern.finditer(norm_text):
            # 归一命中区间 → 原文对应区间（去除空白字符后位置一致）
            start, end = m.start(), m.end()
            snippet = "".join(raw_chars[start:end])
            hits.append((p.page_no, snippet, norm_text[start:end]))
    return hits


_UNCHECKED_BOX = "□"
_CHECKED_BOXES = ("☑", "■", "√", "☒", "☒", "✓", "✔")


# 极性由 polarity 模块从命中原文派生的锚点（「（□接受/☑不接受）联合体」勾选语义在片段内部），不做抑制
_POLARITY_ANCHORS = frozenset({"consortium"})


def _unchecked_option(preceding: str, matched_head: str = "") -> bool:
    """命中片段所属条款是否为「□ 未勾选」的可选项（勾选式招标文件：□3.6 … / ☑3.4 …）。

    看本条款起点（最近句读之后）到命中处的前文 + 片段开头几个字符（勾选符号可能紧贴片段）：
    含 □ 且不含任何已勾选符号 → 该条款在本文件未启用，不产出候选（否则把“未选中的模板条款”当成要求）。"""
    cut = max(preceding.rfind("。"), preceding.rfind("；"), preceding.rfind(";"))
    pre_seg = preceding[cut + 1:] if cut >= 0 else preceding
    matched = matched_head or ""
    if _UNCHECKED_BOX not in pre_seg + matched[:3]:
        return False
    # 片段内部出现已勾选符号（「□不要求…☑要求提交投标保证金…金额」从第一个词起匹配）→ 不是未勾选项
    return not any(b in pre_seg + matched for b in _CHECKED_BOXES)


def _clause_for_hit(preceding: str, line: str, default_clause: str) -> str:
    """条款号：优先取命中前文（本句内）最后一个编号——「3.5 自 2020 年…业绩」的 3.5 在片段之前；
    前文没有再在片段内找；都没有用章节提示。"""
    cut = max(preceding.rfind("。"), preceding.rfind("；"), preceding.rfind(";"))
    seg = preceding[cut + 1:] if cut >= 0 else preceding
    nums = re.findall(r"(?<![\d.])(\d{1,2}(?:\.\d{1,2}){1,3})(?![\d.])", seg)
    if nums:
        return nums[-1]
    return _clause_from_line(line, default_clause)


def _find_in_pages_ctx(pages: list, pattern: re.Pattern, *, ctx: int = 24) -> list[tuple[int, str, str, str]]:
    """同 _find_in_pages，另返回命中前 ctx 个归一字符（勾选框极性判断用）。"""
    hits: list[tuple[int, str, str, str]] = []
    for p in pages:
        joined = _page_fulltext(p)
        raw_chars = [ch for ch in joined if ch not in " \t\r\n\f"]
        norm_text = "".join(raw_chars)
        for m in pattern.finditer(norm_text):
            start, end = m.start(), m.end()
            hits.append((p.page_no, "".join(raw_chars[start:end]), norm_text[start:end],
                         norm_text[max(0, start - ctx):start]))
    return hits


def _clause_from_line(line: str, default_clause: str) -> str:
    """从命中行提取条款号（如 '3.2' / '3.7'）；提取不到用默认章节。

    首段限 1-2 位且前后不接数字/点——「2026.10.10日」「842.002736万元」不再被当成条款号
    （docs/10 附录 A extractor #5）。"""
    m = re.search(r"(?<![\d.])(\d{1,2}(?:\.\d{1,2}){1,3})(?![\d.])", line)
    return m.group(1) if m else default_clause


# ── 锚点模式（以农大房建招标文件为基线；2026-09-10 按唐山三友 EPC 等实测公告拓宽措辞：
#    等级兼容大写数字/甲乙丙、「取得/具有」、「未被…列入」、年份/天数不写死、资质多类型。
#    仍为确定性超集匹配，不推断；版本差异由复核人修正） ─────────────────

ANCHORS: dict[str, dict] = {
    # 资质类：① 「具备…施工总承包/专业承包/专项设计/工程设计综合…X级及以上」；
    #        ② EPC 等按「资质要求：…；」整句列出多项设计+施工资质（无「施工总承包」字样）。
    #        ②中"；"是并列子条件分隔（…资质；有效的安全生产许可证），不切断；仅当"；"后紧跟
    #        下一条款编号（3.1.2）或 label（可隔一个 (n) 编号，如 ；（2）业绩要求：）才收尾
    #        （2026-09-11 河北工大案例，与 announcement_prescreen._group_end 同口径）。
    "qualification_grade": {
        "req_type": "hard_requirement", "category": "资质",
        "pattern": re.compile(
            r"具备.{0,60}?(?:施工总承包|专业承包|专项设计|工程设计综合)(?:资质)?.{0,30}?"
            r"[一二三壹贰叁甲乙丙特]级(?:及以上)?.{0,40}?(不接受.{0,10}?资质预警.{0,10}?资质异常)?"
            r"|资质要求[：:](?:[^。；;]|[；;](?![0-9]{1,3}(?:\.[0-9]{1,3})+"
            r"|(?:[（(][一二三四五六七八九十0-9]{1,3}[)）])?[\u4e00-\u9fff]{2,10}[：:])){5,300}",
            re.S,
        ),
        "evidence": ["qualification_record"],
        "clause_hint": "招标公告 §3.2",
    },
    "safety_license": {
        "req_type": "hard_requirement", "category": "资质",
        "pattern": re.compile(r"具备有效的企业安全生产许可证.{0,30}?|有效的安全生产许可证", re.S),
        "evidence": ["safety_license"],
        "clause_hint": "招标公告 §3.4",
    },
    # 人员类（等级含「特级」，「及以上」可选——「二级注册建造师」无「及以上」亦为硬性要求）
    "pm_registered_builder": {
        "req_type": "hard_requirement", "category": "人员",
        # (?!\d{1,2}\.\d) 不跨下一条款号：避免从章节标题「…施工负责人（建造师）3.7 拟派项目经理…」起匹配
        "pattern": re.compile(r"(?:拟派)?项目经理(?:(?!\d{1,2}\.\d)[^；。]){0,60}?[一二三壹贰叁特]级(?:及以上)?注册建造师执业资格", re.S),
        "evidence": ["manager_profile"],
        "clause_hint": "招标公告 §3.7",
    },
    "pm_b_cert": {
        "req_type": "hard_requirement", "category": "人员",
        "pattern": re.compile(r"同时(?:具有|取得).{0,10}?安全生产考核合格证书(?:（[A-Z]类）)?"),
        "evidence": ["manager_profile"],
        "clause_hint": "招标公告 §3.7",
    },
    "pm_no_active": {
        "req_type": "hard_requirement", "category": "人员",
        # v1.5：「不得以拟派项目经理或施工负责人的身份参加本次投标」（河北大学 EPC 实测）
        "pattern": re.compile(r"不得以(?:拟派)?项目经理(?:或[\u4e00-\u9fff（）()]{2,12})?的身份参加本次投标|未在其他在[施建].{0,12}?担任项目经理"),
        "evidence": ["manager_profile"],
        "clause_hint": "招标公告 §3.8",
    },
    "pm_social_security": {
        "req_type": "hard_requirement", "category": "人员",
        "pattern": re.compile(
            r"任意连续3个月.{0,40}?缴纳社保的证明|连续3个月在本单位缴纳社保"
            r"|连续缴纳近[三3]个月的养老保险缴费证明|连续[三3]个月.{0,20}?(?:社保|养老保险)",
            re.S,
        ),
        "evidence": ["social_security_proof"],
        "clause_hint": "招标公告 §3.9",
    },
    "safety_officer": {
        "req_type": "hard_requirement", "category": "人员",
        # v1.5：「配备人数不少于 1 个」（数量词前可有 不少于/不低于/至少）
        "pattern": re.compile(r"专职安全生产管理人员.{0,60}?配备(?:人数)?(?:不少于|不低于|至少)?[0-9一二三四五六七八九十]+[个名人]", re.S),
        "evidence": ["safety_officer_cert"],
        "clause_hint": "招标公告 §3.10",
    },
    "tech_team": {
        "req_type": "hard_requirement", "category": "人员",
        "pattern": re.compile(r"至少具备建筑工程.{0,10}给排水.{0,10}暖通.{0,10}电气专业的技术人员各一名", re.S),
        "evidence": ["personnel_roster"],
        "clause_hint": "招标公告 §3.13①",
    },
    # 财务类（年度不写死；「近三年…财务审计报告」亦命中）
    "financial_audit": {
        "req_type": "hard_requirement", "category": "财务",
        "pattern": re.compile(
            r"提供20\d\d.{0,80}?年度.{0,20}?财务审计报告|经会计师事务所.{0,20}?出具的财务审计报告"
            r"|近[三3]年.{0,30}?财务审计报告",
            re.S,
        ),
        "evidence": ["financial_audit_report"],
        "clause_hint": "招标公告 §3.13②",
    },
    # 信用类（「未列入」/「未被(法院/平台…)列入」；不跨 ；。 拼接相邻条款）
    "credit_no_loser": {
        "req_type": "hard_requirement", "category": "信用",
        "pattern": re.compile(r"未(?:被[^；;。]{0,60}?)?列入失信被执行人名单(?:.{0,30}?信用中国)?", re.S),
        "evidence": ["credit_check"],
        "clause_hint": "招标公告 §3.11",
    },
    # 联合体：表单式「是否接受联合体投标：否」/ 值式「联合体投标：不接受」/ 勾选式
    # 「☑不接受）联合体投标」/ 叙述式「不接受（接受）联合体投标」四种版式并集；极性由
    # polarity.consortium_match 从命中原文派生写入 rule.accepts_consortium（2026-09-11：
    # 旧锚点只认「不接受」，公告写「接受联合体」时误进 missing 复核队列）。
    "consortium": {
        "req_type": "hard_requirement", "category": "联合体",
        "pattern": CONSORTIUM_ANY,
        "evidence": [],
        "clause_hint": "招标公告 §3.3",
    },
    # 响应性/有效期（天数不写死）
    "bid_validity": {
        "req_type": "hard_requirement", "category": "响应性",
        "pattern": re.compile(r"投标有效期.{0,20}?\d{2,3}日历天", re.S),
        "evidence": ["bid_document"],
        "clause_hint": "投标人须知 §3.3.1",
    },
    # 保证金（农大勾选表式；其他版式退回「投标保证金…人民币X万元」通用式。确定性核验在匹配层按统一单位比对）
    "bid_bond": {
        "req_type": "hard_requirement", "category": "保证金",
        # v1.5：金额支持大写「人民币叁拾万元整，小写300000.00元」与「投标保证金的金额：」版式
        "pattern": re.compile(
            r"要求提交投标保证金.{0,20}?1.金额[：:]?人民币[：:]?([0-9，,.]+万?元).{0,120}?(银行汇票|电汇|支票|银行保函|电子保函|保证保险)"
            r"|投标保证金(?:的)?(?:金额|数额)?.{0,40}?(?:人民币[：:]?(?:[零壹贰叁肆伍陆柒捌玖拾佰仟万亿]{1,12}元(?:整)?|[0-9，,.]+万?元)|小写[0-9，,.]+元)"
            r"|投标保证金.{0,40}?人民币[：:]?[0-9，,.]+万?元",
            re.S,
        ),
        "evidence": ["bid_bond_receipt"],
        "clause_hint": "投标人须知 §3.4.1",
    },
    # 报价限价
    "ceiling_price": {
        "req_type": "hard_requirement", "category": "响应性",
        "pattern": re.compile(r"最高投标限价\s*([0-9，,.]+\s*万元)", re.S),
        "evidence": ["bid_document"],
        "clause_hint": "招标公告 §2.2",
    },
    # 评分类（scored）
    "scoring_tech": {
        "req_type": "scored_requirement", "category": "技术标",
        # v1.5：「技术标（暗标）采用暗标方式编制及评审」/「技术标采取暗标评审」/「…暗标…明标」
        "pattern": re.compile(r"技术标(?:[（(]暗标[)）])?(?:采取|采用|实行).{0,20}?暗标|技术标采取暗标评审|技术标.{0,80}?暗标.{0,20}?(?:明标|评审|评标)", re.S),
        "evidence": [],
        "clause_hint": "评标办法 第三章 四(2)",
    },
    "scoring_similar_performance": {
        "req_type": "scored_requirement", "category": "技术标明标/类似业绩",
        # v1.5：增「(投标人|企业)…类似/同类…业绩…N分」「类似…业绩…得/加/计 N 分」评分写法
        "pattern": re.compile(
            r"投标人.{0,20}?具有.{0,30}?类似.{0,16}?业绩|单体建筑面积.{0,30}?业绩"
            r"|(?:投标人|企业).{0,30}?(?:类似|同类).{0,16}?业绩.{0,40}?[0-9]{1,2}(?:\.[0-9])?分"
            r"|(?:类似|同类).{0,10}?业绩.{0,30}?(?:得|加|计)[0-9]{1,2}(?:\.[0-9])?分"
            # 河北 EPC 评标办法：「除资格审查以外，完成过一项 3000 万元及以上…业绩…得 5 分」/「企业业绩 … 得 N 分」
            r"|除资格审查以外.{0,20}?完成过.{0,120}?业绩.{0,80}?(?:得|加|计)[0-9]{1,2}(?:\.[0-9])?分"
            r"|企业业绩.{0,120}?(?:得|加|计)[0-9]{1,2}(?:\.[0-9])?分", re.S),
        "evidence": ["performance_record"],
        "clause_hint": "评标办法 第三章 四(5)",
    },
    "scoring_business_credit": {
        "req_type": "scored_requirement", "category": "商务标/信用",
        "pattern": re.compile(r"商务标.{0,10}?明标评审|随机平均价为评分基准价|信用.{0,10}?85分", re.S),
        "evidence": [],
        "clause_hint": "评标办法 第三章 四(3)(4)",
    },
    # 动作类（action）：报名「获取/下载…招标文件」；截止时间两种表述 + 「09:00」时刻写法
    "action_file_acquisition": {
        "req_type": "action_requirement", "category": "报名/文件获取",
        "pattern": re.compile(r"凡有意参加投标者.{0,10}?请于(\d{4})年(\d{1,2})月(\d{1,2})日.{0,120}?(?:获取|下载).{0,6}?招标文件", re.S),
        "evidence": [],
        "clause_hint": "招标公告 §4.1",
    },
    "action_deadline_bid": {
        "req_type": "action_requirement", "category": "递交",
        "pattern": re.compile(r"(?:投标文件递交的截止时间|投标截止时间).{0,60}?(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2})[时:：](\d{2})分?", re.S),
        "evidence": [],
        "clause_hint": "招标公告 §5.1",
    },
    "action_bid_bond_due": {
        "req_type": "action_requirement", "category": "保证金到账",
        "pattern": re.compile(r"投标保证金递交时间[：:]?在投标文件递交截止时间之前递交到以下账户，以到账时间为准"),
        "evidence": [],
        "clause_hint": "投标人须知 §3.4.1",
    },
    "action_open": {
        "req_type": "action_requirement", "category": "开标",
        "pattern": re.compile(r"开标时间[：:]?同投标截止时间|开标时间[：:]?\d{4}年", re.S),
        "evidence": [],
        "clause_hint": "投标人须知 §4.1.5",
    },
    # ── 2026-09-14 锚点扩充批次（按招标行业经验覆盖硬性资格 / 评标评分 / 投标动作）──
    # 硬性资格：营业执照与民事责任能力 / 类似业绩（资格审查口径：近 N 年 + 数量词）/ 无重大违法
    # 记录 / 严重违法失信名单 / 纳税与社保证明 / 资格审查方式 / 技术负责人 / 项目经理业绩
    "business_license": {
        "req_type": "hard_requirement", "category": "资质",
        "pattern": re.compile(
            r"(?:具有|具备)?独立(?:承担民事责任|法人)(?:的)?能力|(?:有效|合法)的?(?:企业)?(?:法人)?营业执照", re.S),
        "evidence": ["business_license"],
        "clause_hint": "招标公告 §3.1",
    },
    "similar_performance_hard": {
        "req_type": "hard_requirement", "category": "业绩",
        # 四种版式：① 近N年 + 数量词 + 类似…业绩；② 近N年 + 完成/承建 + 类似…；
        # ③（2026-09-14 v1.5，河北交易中心高频）「自 X 年 X 月 X 日以来完成过一项 3000 万元及以上…业绩」——
        #    不含「类似」「近N年」，起算点是具体日期；④ 近N年（括注区间）+ 完成 + 规模量词 + 业绩
        "pattern": re.compile(
            r"近[0-9一二三四五]年(?:内|以来)?.{0,60}?(?:至少|不少于|[0-9一二三]项(?:及)?以上|以上|[0-9一二三]项).{0,60}?类似.{0,40}?(?:[\u4e00-\u9fff]{0,8}?业绩|工程|项目)"
            r"|近[0-9一二三四五]年(?:内|以来)?.{0,40}?(?:完成|承建|承担|竣工).{0,40}?类似.{0,40}?(?:[\u4e00-\u9fff]{0,8}?业绩|工程|项目)"
            r"|(?:自|从)?\d{4}年\d{1,2}月\d{1,2}日(?:以来|至今|起|以后)(?:[（(][^）)]{0,40}[)）])?.{0,40}?(?:完成|承建|承担|竣工|实施)(?:过|了)?.{0,80}?业绩"
            r"|近[0-9一二三四五]年(?:内|以来|至今)?(?:[（(][^）)]{0,40}[)）])?.{0,40}?(?:完成|承建|承担|竣工|实施)(?:过|了)?.{0,80}?(?:万元|平方米|公里|㎡|m²).{0,40}?业绩",
            re.S),
        "evidence": ["performance_record"],
        "clause_hint": "招标公告 §3.5",
    },
    "no_major_violation": {
        "req_type": "hard_requirement", "category": "信用",
        "pattern": re.compile(
            r"(?:近|前|参加.{0,12}?活动前)[0-9一二三]年(?:内)?.{0,30}?(?:无|没有)(?:重大)?(?:违法|违规)(?:记录|行为)?", re.S),
        "evidence": ["credit_check"],
        "clause_hint": "招标公告 §3.11",
    },
    "credit_blacklist": {
        "req_type": "hard_requirement", "category": "信用",
        "pattern": re.compile(
            r"重大税收违法(?:失信)?(?:案件)?(?:当事人|主体)?名单|政府采购严重违法失信行为(?:记录)?名单"
            r"|(?:建筑市场监管公共服务平台|信用中国).{0,24}?(?:黑名单|失信|不良行为)", re.S),
        "evidence": ["credit_check"],
        "clause_hint": "招标公告 §3.11",
    },
    "tax_social_proof": {
        "req_type": "hard_requirement", "category": "财务",
        "pattern": re.compile(
            r"依法缴纳税收和社会保障资金的?(?:相关)?(?:材料|证明|凭证)|(?:缴纳|纳税)?税收.{0,10}?社会保障资金.{0,10}?(?:证明|凭证|材料)"
            r"|(?:近|最近)[0-9一二三六十二]{1,2}个月.{0,20}?(?:纳税|缴税)(?:证明|凭证)", re.S),
        "evidence": ["tax_social_proof"],
        "clause_hint": "招标公告 §3.13③",
    },
    "prequalification_method": {
        "req_type": "hard_requirement", "category": "资格审查",
        "pattern": re.compile(r"资格审查(?:方式|方法)?[：:]?(?:采用|为)?(?:资格)?(后审|预审)|(?:采用|实行)资格(后审|预审)", re.S),
        "evidence": [],
        "clause_hint": "招标公告 §3.14",
    },
    "tech_lead": {
        "req_type": "hard_requirement", "category": "人员",
        "pattern": re.compile(r"技术负责人.{0,60}?(?:高级|中级|初级)?(?:工程师|职称|技术职务).{0,24}", re.S),
        "evidence": ["personnel_roster"],
        "clause_hint": "招标公告 §3.7",
    },
    "pm_similar_performance": {
        "req_type": "hard_requirement", "category": "人员",
        "pattern": re.compile(r"项目经理.{0,40}?(?:担任|主持|负责|完成).{0,40}?类似.{0,30}?(?:[\u4e00-\u9fff]{0,8}?业绩|工程|项目)", re.S),
        "evidence": ["manager_profile", "performance_record"],
        "clause_hint": "招标公告 §3.7",
    },
    # 评标评分项：评标办法 / 价格分（评标基准价）/ 项目经理评分 / 施工组织设计评分 /
    # 企业荣誉与信用加分 / 拟投入设备评分
    "evaluation_method": {
        "req_type": "scored_requirement", "category": "评标办法",
        "pattern": re.compile(
            r"(?:评标|评审|评定)(?:办法|方法|方式)[：:]?(?:本项目)?(?:采用|为|拟采用)?"
            r"((?:经评审的)?(?:最低投标价法|最低价法|综合评估法|综合评分法|合理低价法|综合评审法|性价比法|最低评标价法|综合评价法))"
            r"|(?:采用|实行)((?:经评审的)?(?:最低投标价法|综合评估法|综合评分法|合理低价法|综合评审法|最低评标价法|综合评价法))",
            re.S),
        "evidence": [],
        "clause_hint": "评标办法 前附表",
    },
    "scoring_price": {
        "req_type": "scored_requirement", "category": "商务标/价格",
        "pattern": re.compile(
            r"(?:评标基准价|价格分|报价得分|投标报价得分|价格部分|报价分).{0,60}?[0-9]{1,3}分"
            r"|(?:价格|报价)(?:分|部分|得分)[：:（(]?(?:满分|权重|占|为|共)?[：:]?[0-9]{1,3}(?:分|%)", re.S),
        "evidence": [],
        "clause_hint": "评标办法 第三章 四(1)",
    },
    "scoring_pm": {
        "req_type": "scored_requirement", "category": "技术标/人员评分",
        "pattern": re.compile(r"项目经理.{0,60}?(?:得|加|计|各得)[0-9]{1,2}(?:\.[0-9])?分", re.S),
        "evidence": ["manager_profile"],
        "clause_hint": "评标办法 第三章 四(6)",
    },
    "scoring_construction_plan": {
        "req_type": "scored_requirement", "category": "技术标/施工组织设计",
        "pattern": re.compile(r"施工组织设计.{0,80}?[0-9]{1,3}分|施工组织设计.{0,30}?(?:评审|评分)(?:标准|因素)", re.S),
        "evidence": [],
        "clause_hint": "评标办法 第三章 四(2)",
    },
    "scoring_enterprise_honor": {
        "req_type": "scored_requirement", "category": "商务标/企业信誉",
        "pattern": re.compile(
            r"(?:鲁班奖|国家优质工程(?:奖)?|省(?:级)?优质工程|市(?:级)?优质工程|安全文明(?:标准化)?(?:示范)?工地|AAA(?:级)?(?:信用|资信)|质量奖|詹天佑奖)"
            r".{0,40}?(?:得|加|计)?[0-9]{1,2}(?:\.[0-9])?分", re.S),
        "evidence": ["honor_certificate"],
        "clause_hint": "评标办法 第三章 四(4)",
    },
    "scoring_equipment": {
        "req_type": "scored_requirement", "category": "技术标/设备",
        "pattern": re.compile(r"(?:拟投入|主要)(?:本工程)?(?:的)?(?:施工)?(?:机械)?设备.{0,60}?[0-9]{1,2}(?:\.[0-9])?分", re.S),
        "evidence": ["equipment_list"],
        "clause_hint": "评标办法 第三章 四(7)",
    },
    # 投标动作：答疑/质疑截止、现场踏勘
    "action_q_and_a": {
        "req_type": "action_requirement", "category": "答疑/质疑",
        "pattern": re.compile(
            r"(?:答疑|质疑|澄清|异议)(?:截止)?(?:时间|期限|日期)[：:]?.{0,25}?\d{4}年\d{1,2}月\d{1,2}日", re.S),
        "evidence": [],
        "clause_hint": "投标人须知 §1.10",
    },
    "action_site_visit": {
        "req_type": "action_requirement", "category": "踏勘",
        "pattern": re.compile(
            r"(?:现场踏勘|踏勘现场|踏勘)(?:时间)?[：:]?.{0,30}?(?:\d{4}年\d{1,2}月\d{1,2}日|不(?:统一)?组织|自行(?:踏勘|前往)|投标人自行)", re.S),
        "evidence": [],
        "clause_hint": "投标人须知 §1.9",
    },
    # ── 追加锚点一律放末尾：候选编号含锚点序号，中间插入会让既有材料重解析时序号漂移撞出重复候选 ──
    # EPC / 工程总承包（2026-09-14 v1.5）：拟派设计负责人 / 施工负责人的执业资格——施工总承包文件没有，
    # 属可选锚点（未命中不产出 missing）；实测文件 §3.8「拟派设计负责人具有国家注册土木工程师（道路工程）
    # 或公用设备工程师（给水排水）执业资格」、§3.9「拟派施工负责人（建造师）具有…壹级及以上注册建造师执业资格」
    "design_lead": {
        "req_type": "hard_requirement", "category": "人员",
        "pattern": re.compile(
            r"(?:拟派)?设计负责人.{0,40}?(?:注册[\u4e00-\u9fff（）()]{2,16}?工程师|注册建筑师|一级注册|执业资格|[高中]级(?:工程师|职称))", re.S),
        "evidence": ["personnel_roster"],
        "clause_hint": "招标公告 §3.8",
    },
    "construction_lead": {
        "req_type": "hard_requirement", "category": "人员",
        "pattern": re.compile(
            r"(?:拟派)?施工负责人(?:[（(]建造师[)）])?(?:(?!\d{1,2}\.\d)[^；。]){0,40}?[一二三壹贰叁特]级(?:及以上)?注册建造师(?:执业资格)?", re.S),
        "evidence": ["personnel_roster"],
        "clause_hint": "招标公告 §3.9",
    },
}

# ── 人工定位检索关键词（F021 §2.1 v1.5）────────────────────────────────
# 锚点未命中时聚合接口不再回传基线文件的条款提示（在别的招标文件里是错误位置），改为给出
# 该锚点对应条款在各类招标文件里的常见措辞，供复核人全文搜索后「人工定位补录」或确认「本文件无此条款」。
LOCATE_HINTS: dict[str, list[str]] = {
    "qualification_grade": ["资质要求", "施工总承包", "专业承包", "工程设计", "级及以上"],
    "safety_license": ["安全生产许可证", "安许"],
    "pm_registered_builder": ["项目经理", "注册建造师", "建造师执业资格"],
    "pm_b_cert": ["安全生产考核合格证", "B 证", "B类"],
    "pm_no_active": ["在施", "在建", "不得同时担任", "无在施项目"],
    "pm_social_security": ["社保", "养老保险", "社会保险缴费"],
    "safety_officer": ["专职安全生产管理人员", "安全员", "C 证"],
    "tech_team": ["技术人员", "各一名", "专业技术人员"],
    "financial_audit": ["财务审计报告", "审计报告", "财务状况", "财务报表"],
    "credit_no_loser": ["失信被执行人", "信用中国", "失信名单"],
    "consortium": ["联合体", "接受联合体", "不接受联合体"],
    "bid_validity": ["投标有效期", "日历天"],
    "bid_bond": ["投标保证金", "保证金金额", "保函"],
    "ceiling_price": ["最高投标限价", "拦标价", "招标控制价"],
    "scoring_tech": ["技术标", "暗标", "技术部分评分"],
    "scoring_similar_performance": ["类似业绩", "类似工程", "业绩得分"],
    "scoring_business_credit": ["商务标", "评分基准价", "信用评分", "信用得分"],
    "action_file_acquisition": ["招标文件的获取", "获取招标文件", "下载招标文件", "报名"],
    "action_deadline_bid": ["投标截止时间", "投标文件递交", "递交截止"],
    "action_bid_bond_due": ["保证金递交", "到账时间", "保证金到账"],
    "action_open": ["开标时间", "开标地点"],
    "business_license": ["营业执照", "独立法人", "民事责任"],
    "similar_performance_hard": ["业绩", "类似工程", "以来完成过", "近三年", "近五年", "万元及以上"],
    "no_major_violation": ["重大违法记录", "违法行为", "无违法"],
    "credit_blacklist": ["严重违法失信", "重大税收违法", "黑名单", "不良行为"],
    "tax_social_proof": ["纳税", "税收", "社会保障资金", "缴税证明"],
    "prequalification_method": ["资格审查方式", "资格后审", "资格预审"],
    "tech_lead": ["技术负责人", "职称"],
    "pm_similar_performance": ["项目经理业绩", "担任项目经理", "主持完成"],
    "evaluation_method": ["评标办法", "综合评估法", "最低投标价法", "经评审的"],
    "scoring_price": ["评标基准价", "价格分", "报价得分", "价格部分"],
    "scoring_pm": ["项目经理", "得分", "加分"],
    "scoring_construction_plan": ["施工组织设计", "技术方案评分"],
    "scoring_enterprise_honor": ["鲁班奖", "优质工程", "安全文明工地", "AAA", "信誉"],
    "scoring_equipment": ["拟投入设备", "主要施工机械", "设备评分"],
    "action_q_and_a": ["答疑", "质疑", "澄清截止", "异议"],
    "action_site_visit": ["现场踏勘", "踏勘时间", "自行踏勘"],
    "design_lead": ["设计负责人", "注册土木工程师", "注册公用设备工程师", "注册结构工程师", "设计负责人执业资格"],
    "construction_lead": ["施工负责人", "建造师", "施工负责人执业资格"],
}

# 这些是招标文件里投标决策必看的**事实条款**（工期、质量标准、合同类型、付款、预付款、履约担保、
# 质保、暂列金、下浮率、安全文明施工费、技术标准、分包），不是三类"要求"（F008 §4.1 硬性/评分/
# 动作），因此不进 ANCHORS/RuleCandidate，而以 MainCardCandidate 形态、kind=term_field 落库
# （parse_service 分组到「投标动作与风险条款」）。value 取首个非空捕获组；无组取整体命中。
_T_AMOUNT = r"(?:[0-9][0-9，,.]{0,12}[0-9]?(?:万元|亿元|万|元)(?:整)?|(?:人民币)?[零壹贰叁肆伍陆柒捌玖拾佰仟万亿]{2,14}元(?:整)?)"
_T_PERCENT = r"[0-9]{1,2}(?:\.[0-9]{1,2})?[%％]"

TERM_ANCHORS: dict[str, dict] = {
    "duration": {
        "label": "工期",
        "pattern": re.compile(r"(?:计划工期|总工期|合同工期|工期)[：:]?(?:为|约|要求)?([0-9]{1,4}(?:日历天|天|个月))", re.S),
    },
    "quality_standard": {
        "label": "质量标准",
        "pattern": re.compile(
            r"(?<!设计)(?:工程)?质量(?:标准|要求|目标)[：:]?(?:达到|符合|满足)?((?:国家|行业|现行)?(?:有关)?(?:验收)?(?:规范|标准)?(?:的)?(?:合格|优良)(?:标准|等级)?)", re.S),
    },
    "contract_type": {
        "label": "合同类型/计价方式",
        "pattern": re.compile(
            r"(?:合同(?:类型|形式|计价(?:方式|模式)?)|计价(?:方式|模式|形式)|承包方式)[：:]?(?:采用|为|实行)?((?:固定|可调)?(?:总价|单价|成本加酬金)(?:合同|包干|承包)?)"
            r"|(?:采用|实行)((?:固定|可调)(?:总价|单价)(?:合同|包干))", re.S),
    },
    "payment_terms": {
        "label": "付款方式",
        "pattern": re.compile(
            r"(?:付款(?:方式|条件)|支付(?:方式|条件)|工程款支付(?:方式)?|进度款支付(?:方式)?|资金支付(?:方式)?)[：:]?([^。；;]{4,160})", re.S),
    },
    "advance_payment": {
        "label": "预付款",
        "pattern": re.compile(r"(?:工程|合同)?预付款[^。；]{0,30}?(" + _T_PERCENT + "|" + _T_AMOUNT + ")", re.S),
    },
    "performance_bond": {
        "label": "履约保证金/担保",
        "pattern": re.compile(
            r"履约(?:保证金|担保|保函)(?:金额|数额|比例)?[^。；]{0,24}?(?:为|按|不(?:得)?超过|不(?:得)?高于)?(?:合同(?:总)?(?:价|金额|价款)的?)?(" + _T_PERCENT + "|" + _T_AMOUNT + ")", re.S),
    },
    "retention_money": {
        "label": "质量保证金",
        "pattern": re.compile(r"(?:质量保证金|质保金|保修金|工程质量保修金)[^。；]{0,30}?(" + _T_PERCENT + "|" + _T_AMOUNT + ")", re.S),
    },
    "warranty": {
        "label": "保修期/缺陷责任期",
        "pattern": re.compile(
            r"(?:工程)?(?:保修期|质保期|质量保修期|缺陷责任期|保修期限)[：:]?(?:为|自[^。；]{0,30}?起)?([0-9]{1,3}(?:个月|年|日历天|天))", re.S),
    },
    "provisional_sum": {
        "label": "暂列金额",
        "pattern": re.compile(r"暂列金额[：:]?(?:为)?(" + _T_AMOUNT + ")", re.S),
    },
    "downward_rate": {
        "label": "下浮率",
        "pattern": re.compile(r"下浮率[：:]?(?:为|不(?:得)?低于|不(?:得)?高于|不(?:得)?超过|按)?(" + _T_PERCENT + ")", re.S),
    },
    "safety_fee": {
        "label": "安全文明施工费",
        "pattern": re.compile(
            r"安全(?:文明)?施工(?:措施)?费[^。；]{0,30}?(不(?:得)?(?:参与|作为|列入)?竞争(?:性)?(?:费用|报价)?|" + _T_AMOUNT + "|" + _T_PERCENT + ")", re.S),
    },
    "tech_standard": {
        "label": "技术标准和要求",
        "pattern": re.compile(
            r"(?:技术(?:标准|要求|规范)(?:和要求|及要求)?|执行(?:的)?(?:技术)?标准|主要技术(?:参数|指标))[：:]([^。；;]{4,160})", re.S),
    },
    "subcontract": {
        "label": "分包",
        "pattern": re.compile(
            r"(?:是否|[(（]是[/／]?否[)）])?(?:允许|接受)分包[：:]?(?:是|否|[01])?|分包[：:](?:不允许|不接受|允许|接受)"
            r"|(?<![)）□○/／否])(?:不允许|不接受|不得|禁止|允许|接受|可以)(?:将[^。；，]{0,30}?)?(?:进行)?分包", re.S),
    },
}

# 类别 → evidence_required（F006/F007 证据类型，与 golden 一致）
CATEGORY_EVIDENCE: dict[str, list[str]] = {
    "资质": ["qualification_record"],
    "人员": ["manager_profile"],
    "财务": ["financial_audit_report"],
    "信用": ["credit_check"],
    "业绩": ["performance_record"],
    "响应性": ["bid_document"],
    "保证金": ["bid_bond_receipt"],
    "技术标明标/类似业绩": ["performance_record"],
}

# 2026-09-14 扩充批次的锚点：并非每份招标文件都有该条款，未命中**不**产出 missing 候选
# （否则复核队列被"本文件本无此要求"的噪音淹没）；基线 21 项的 missing 语义不变。
OPTIONAL_ANCHORS: frozenset[str] = frozenset({
    "business_license", "no_major_violation", "credit_blacklist",
    "tax_social_proof", "prequalification_method", "tech_lead", "pm_similar_performance",
    "evaluation_method", "scoring_price", "scoring_pm", "scoring_construction_plan",
    "scoring_enterprise_honor", "scoring_equipment", "action_q_and_a", "action_site_visit",
    "design_lead", "construction_lead",
})
# similar_performance_hard 2026-09-14 v1.5 移出 OPTIONAL：类似业绩是致命条款，未命中必须以 missing 候选可见，
# 由复核人三选一（本文件无此条款 / 人工定位补录 / 无法确认），不得静默消失


# ── 抽取主逻辑 ────────────────────────────────────────────────────────


def _category_for(key: str) -> str:
    return ANCHORS[key]["category"]


def extract_rule_candidates(
    pages: list,
    *,
    project_id: str,
    material_id: str,
    content_hash: str,
    as_of: str | None = None,
    prefix: str | None = None,
) -> list[RuleCandidate]:
    """从解析页抽取规则候选。

    对每个锚点：在逐页命中第一处（公告/须知优先——按页码小到大，公告章节在前）提取
    assertion 原文；clause_ref = 条款号（从命中行提取）+ 章节 hint；confidence 由锚点
    匹配质量决定；未命中锚点 → 该类别候选标 missing_marker（若属必查 hard 类）。
    返回候选列表（须人工复核确认，F021 §2.7）。

    requirement_id 前缀必须包含材料标识（默认取 material_id）：ParseCandidate.candidate_id
    与 Requirement.requirement_id 均为全局主键，前缀写死会导致不同招标文件产出相同
    候选 ID 而撞库（2026-09-03 MAT-TEST-001 与 MAT-ND-TENDER 同前缀实测复现）。
    """
    id_prefix = prefix or material_id or "MAT"
    candidates: list[RuleCandidate] = []
    seq = 0
    for key, anchor in ANCHORS.items():
        req_type = anchor["req_type"]
        pattern = anchor["pattern"]
        evidence = list(anchor["evidence"]) or list(CATEGORY_EVIDENCE.get(anchor["category"], []))
        # 勾选式条款：「□」未勾选的模板项不算本文件要求（☑/■ 已勾选或无勾选框才计入）
        hits = [(pg, ln, mt, pre) for pg, ln, mt, pre in _find_in_pages_ctx(pages, pattern)
                if key in _POLARITY_ANCHORS or not _unchecked_option(pre, mt)]
        seq += 1
        if not hits:
            # 必查 hard/客观项未命中 → 产出 missing_marker 候选（进人工复核，F005 §4.2.1）；
            # 扩充批次的可选锚点未命中不产出（OPTIONAL_ANCHORS）
            must_report = key not in OPTIONAL_ANCHORS and (
                req_type == "hard_requirement" or key in ("scoring_similar_performance", "scoring_tech"))
            if must_report:
                candidates.append(RuleCandidate(
                    requirement_id=f"{id_prefix}-{req_type_to_code(req_type)}-{seq:03d}-draft",
                    req_type=req_type,
                    category=anchor["category"],
                    clause_ref=anchor["clause_hint"],
                    assertion=MISSING,
                    page_no=None,
                    rule={"type": "missing", "anchor_key": key},
                    evidence_required=evidence,
                    confidence=CONFIDENCE_LOW,
                    missing_marker=True,
                    note="锚点未命中（版式变化/澄清改写？）→ 人工复核补录，禁止推断",
                ))
            continue
        page_no, line, matched, pre = hits[0]
        clause = _clause_for_hit(pre, line, anchor["clause_hint"])
        # assertion = 命中行的原文摘录（可回跳页码）；限长保留完整句
        assertion = (line or matched).strip()
        if len(assertion) > 300:
            assertion = assertion[:300] + "…"
        conf = CONFIDENCE_HIGH if req_type == "hard_requirement" else (
            CONFIDENCE_MEDIUM if req_type == "scored_requirement" else CONFIDENCE_HIGH
        )
        rule: dict = {"type": _rule_type_for(key), "anchor_key": key}
        if key == "consortium":
            # 极性 = f(命中原文)，不由锚点词面决定（「是否接受…：否」→ False；判不出 → None）
            rule["accepts_consortium"] = consortium_match(matched)[0]
        if key == "safety_license":
            # 安许证不分等级，证据种类独立（matching.build_enterprise_evidence 归入 safety_license）；
            # 不带此字段时引擎会拿总承包资质比等级，把有效安许证误判硬性失败（PJ-26a44c8684 实测）
            rule["evidence_type"] = "safety_license"
        candidates.append(RuleCandidate(
            requirement_id=f"{id_prefix}-{req_type_to_code(req_type)}-{seq:03d}-draft",
            req_type=req_type,
            category=anchor["category"],
            clause_ref=(anchor["clause_hint"] if clause == anchor["clause_hint"]
                        else f"{clause}（{anchor['clause_hint']}）"),
            assertion=assertion,
            page_no=page_no,
            rule=rule,
            evidence_required=evidence,
            confidence=conf,
            missing_marker=False,
        ))
    return candidates


def req_type_to_code(req_type: str) -> str:
    return {"hard_requirement": "H", "scored_requirement": "S", "action_requirement": "A"}[req_type]


def _rule_type_for(anchor_key: str) -> str:
    mapping = {
        "qualification_grade": "qualification", "safety_license": "qualification",
        "pm_registered_builder": "project_manager", "pm_b_cert": "project_manager",
        "pm_no_active": "project_manager", "pm_social_security": "social_security",
        "safety_officer": "safety_officer", "tech_team": "tech_team",
        "financial_audit": "financial", "credit_no_loser": "credit",
        "consortium": "consortium", "bid_validity": "bid_validity",
        "bid_bond": "bid_bond", "ceiling_price": "ceiling_price",
        "scoring_tech": "scoring", "scoring_similar_performance": "similar_performance",
        "scoring_business_credit": "scoring", "action_file_acquisition": "action",
        "action_deadline_bid": "action", "action_bid_bond_due": "action",
        "action_open": "action",
        # 2026-09-14 扩充
        # business_license 独立类型（v1.5）：曾映射 qualification → 匹配引擎按资质等级比对，把「有效营业执照」
        # 误判为硬性失败（PJ-V15-E2E 实测）；未实现的类型进 manual_review 才是不推断的正确行为
        "business_license": "business_license", "similar_performance_hard": "similar_performance",
        "no_major_violation": "credit", "credit_blacklist": "credit",
        "tax_social_proof": "financial", "prequalification_method": "prequalification",
        "tech_lead": "tech_lead", "pm_similar_performance": "project_manager",
        "evaluation_method": "evaluation_method", "scoring_price": "scoring",
        "scoring_pm": "scoring", "scoring_construction_plan": "scoring",
        "scoring_enterprise_honor": "scoring", "scoring_equipment": "scoring",
        "action_q_and_a": "action", "action_site_visit": "action",
        # 2026-09-14 v1.5
        "design_lead": "design_lead", "construction_lead": "construction_lead",
    }
    return mapping.get(anchor_key, "generic")


# ── 主卡字段抽取（F005 §4.1 主卡 20 字段） ─────────────────────────────

MAIN_CARD_PATTERNS: dict[str, re.Pattern] = {
    # ① 农大式「…建设项目施工(招标公告)」；② 通用「项目名称：…」（到下一编号小节/句末为止）
    # ① 农大式「…建设项目施工(招标公告)」；② 通用「项目名称：…」；
    # ③（v1.5）公告标题「…工程/项目 + 工程总承包/施工/监理… + 招标公告/招标文件」；④ 招标条件首句「…工程/项目 已由 … 批准」
    "project_name": re.compile(
        r"(?:第[一二三四五六七八九十]+章)?(?:招标公告)?([\u4e00-\u9fff（）()]{4,80}?建设项目施工)(?:招标公告|施工招标)?"
        r"|项目名称[：:]([\u4e00-\u9fff0-9（）()、]{4,120}?)(?=\d\.\d|。|；|招标公告|$)"
        r"|(?:第[一二三四五六七八九十]+章招标公告)?([\u4e00-\u9fff（）()0-9]{4,60}?(?:工程|项目))(?:工程总承包|设计施工总承包|EPC|施工|设计|监理|勘察|总承包)?(?:招标公告|招标文件)"
        r"|([\u4e00-\u9fff（）()0-9]{4,60}?(?:工程|项目))已由"
    ),
    "tender_no": re.compile(r"(?:招标|项目)编号[：:]?\s*([A-Z0-9\-]{6,40})"),
    "tenderee": re.compile(r"(?:招标人|建设单位)[为：:]\s*([\u4e00-\u9fff（）()]{2,40}?)[，,。；]"),
    "agency": re.compile(r"(?:委托)?代理机构[为：:]\s*([\u4e00-\u9fff（）()]{2,40}?)[。，,；]"),
    "region": re.compile(r"建设地点[：:]?\s*((?:[\u4e00-\u9fff]{2,3}省)?[\u4e00-\u9fff]{2,3}?市[\u4e00-\u9fff]{2,10}?[区县])"),
    "ceiling_price": re.compile(r"最高投标限价\s*([0-9，,.]+\s*万元)"),
    "budget_amount": re.compile(r"总投资\s*([0-9，,.]+\s*万元)"),
    "bid_bond_amount": re.compile(r"金额[：:]?人民币[：:]?\s*([0-9，,.]+万?元)"),
    "deadline_signup": re.compile(r"请于\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[时:：](\d{2})分?"),
    "deadline_bid": re.compile(r"投标(?:文件)?(?:递交)?(?:的)?截止时间.{0,30}?(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[时:：](\d{2})分?", re.S),
    "open_date": re.compile(r"开标时间[：:]?\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[时:：](\d{2})分?"),
}
# project_type 不再走正文首命中正则（货物/服务曾被包含性过滤反转为缺失）——P2 改由
# polarity.classify_project_type 在封面/公告标题区按优先级判类（总承包/EPC > 施工 >
# 监理 > 设计 > 勘察 > 货物/服务），见 extract_main_card 尾部。


@dataclass
class MainCardCandidate:
    """主卡字段候选：field_key/value/clause/page/assertion/confidence/missing。"""

    field_key: str
    value: str
    clause: str
    page_no: Optional[int]
    assertion: str
    confidence: str
    missing_marker: bool = False

    def to_dict(self) -> dict:
        return {
            "field_key": self.field_key,
            "value": self.value,
            "clause": self.clause,
            "page_no": self.page_no,
            "assertion": self.assertion,
            "confidence": self.confidence,
            "missing_marker": self.missing_marker,
        }


def extract_main_card(pages: list, *, default_clause: str = "招标公告") -> list[MainCardCandidate]:
    """从公告页抽取主卡字段候选（缺失字段标 __待补__，不推断——F005 §4.1/§6）。

    归一化匹配 + 页码保留；公告章节通常在文件前 12 页（目录+公告），跨页查找。
    返回候选列表；必填主卡字段缺失 → missing_marker（人工复核）。
    """
    out: list[MainCardCandidate] = []
    # 限定公告区：目录后前 12 页正文（农大实测公告在 p4-6；兼容封面/目录偏移）
    scope = [p for p in pages if (p.page_no or 1) <= 12]
    for field_key, pattern in MAIN_CARD_PATTERNS.items():
        hits = _find_in_pages(scope, pattern)
        if not hits:
            continue
        page_no, snippet, norm_hit = hits[0]
        # 时间字段：直接从归一命中文本抽完整时间（跨行归一后无空白）
        if field_key in ("deadline_signup", "deadline_bid", "open_date"):
            dm = re.search(
                r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[时:：](\d{2})分?", norm_hit
            )
            if dm:
                value = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d} {int(dm.group(4)):02d}:{dm.group(5)}"
            else:
                value = norm_hit[:120]
        else:
            m = pattern.search(norm_hit)
            value = next((g for g in m.groups() if g), norm_hit) if m else norm_hit
        # project_name 清理章节前缀（匹配组含 "…施工)招标公告"，value 取项目名部分）
        if field_key == "project_name" and "建设项目施工" in value:
            pm = re.search(r"([\u4e00-\u9fff]{2,60}?宿舍建设项目施工|[\u4e00-\u9fff]{2,60}?建设项目施工)", value)
            if pm:
                value = pm.group(1)
        value = (value or "").strip()
        if len(value) > 200:
            value = value[:200] + "…"
        out.append(MainCardCandidate(
            field_key=field_key,
            value=value,
            clause=default_clause,
            page_no=page_no,
            assertion=snippet[:200],
            confidence=CONFIDENCE_HIGH,
        ))
    # project_type（P2）：封面/公告标题区（前 2 页）按优先级分类器判类，assertion 取
    # 命中词所在段落（原文逐字）；判不出走必填缺失标记，不推断
    cover = [p for p in pages if (p.page_no or 1) <= 2]
    pt = classify_project_type(" ".join(_page_fulltext(p) for p in cover)) if cover else None
    if not pt:
        # 图片封面（无文本）/ 目录占前两页时标题在公告首页（实测 p3）：扩到前 6 页再判一次
        cover = [p for p in pages if (p.page_no or 1) <= 6]
        pt = classify_project_type(" ".join(_page_fulltext(p) for p in cover)) if cover else None
    if pt:
        inner = pt.split("（", 1)[1].rstrip("）")
        keyword = inner[:-2] if inner.endswith(("招标", "采购")) else inner
        hit_page, hit_line = 1, ""
        for p in cover:
            hit_line = next((ln for ln in p.paragraphs if keyword in ln), "")
            if hit_line:
                hit_page = p.page_no
                break
        out.append(MainCardCandidate(
            field_key="project_type", value=pt, clause=default_clause, page_no=hit_page,
            assertion=hit_line[:200], confidence=CONFIDENCE_HIGH))
    # 必填主卡字段缺失 → missing_marker（F005 §4.1 必填：project_name/tenderee/region/
    # project_type/deadline_bid/source_links/confidence/status）
    required = {"project_name", "tenderee", "region", "project_type", "deadline_bid"}
    found = {c.field_key for c in out}
    for req in sorted(required - found):
        out.append(MainCardCandidate(
            field_key=req, value=MISSING, clause=default_clause, page_no=None,
            assertion="", confidence=CONFIDENCE_LOW, missing_marker=True,
        ))
    return out


# ── 合同 / 商务 / 技术条款候选（2026-09-14，kind=term_field） ─────────────────


def _first_group_or_whole(m: re.Match) -> str:
    if not m.lastindex:
        return m.group(0)
    for g in m.groups():
        if g:
            return g
    return m.group(0)


def extract_term_candidates(pages: list, *, default_clause: str = "合同/商务条款") -> list[MainCardCandidate]:
    """从招标文件全文抽取合同/商务/技术条款事实（TERM_ANCHORS），每项首命中一条。

    与主卡/规则候选同为确定性锚点：value = f(命中原文)（首个非空捕获组），assertion 为命中
    页原文片段（去空白匹配、保留页码）；分包极性由 polarity.subcontract_match 从命中原文派生
    写入 value（允许分包/不允许分包），判不出则 value 取原文命中串、confidence=low 转人工。
    未命中的条款**不产出**（本文件本无此条款属正常，不算缺失）。
    """
    from runtime.parsing.polarity import SUBCONTRACT_FALSE_LABEL, SUBCONTRACT_TRUE_LABEL, subcontract_match

    out: list[MainCardCandidate] = []
    for key, spec in TERM_ANCHORS.items():
        hits = _find_in_pages(pages, spec["pattern"])
        if not hits:
            continue
        page_no, snippet, norm_hit = hits[0]
        conf = CONFIDENCE_HIGH
        if key == "subcontract":
            allowed, _ = subcontract_match(norm_hit)
            if allowed is None:
                value, conf = norm_hit, CONFIDENCE_LOW
            else:
                value = SUBCONTRACT_TRUE_LABEL if allowed else SUBCONTRACT_FALSE_LABEL
        else:
            m = spec["pattern"].search(norm_hit)
            value = _first_group_or_whole(m) if m else norm_hit
        value = (value or "").strip()
        if len(value) > 200:
            value = value[:200]
            conf = CONFIDENCE_LOW
        out.append(MainCardCandidate(
            field_key=key, value=value, clause=default_clause, page_no=page_no,
            assertion=snippet[:300], confidence=conf,
        ))
    return out
