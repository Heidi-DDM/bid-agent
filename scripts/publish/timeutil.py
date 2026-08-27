#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时区工具：统一北京时间（Asia/Shanghai）。任务书 §4.1 日期口径。

规则：
- 日报日期 D 收录 publish_time ∈ [D 00:00:00, D+1 00:00:00)（北京时间）
- 禁止用 collected_at / created_at / updated_at / 首次发现时间 / 入库时间替代 publish_time
- 所有时间存储带时区（ISO8601），展示时统一转 Asia/Shanghai
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T\s](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?"
    r"(Z|[+-]\d{2}:?\d{2})?$"
)


def now() -> datetime:
    """当前北京时间（带时区）。"""
    return datetime.now(TZ)


def to_shanghai(dt: datetime) -> datetime:
    """任意 aware datetime → Asia/Shanghai。naive 输入按北京时间解释。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def parse_dt(value: str | datetime | None) -> datetime | None:
    """解析时间值 → aware datetime（Asia/Shanghai）。

    支持：
      - "2026-08-10"
      - "2026-08-10 09:30" / "2026-08-10T09:30:00"
      - "2026-08-10T09:30:00+08:00" / "2026-08-10T09:30:00Z"
      - naive datetime（按北京时间解释）/ aware datetime
    解析失败返回 None（调用方按 missing_publish_time 处理，不得猜测）。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_shanghai(value)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=TZ)
    s = str(value).strip()
    if not s:
        return None
    m = _ISO_RE.match(s)
    if not m:
        return None
    y, mo, d = int(m[1]), int(m[2]), int(m[3])
    hh = int(m[4]) if m[4] else 0
    mi = int(m[5]) if m[5] else 0
    ss = int(m[6]) if m[6] else 0
    tz = m[7]
    if tz == "Z":
        dt = datetime(y, mo, d, hh, mi, ss, tzinfo=ZoneInfo("UTC"))
    elif tz:
        sign = 1 if tz[0] == "+" else -1
        tz_body = tz[1:].replace(":", "")
        off = timedelta(hours=int(tz_body[:2]), minutes=int(tz_body[2:4] or 0))
        dt = datetime(y, mo, d, hh, mi, ss, tzinfo=ZoneInfo("UTC")) - off * sign
    else:
        dt = datetime(y, mo, d, hh, mi, ss)
    return to_shanghai(dt)


def day_bounds(d: date) -> tuple[datetime, datetime]:
    """D 日（北京时间）起止：[D 00:00:00, D+1 00:00:00)。"""
    start = datetime.combine(d, time.min, tzinfo=TZ)
    end = start + timedelta(days=1)
    return start, end


def in_day(dt: datetime | None, d: date) -> bool:
    """判断时间是否落在 D 日（北京时间口径）。None → False。"""
    if dt is None:
        return False
    start, end = day_bounds(d)
    t = to_shanghai(dt)
    return start <= t < end


def fmt_dt(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """展示格式（北京时间）。None → 空串。"""
    if dt is None:
        return ""
    return to_shanghai(dt).strftime(fmt)


def iso(dt: datetime | None) -> str | None:
    """存储格式：ISO8601 带 +08:00。None → None。"""
    if dt is None:
        return None
    return to_shanghai(dt).isoformat(timespec="seconds")


def today() -> date:
    """今天（北京时间日期）。"""
    return now().date()
