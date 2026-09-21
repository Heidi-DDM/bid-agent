# ADR-004 §2.3 / 优化方案 §12.1：项目身份一致性校验（纯逻辑，无第三方依赖）
#
# 比较「公告侧（expected）」与「招标文件侧（actual）」的项目身份字段，防止公告、招标
# 文件、规则集与匹配结果语义串档（P0-02）。判定原则「多字段加权 + 关键字段硬冲突」：
#   - 编号类（公告/招标/采购编号、标段号）不一致           → 硬冲突
#   - 采购人 / 项目名称 / 地点 显著不一致                    → 高风险冲突
#   - 预算 / 截止时间 / 项目类型 差异                        → 警告（需澄清/延期公告解释）
#   - 无任何可比对字段                                       → 警告（人工核对，不允许直接匹配）
# 只比较双方都明确给出的字段；缺失字段不推断、不视为一致（AGENTS 规则 1）。
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

IDENTITY_CONFIRMED = "identity_confirmed"
IDENTITY_WARNING = "identity_warning"
IDENTITY_CONFLICT = "identity_conflict"
IDENTITY_STATUSES = (IDENTITY_CONFIRMED, IDENTITY_WARNING, IDENTITY_CONFLICT)

SEVERITY_HARD = "hard"          # 编号类不一致
SEVERITY_HIGH = "high"          # 采购人/名称/地点显著不一致
SEVERITY_WARNING = "warning"    # 预算/截止/类型差异

# 编号类字段：任一不一致即硬冲突（优化方案 §12.1 第 1 条）
CODE_FIELDS = ("announcement_no", "tender_no", "procurement_no", "lot_id")
# 名称类字段：按相似度判定
NAME_FIELDS = ("project_name", "purchaser", "location")
# 其余字段：差异只给警告
WARNING_FIELDS = ("project_type", "budget_amount", "bid_deadline")

# 名称相似度阈值（双字二元组 Dice 系数）
NAME_SIMILAR_MIN = 0.60      # ≥ 视为一致
NAME_CONFLICT_MAX = 0.35     # < 视为显著不一致（高风险冲突）；区间内为警告
# 预算相对差异容忍（万元级四舍五入、单位换算误差）
BUDGET_TOLERANCE = Decimal("0.05")

_PUNCT_RE = re.compile(r"[\s\u3000·•・,，。.、;；:：!！?？()（）\[\]【】《》<>〈〉「」『』\"“”'‘’\-—–_/\\|]+")
_CODE_STRIP_RE = re.compile(r"[\s\-—–_/\\.．:：#＃]+")
_NAME_NOISE = ("招标公告", "招标文件", "公开招标", "竞争性磋商", "竞争性谈判", "询价", "采购项目",
               "项目", "工程", "标段", "施工", "公告", "文件")


@dataclass
class IdentityIssue:
    field: str
    severity: str
    expected: Any
    actual: Any
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "severity": self.severity,
            "expected": self.expected,
            "actual": self.actual,
            "note": self.note,
        }


@dataclass
class IdentityResult:
    status: str
    conflicts: list[IdentityIssue] = field(default_factory=list)
    warnings: list[IdentityIssue] = field(default_factory=list)
    compared_fields: list[str] = field(default_factory=list)
    similarity: dict[str, float] = field(default_factory=dict)

    @property
    def blocks_matching(self) -> bool:
        """ADR-004 §2.3 Iteration 0：仅硬/高风险冲突阻断正式匹配与审批。"""
        return self.status == IDENTITY_CONFLICT

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "blocks_matching": self.blocks_matching,
            "conflicts": [c.as_dict() for c in self.conflicts],
            "warnings": [w.as_dict() for w in self.warnings],
            "compared_fields": list(self.compared_fields),
            "similarity": dict(self.similarity),
        }


# ---------- 归一化 ----------

def normalize_text(value: Any) -> str:
    """NFKC 归一 + 去空白/标点 + 小写；空值返回空串。"""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip().lower()
    return _PUNCT_RE.sub("", text)


def normalize_code(value: Any) -> str:
    """编号归一：NFKC + 去分隔符 + 小写（`HBGR-2024085` == `hbgr 2024085`）。"""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip().lower()
    return _CODE_STRIP_RE.sub("", text)


def normalize_project_name(value: Any) -> str:
    """项目名称指纹：归一化后剔除「招标公告/招标文件/项目/工程」等通用噱头词。"""
    text = normalize_text(value)
    for noise in _NAME_NOISE:
        text = text.replace(noise.lower(), "")
    return text


def _bigrams(text: str) -> list[str]:
    if len(text) < 2:
        return [text] if text else []
    return [text[i:i + 2] for i in range(len(text) - 1)]


def name_similarity(a: Any, b: Any) -> float:
    """双字二元组 Dice 系数（0~1），空串对比返回 0。"""
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ga, gb = _bigrams(na), _bigrams(nb)
    if not ga or not gb:
        return 0.0
    from collections import Counter

    ca, cb = Counter(ga), Counter(gb)
    overlap = sum((ca & cb).values())
    return round(2.0 * overlap / (len(ga) + len(gb)), 4)


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        text = unicodedata.normalize("NFKC", str(value))
        text = re.sub(r"[,\s元万亿人民币￥¥]", "", text)
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _day(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    m = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", text)
    if not m:
        return None
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _deadline_clock(value: Any) -> str | None:
    """只抽取明确的时分秒；日期级值返回 None，不伪造时间。"""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value))
    m = re.search(r"(?:T|\s|日)?\s*(\d{1,2})[时:：](\d{2})(?:[分:：](\d{2}))?", text)
    if not m:
        return None
    try:
        return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}:{int(m.group(3) or 0):02d}"
    except ValueError:
        return None

def _both_present(expected: dict, actual: dict, key: str) -> bool:
    return expected.get(key) not in (None, "") and actual.get(key) not in (None, "")


# ---------- 判定 ----------

def compare_identity(expected: dict[str, Any], actual: dict[str, Any]) -> IdentityResult:
    """比较公告侧（expected）与招标文件侧（actual）的项目身份字段。

    仅比较双方都明确给出的字段；返回 IdentityResult（状态 + 冲突/警告明细 + 相似度）。
    """
    expected = expected or {}
    actual = actual or {}
    conflicts: list[IdentityIssue] = []
    warnings: list[IdentityIssue] = []
    compared: list[str] = []
    similarity: dict[str, float] = {}

    # 1) 编号类：硬冲突
    for key in CODE_FIELDS:
        if not _both_present(expected, actual, key):
            continue
        compared.append(key)
        if normalize_code(expected[key]) != normalize_code(actual[key]):
            conflicts.append(IdentityIssue(
                field=key, severity=SEVERITY_HARD,
                expected=expected[key], actual=actual[key],
                note="编号/标段号不一致：疑似不同项目或不同标段材料串档（硬冲突）",
            ))

    # 2) 名称类：相似度
    for key in NAME_FIELDS:
        if not _both_present(expected, actual, key):
            continue
        compared.append(key)
        if key == "project_name":
            sim = name_similarity(normalize_project_name(expected[key]), normalize_project_name(actual[key]))
            # 剔噱头词后若一方为空（名称全是通用词），退回原文相似度
            if not normalize_project_name(expected[key]) or not normalize_project_name(actual[key]):
                sim = name_similarity(expected[key], actual[key])
        else:
            sim = name_similarity(expected[key], actual[key])
        similarity[key] = sim
        if sim >= NAME_SIMILAR_MIN:
            continue
        label = {"project_name": "项目名称", "purchaser": "招标人/采购人", "location": "项目所在地"}[key]
        if sim < NAME_CONFLICT_MAX:
            conflicts.append(IdentityIssue(
                field=key, severity=SEVERITY_HIGH,
                expected=expected[key], actual=actual[key],
                note=f"{label}显著不一致（相似度 {sim:.2f}）：疑似不同项目材料串档（高风险冲突）",
            ))
        else:
            warnings.append(IdentityIssue(
                field=key, severity=SEVERITY_WARNING,
                expected=expected[key], actual=actual[key],
                note=f"{label}部分不一致（相似度 {sim:.2f}），需人工核对是否同一项目",
            ))

    # 3) 预算：相对差异
    if _both_present(expected, actual, "budget_amount"):
        compared.append("budget_amount")
        e, a = _decimal(expected["budget_amount"]), _decimal(actual["budget_amount"])
        if e is None or a is None:
            warnings.append(IdentityIssue(
                field="budget_amount", severity=SEVERITY_WARNING,
                expected=expected["budget_amount"], actual=actual["budget_amount"],
                note="预算/最高限价无法解析为金额，需人工核对",
            ))
        else:
            base = max(abs(e), abs(a))
            if base > 0 and abs(e - a) / base > BUDGET_TOLERANCE:
                warnings.append(IdentityIssue(
                    field="budget_amount", severity=SEVERITY_WARNING,
                    expected=str(e), actual=str(a),
                    note="预算/最高限价差异超过 5%，需依据澄清/补遗解释，否则不得进入正式匹配",
                ))

    # 4) 截止时间：日期不同即警告
    if _both_present(expected, actual, "bid_deadline"):
        compared.append("bid_deadline")
        e, a = _day(expected["bid_deadline"]), _day(actual["bid_deadline"])
        if e is None or a is None:
            warnings.append(IdentityIssue(
                field="bid_deadline", severity=SEVERITY_WARNING,
                expected=expected["bid_deadline"], actual=actual["bid_deadline"],
                note="投标截止时间无法解析为日期，需人工核对",
            ))
        elif e != a:
            warnings.append(IdentityIssue(
                field="bid_deadline", severity=SEVERITY_WARNING,
                expected=e, actual=a,
                note="公告与招标文件截止时间不一致，需依据延期公告解释并以最新版本为准",
            ))
        elif _deadline_clock(expected["bid_deadline"]) != _deadline_clock(actual["bid_deadline"]):
            warnings.append(IdentityIssue(
                field="bid_deadline", severity=SEVERITY_WARNING,
                expected=expected["bid_deadline"], actual=actual["bid_deadline"],
                note="公告与招标文件截止日期相同但时刻/精度不一致，需依据原文或延期公告核对",
            ))

    # 5) 项目类型：不同即警告
    if _both_present(expected, actual, "project_type"):
        compared.append("project_type")
        if normalize_text(expected["project_type"]) != normalize_text(actual["project_type"]):
            warnings.append(IdentityIssue(
                field="project_type", severity=SEVERITY_WARNING,
                expected=expected["project_type"], actual=actual["project_type"],
                note="项目类型不一致，需人工核对",
            ))

    if conflicts:
        status = IDENTITY_CONFLICT
    elif warnings:
        status = IDENTITY_WARNING
    elif compared:
        status = IDENTITY_CONFIRMED
    else:
        status = IDENTITY_WARNING
        warnings.append(IdentityIssue(
            field="*", severity=SEVERITY_WARNING, expected=None, actual=None,
            note="公告侧与招标文件侧无任何可比对字段（均缺失），无法确认同一项目，需人工核对后方可匹配",
        ))
    return IdentityResult(
        status=status, conflicts=conflicts, warnings=warnings,
        compared_fields=compared, similarity=similarity,
    )
