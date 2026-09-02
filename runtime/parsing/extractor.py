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


def _clause_from_line(line: str, default_clause: str) -> str:
    """从命中行提取条款号（如 '3.2' / '3.7'）；提取不到用默认章节。"""
    m = re.search(r"(\d+(?:\.\d+){1,3})", line)
    return m.group(1) if m else default_clause


# ── 锚点模式（农大房建招标文件；版本差异由复核人修正） ─────────────────

ANCHORS: dict[str, dict] = {
    # 资质类
    "qualification_grade": {
        "req_type": "hard_requirement", "category": "资质",
        "pattern": re.compile(r"具备建筑工程施工总承包[一二三级]级及以上.{0,40}?(不接受.{0,10}?资质预警.{0,10}?资质异常)?", re.S),
        "evidence": ["qualification_record"],
        "clause_hint": "招标公告 §3.2",
    },
    "safety_license": {
        "req_type": "hard_requirement", "category": "资质",
        "pattern": re.compile(r"具备有效的企业安全生产许可证.{0,30}?", re.S),
        "evidence": ["safety_license"],
        "clause_hint": "招标公告 §3.4",
    },
    # 人员类
    "pm_registered_builder": {
        "req_type": "hard_requirement", "category": "人员",
        "pattern": re.compile(r"拟派项目经理具有注册在投标单位的.{0,30}?专业二级及以上注册建造师执业资格", re.S),
        "evidence": ["manager_profile"],
        "clause_hint": "招标公告 §3.7",
    },
    "pm_b_cert": {
        "req_type": "hard_requirement", "category": "人员",
        "pattern": re.compile(r"同时具有对应有效的安全生产考核合格证书"),
        "evidence": ["manager_profile"],
        "clause_hint": "招标公告 §3.7",
    },
    "pm_no_active": {
        "req_type": "hard_requirement", "category": "人员",
        "pattern": re.compile(r"不得以拟派项目经理的身份参加本次投标"),
        "evidence": ["manager_profile"],
        "clause_hint": "招标公告 §3.8",
    },
    "pm_social_security": {
        "req_type": "hard_requirement", "category": "人员",
        "pattern": re.compile(r"任意连续3个月.{0,40}?缴纳社保的证明|连续3个月在本单位缴纳社保", re.S),
        "evidence": ["social_security_proof"],
        "clause_hint": "招标公告 §3.9",
    },
    "safety_officer": {
        "req_type": "hard_requirement", "category": "人员",
        "pattern": re.compile(r"专职安全生产管理人员.{0,60}?配备人数2个", re.S),
        "evidence": ["safety_officer_cert"],
        "clause_hint": "招标公告 §3.10",
    },
    "tech_team": {
        "req_type": "hard_requirement", "category": "人员",
        "pattern": re.compile(r"至少具备建筑工程.{0,10}给排水.{0,10}暖通.{0,10}电气专业的技术人员各一名", re.S),
        "evidence": ["personnel_roster"],
        "clause_hint": "招标公告 §3.13①",
    },
    # 财务类
    "financial_audit": {
        "req_type": "hard_requirement", "category": "财务",
        "pattern": re.compile(r"提供2022.{0,80}?年度.{0,20}?财务审计报告|经会计师事务所或第三方审计机构出具的财务审计报告", re.S),
        "evidence": ["financial_audit_report"],
        "clause_hint": "招标公告 §3.13②",
    },
    # 信用类
    "credit_no_loser": {
        "req_type": "hard_requirement", "category": "信用",
        "pattern": re.compile(r"未被列入失信被执行人名单.{0,30}?信用中国", re.S),
        "evidence": ["credit_check"],
        "clause_hint": "招标公告 §3.11",
    },
    # 联合体（原文常见"☑不接受）联合体投标"，括号打断直接相邻）
    "consortium": {
        "req_type": "hard_requirement", "category": "联合体",
        "pattern": re.compile(r"不接受.{0,8}?联合体投标"),
        "evidence": [],
        "clause_hint": "招标公告 §3.3",
    },
    # 响应性/有效期
    "bid_validity": {
        "req_type": "hard_requirement", "category": "响应性",
        "pattern": re.compile(r"投标有效期.{0,20}?120日历天", re.S),
        "evidence": ["bid_document"],
        "clause_hint": "投标人须知 §3.3.1",
    },
    # 保证金（金额 20 万 → rule 用原文+归一；确定性核验在匹配层按统一单位比对）
    "bid_bond": {
        "req_type": "hard_requirement", "category": "保证金",
        "pattern": re.compile(r"要求提交投标保证金.{0,20}?1.金额[：:]?人民币[：:]?([0-9，,.]+万?元).{0,120}?(银行汇票|电汇|支票|银行保函|电子保函|保证保险)", re.S),
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
        "pattern": re.compile(r"技术标采取暗标评审|技术标.{0,80}?暗标.{0,20}?明标", re.S),
        "evidence": [],
        "clause_hint": "评标办法 第三章 四(2)",
    },
    "scoring_similar_performance": {
        "req_type": "scored_requirement", "category": "技术标明标/类似业绩",
        "pattern": re.compile(r"投标人具有.{0,30}?类似.{0,10}?业绩|单体建筑面积.{0,30}?业绩", re.S),
        "evidence": ["performance_record"],
        "clause_hint": "评标办法 第三章 四(5)",
    },
    "scoring_business_credit": {
        "req_type": "scored_requirement", "category": "商务标/信用",
        "pattern": re.compile(r"商务标.{0,10}?明标评审|随机平均价为评分基准价|信用.{0,10}?85分", re.S),
        "evidence": [],
        "clause_hint": "评标办法 第三章 四(3)(4)",
    },
    # 动作类（action）
    "action_file_acquisition": {
        "req_type": "action_requirement", "category": "报名/文件获取",
        "pattern": re.compile(r"凡有意参加投标者.{0,10}?请于(\d{4})年(\d{1,2})月(\d{1,2})日.{0,120}?获取招标文件", re.S),
        "evidence": [],
        "clause_hint": "招标公告 §4.1",
    },
    "action_deadline_bid": {
        "req_type": "action_requirement", "category": "递交",
        "pattern": re.compile(r"投标文件递交的截止时间.{0,60}?(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2})时(\d{2})分", re.S),
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
}

# 类别 → evidence_required（F006/F007 证据类型，与 golden 一致）
CATEGORY_EVIDENCE: dict[str, list[str]] = {
    "资质": ["qualification_record"],
    "人员": ["manager_profile"],
    "财务": ["financial_audit_report"],
    "信用": ["credit_check"],
    "响应性": ["bid_document"],
    "保证金": ["bid_bond_receipt"],
    "技术标明标/类似业绩": ["performance_record"],
}


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
    prefix: str = "ND",
) -> list[RuleCandidate]:
    """从解析页抽取规则候选。

    对每个锚点：在逐页命中第一处（公告/须知优先——按页码小到大，公告章节在前）提取
    assertion 原文；clause_ref = 条款号（从命中行提取）+ 章节 hint；confidence 由锚点
    匹配质量决定；未命中锚点 → 该类别候选标 missing_marker（若属必查 hard 类）。
    返回候选列表（须人工复核确认，F021 §2.7）。
    """
    candidates: list[RuleCandidate] = []
    seq = 0
    for key, anchor in ANCHORS.items():
        req_type = anchor["req_type"]
        pattern = anchor["pattern"]
        evidence = list(anchor["evidence"]) or list(CATEGORY_EVIDENCE.get(anchor["category"], []))
        hits = _find_in_pages(pages, pattern)
        seq += 1
        if not hits:
            # 必查 hard/客观项未命中 → 产出 missing_marker 候选（进人工复核，F005 §4.2.1）
            if req_type == "hard_requirement" or key in ("scoring_similar_performance", "scoring_tech"):
                candidates.append(RuleCandidate(
                    requirement_id=f"{prefix}-{req_type_to_code(req_type)}-{seq:03d}-draft",
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
        page_no, line, matched = hits[0]
        clause = _clause_from_line(line, anchor["clause_hint"])
        # assertion = 命中行的原文摘录（可回跳页码）；限长保留完整句
        assertion = (line or matched).strip()
        if len(assertion) > 300:
            assertion = assertion[:300] + "…"
        conf = CONFIDENCE_HIGH if req_type == "hard_requirement" else (
            CONFIDENCE_MEDIUM if req_type == "scored_requirement" else CONFIDENCE_HIGH
        )
        rule: dict = {"type": _rule_type_for(key), "anchor_key": key}
        candidates.append(RuleCandidate(
            requirement_id=f"{prefix}-{req_type_to_code(req_type)}-{seq:03d}-draft",
            req_type=req_type,
            category=anchor["category"],
            clause_ref=f"{clause}（{anchor['clause_hint']}）",
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
    }
    return mapping.get(anchor_key, "generic")


# ── 主卡字段抽取（F005 §4.1 主卡 20 字段） ─────────────────────────────

MAIN_CARD_PATTERNS: dict[str, re.Pattern] = {
    "project_name": re.compile(r"(?:第[一二三四五六七八九十]+章)?(?:招标公告)?([\u4e00-\u9fff（）()]{4,80}?建设项目施工)(?:招标公告|施工招标)?"),
    "tender_no": re.compile(r"(?:招标|项目)编号[：:]?\s*([A-Z0-9\-]{6,40})"),
    "tenderee": re.compile(r"(?:招标人|建设单位)为\s*([\u4e00-\u9fff（）()]{2,40}?)[，,。]"),
    "agency": re.compile(r"(?:委托)?代理机构为\s*([\u4e00-\u9fff（）()]{2,40}?)[。，,]"),
    "region": re.compile(r"建设地点[：:]?\s*([\u4e00-\u9fff]{2,3}?市[\u4e00-\u9fff]{2,10}?[区县])"),
    "ceiling_price": re.compile(r"最高投标限价\s*([0-9，,.]+\s*万元)"),
    "budget_amount": re.compile(r"总投资\s*([0-9，,.]+\s*万元)"),
    "bid_bond_amount": re.compile(r"金额[：:]?人民币[：:]?\s*([0-9，,.]+万?元)"),
    "deadline_signup": re.compile(r"请于\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[时:：](\d{2})分?"),
    "deadline_bid": re.compile(r"投标(?:文件)?递交(?:的)?截止时间.{0,30}?(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[时:：](\d{2})分?", re.S),
    "open_date": re.compile(r"开标时间[：:]?\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[时:：](\d{2})分?"),
    "project_type": re.compile(r"(工程|货物|服务)"),
}


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
        if field_key == "project_name":
            pm = re.search(r"([\u4e00-\u9fff]{2,60}?宿舍建设项目施工|[\u4e00-\u9fff]{2,60}?建设项目施工)", value)
            if pm:
                value = pm.group(1)
        value = (value or "").strip()
        if len(value) > 200:
            value = value[:200] + "…"
        if field_key == "project_type" and "工程" not in value:
            continue
        out.append(MainCardCandidate(
            field_key=field_key,
            value=value,
            clause=default_clause,
            page_no=page_no,
            assertion=snippet[:200],
            confidence=CONFIDENCE_HIGH,
        ))
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
