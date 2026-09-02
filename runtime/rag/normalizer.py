# F025 §6 / 方案 §3.5：结构化字段规范化（纯逻辑，无第三方依赖）
# 金额 Decimal、日期 ISO 8601、中文等级/单位显式映射表。
# 规范化是确定性核验的输入，向量分数/模型置信度不得参与（F025 §6）。
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# 中文等级映射（F008 等级比较 ≥ 语义；含壹贰叁归一化）
LEVEL_ORDER = {
    "特级": 5,
    "一级": 4,
    "壹级": 4,
    "甲级": 4,
    "二级": 3,
    "贰级": 3,
    "乙级": 3,
    "三级": 2,
    "叁级": 2,
    "丙级": 2,
    "四级": 1,
    "四级以下": 0,
    "无": 0,
    "不分级": 0,
}
_LEVEL_RE = re.compile(r"[一二三四五六七八九十壹贰叁肆伍]+级|[特甲乙丙丁]级")


def normalize_level(raw: str | None) -> str | None:
    """提取中文等级并归一化（壹贰叁 → 一二三）；无法识别返回 None。"""
    if not raw:
        return None
    text = raw.strip()
    m = _LEVEL_RE.search(text)
    if not m:
        return None
    level = m.group(0)
    return _TO_ARABIC.get(level, level)


_TO_ARABIC = {
    "壹级": "一级", "贰级": "二级", "叁级": "三级", "肆级": "四级",
    "壹": "一", "贰": "二", "叁": "三", "肆": "四", "伍": "五",
}


def level_rank(level: str | None) -> int | None:
    """等级排名（特级 5 > 一级 4 > ...）；未知返回 None。"""
    if not level:
        return None
    return LEVEL_ORDER.get(level)


def level_ge(left: str | None, right: str | None) -> bool | None:
    """left >= right 语义比较；任一未知返回 None（不可判定，不得推断）。"""
    lr, rr = level_rank(left), level_rank(right)
    if lr is None or rr is None:
        return None
    return lr >= rr


_AMOUNT_RE = re.compile(
    r"(?P<num>[0-9][0-9,]*(?:\.[0-9]{1,4})?)\s*(?P<unit>万元|元|万|亿|元整|圆)?"
)


def normalize_amount(raw: str | None) -> Decimal | None:
    """金额归一化为 Decimal（单位：元）。无法解析返回 None（不得推断）。"""
    if not raw:
        return None
    text = raw.strip().replace("（", "(").replace("）", ")")
    # 中文大写金额（壹佰贰拾叁万...）不做推断，直接返回 None（待人工）
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    try:
        num = Decimal(m.group("num").replace(",", ""))
    except InvalidOperation:
        return None
    unit = m.group("unit") or ""
    if unit in ("万", "万元"):
        num *= Decimal("10000")
    elif unit == "亿":
        num *= Decimal("100000000")
    return num


def normalize_date(raw: str | None) -> date | None:
    """日期归一化为 ISO date；仅接受明确的 年/月/日 组合，无法解析返回 None。"""
    if not raw:
        return None
    text = raw.strip()
    # 2026-09-02 / 2026/09/02 / 2026年9月2日 / 2026.09.02
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%Y年%m月"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    m = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def normalize_unit(raw: str | None) -> str | None:
    """单位归一化（显式映射表）；未知返回原值，不做推断。"""
    if not raw:
        return None
    text = raw.strip()
    mapping = {
        "人民币元": "元", "元整": "元", "圆": "元",
        "平方米": "m²", "㎡": "m²", "m2": "m²",
        "公里": "km", "千米": "km",
        "日历天": "日历天", "个日历天": "日历天",
        "元/平方米": "元/m²", "元每平方米": "元/m²",
    }
    return mapping.get(text, text)