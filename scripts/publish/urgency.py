#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""紧迫程度计算（任务书 §5.2 B）与展示分组（§9.3 section）。

紧迫程度规则（明确规则，不允许模型自由判断）：
  deadline <= now + 24h  → urgent
  deadline <= now + 72h  → high
  其他                    → normal
  字段缺失                → unknown（不得虚构紧迫等级）

判定基准：deadline_signup（报名/文件获取截止）优先；缺失时用 deadline_bid；两者均缺失 → unknown。

展示分组 section（任务书 §5.2 B 五组）：
  urgent   → 报名或文件获取即将截止（紧迫且判定基准为报名截止）
  deadline → 投标即将截止（紧迫且判定基准为投标截止）
  new      → 今日新增公告（招标公告/资格预审）
  change   → 今日变更与澄清（变更/澄清/更正/终止）
  result   → 今日中标及结果信息（中标候选人/中标结果）
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import timeutil as tu

_CHANGE_TYPES = ("变更公告", "澄清公告", "更正公告", "终止公告", "答疑公告")
_RESULT_TYPES = ("中标候选人公示", "中标结果公示", "中标结果公告", "评标结果公示")


def compute_urgency(deadline_signup: str | None, deadline_bid: str | None,
                    now: datetime | None = None,
                    urgent_hours: int = 24, high_hours: int = 72) -> dict:
    """计算紧迫程度。返回 {urgency, basis, deadline}：
      basis: "signup" | "bid" | None（判定基准）
      deadline: 判定的截止时间（ISO，可能 None）
    """
    now = now or tu.now()
    signup = tu.parse_dt(deadline_signup)
    bid = tu.parse_dt(deadline_bid)

    # 判定基准：报名截止优先（§2.2.7 一致性），其次投标截止
    if signup is not None:
        basis, dl = "signup", signup
    elif bid is not None:
        basis, dl = "bid", bid
    else:
        return {"urgency": "unknown", "basis": None, "deadline": None}

    delta = dl - now
    if delta <= timedelta(hours=urgent_hours):
        urgency = "urgent"
    elif delta <= timedelta(hours=high_hours):
        urgency = "high"
    else:
        urgency = "normal"
    return {"urgency": urgency, "basis": basis, "deadline": tu.iso(dl)}


def section_for(announcement_type: str, urgency: dict) -> str:
    """展示分组。紧迫事件优先进 urgent/deadline，其余按公告类型分组。"""
    ann_type = announcement_type or ""
    if urgency.get("urgency") in ("urgent", "high"):
        return "urgent" if urgency.get("basis") == "signup" else "deadline"
    if any(t in ann_type for t in _CHANGE_TYPES):
        return "change"
    if any(t in ann_type for t in _RESULT_TYPES):
        return "result"
    return "new"


SECTION_TITLES = {
    "urgent": "⏰ 报名或文件获取即将截止",
    "deadline": "📅 投标即将截止",
    "new": "🆕 今日新增公告",
    "change": "🔧 今日变更与澄清",
    "result": "🏆 今日中标及结果信息",
}

URGENCY_LABELS = {
    "urgent": "紧急",
    "high": "较急",
    "normal": "常规",
    "unknown": "待核",
}
