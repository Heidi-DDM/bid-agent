#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布层数据结构（任务书 §九 六类对象）。

落地方式：YAML frontmatter + Markdown body 文件（与项目现有 OKF 风格一致，不引入数据库）。
文件命名：
  announcement_event  → data/announcement_events/AE-<YYYYMMDD>-<seq>.md
  daily_issue         → data/daily_issues/<YYYY-MM-DD>.md（items 内嵌于 frontmatter，见 DAILY_ISSUE_ITEM 注释）
  delivery_record     → data/delivery_records/DEL-<issue_date>-<channel>.md
  source_run          → data/source_runs/RUN-<YYYYMMDDHHMMSS>-<seq>.md
  weekly_source_report→ data/weekly_source_reports/<YYYY>-W<xx>.md

唯一约束：
  daily_issue_item: UNIQUE(daily_issue_id, announcement_event_id)
  → 由 generate_daily_issue 强制（同一事件不可在同一日报重复出现）；读取时校验。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import yaml

from . import timeutil as tu


# ---------- frontmatter 读写 ----------

def read_doc(path: Path) -> tuple[dict, str]:
    """读取 YAML frontmatter + body。非 frontmatter 文件 → 视为空 dict。"""
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm = yaml.safe_load(parts[1]) or {}
    body = parts[2].lstrip("\n")
    return fm, body


def write_doc(path: Path, frontmatter: dict, body: str = "") -> None:
    """写 YAML frontmatter + body。目录自动创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).rstrip(), "---", ""]
    if body:
        lines.append(body.rstrip() + "\n")
    path.write_text("\n".join(lines), encoding="utf-8")


def load_all(dir_path: Path, cls: type) -> list:
    """加载目录下某类型全部记录（按 type 过滤，容忍其他文件）。"""
    out = []
    if not dir_path.exists():
        return out
    for p in sorted(dir_path.glob("*.md")):
        fm, _ = read_doc(p)
        if fm.get("type") == cls.TYPE:
            out.append(cls.from_dict(fm, path=p))
    return out


def save(obj) -> Path:
    """保存单条记录，返回路径。_path 由对象携带但不落盘。"""
    fm = obj.to_dict()
    p = fm.pop("_path", "") or ""
    if not p:
        raise ValueError(f"{obj.id} 缺少 _path（由工厂函数生成）")
    body = obj.body if hasattr(obj, "body") else ""
    write_doc(Path(p), fm, body)
    return Path(p)


# ---------- 四类对象（§9.1-9.4） ----------

@dataclass
class AnnouncementEvent:
    """公告事件（§9.1）。日报的原料单位：项目在某个时间发生的一次公告事件。"""
    TYPE = "announcement_event"

    id: str
    project_id: str
    announcement_type: str
    title: str
    publish_time: str | None            # ISO8601 带时区；进入日报必须非空
    timezone: str = "Asia/Shanghai"
    primary_source_id: str = ""
    primary_source_url: str = ""
    source_links: list = field(default_factory=list)   # [{source_id, source_name, url, publish_time}]
    content_hash: str = ""
    dedup_key: str = ""
    content_hash_source: str | None = None   # notice_fulltext_snapshot | raw_intake_extract | None
    discovered_at: str = ""
    fact_gate_status: str = "pending"   # passed | pending | rejected
    delivery_status: str = "eligible"   # eligible | delivered | pending_review | late_discovered | excluded
    exclusion_reason: str | None = None
    related_event_ids: list = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    # §4.6 迟到公告附加字段
    late_discovered_at: str | None = None
    original_publish_time: str | None = None
    # 展示辅助（业务字段，来自上游主卡/事实卡）
    project_name: str = ""
    tenderee: str = ""
    region: str = ""
    deadline_signup: str | None = None
    deadline_bid: str | None = None
    budget_amount: str | None = None
    ceiling_price: str | None = None
    bid_bond_amount: str | None = None
    tender_doc_method: str | None = None
    tender_doc_window: str | None = None
    tender_doc_link: str | None = None
    detail_fields: dict = field(default_factory=dict)   # 详情页完整事实字段
    source_platform: str = ""
    confidence: str = ""             # 骨架树 §3.3：confirmed/high/medium/low（必填；官方=confirmed）
    _path: str = ""

    @classmethod
    def from_dict(cls, d: dict, path: Path | None = None) -> "AnnouncementEvent":
        obj = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        obj._path = str(path) if path else d.get("_path", "")
        return obj

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.TYPE
        return d


@dataclass
class DailyIssueItem:
    """日报条目（§9.3）。内嵌于 DailyIssue.items，唯一约束 UNIQUE(daily_issue_id, announcement_event_id)。"""
    id: str
    daily_issue_id: str
    announcement_event_id: str
    section: str = "new"                # urgent | deadline | new | change | result
    urgency: str = "unknown"            # urgent | high | normal | unknown
    display_order: int = 0
    included_at: str = ""
    inclusion_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DailyIssue:
    """日报（§9.2）。一自然日一份。"""
    TYPE = "daily_issue"

    id: str
    issue_date: str                     # YYYY-MM-DD
    timezone: str = "Asia/Shanghai"
    title: str = ""
    status: str = "draft"               # draft | published | published_empty | revised | failed
    item_count: int = 0
    urgent_count: int = 0
    version: int = 1
    generated_at: str = ""
    published_at: str | None = None
    last_updated_at: str = ""
    stable_url: str = ""                # /daily/2026-08-17（逻辑地址）
    archive_path: str = ""              # daily/2026-08-17/index.html（物理路径）
    items: list = field(default_factory=list)   # list[DailyIssueItem]（内嵌实现，见 §9.3）
    late_items: list = field(default_factory=list)   # 迟到补录区条目（policy=supplement_section 时非空）
    pending_review_items: list = field(default_factory=list)  # 复核队列事件 id 列表
    revisions: list = field(default_factory=list)    # 版本修订记录 [{version, at, reason, actor, changes}]
    _path: str = ""

    @classmethod
    def from_dict(cls, d: dict, path: Path | None = None) -> "DailyIssue":
        obj = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__ and k not in ("items", "late_items", "pending_review_items", "revisions")})
        obj.items = [DailyIssueItem(**it) for it in d.get("items", [])]
        obj.late_items = [DailyIssueItem(**it) for it in d.get("late_items", [])]
        obj.pending_review_items = list(d.get("pending_review_items", []))
        obj.revisions = list(d.get("revisions", []))
        obj._path = str(path) if path else d.get("_path", "")
        return obj

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.TYPE
        return d


@dataclass
class DeliveryRecord:
    """投递记录（§9.4）。"""
    TYPE = "delivery_record"

    id: str
    daily_issue_id: str
    channel: str = "web"                # wechat | feishu | email | web
    target: str = ""
    status: str = "pending"             # pending | sent | failed | retrying
    sent_at: str | None = None
    retry_count: int = 0
    rendered_url: str = ""
    error_summary: str | None = None
    created_at: str = ""
    _path: str = ""

    @classmethod
    def from_dict(cls, d: dict, path: Path | None = None) -> "DeliveryRecord":
        obj = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        obj._path = str(path) if path else d.get("_path", "")
        return obj

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.TYPE
        return d


# ---------- 审计层两类对象（§7.2 / §9.6） ----------

@dataclass
class SourceRun:
    """单次平台巡检运行记录（§7.2）。只进后台审计，不进客户日报。"""
    TYPE = "source_run"

    id: str
    source_id: str
    source_name: str
    started_at: str
    finished_at: str
    status: str = "success"             # success | partial | failed
    pages_scanned: int = 0
    raw_items_found: int = 0
    valid_events_found: int = 0
    duplicate_items: int = 0
    rejected_items: int = 0
    late_items: int = 0
    error_category: str | None = None
    error_summary: str | None = None
    retry_count: int = 0
    last_success_at: str | None = None
    task_version: str = ""
    created_at: str = ""
    _path: str = ""

    @classmethod
    def from_dict(cls, d: dict, path: Path | None = None) -> "SourceRun":
        obj = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        obj._path = str(path) if path else d.get("_path", "")
        return obj

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.TYPE
        return d


@dataclass
class WeeklySourceReport:
    """来源与覆盖周报（§9.6）。"""
    TYPE = "weekly_source_report"

    id: str
    period_start: str
    period_end: str
    timezone: str = "Asia/Shanghai"
    status: str = "draft"               # draft | published | revised
    version: int = 1
    source_count: int = 0
    successful_source_count: int = 0
    partial_source_count: int = 0
    failed_source_count: int = 0
    raw_item_count: int = 0
    deduplicated_event_count: int = 0
    delivered_event_count: int = 0
    late_event_count: int = 0
    pending_review_count: int = 0
    report_url: str = ""
    export_path: str = ""
    generated_at: str = ""
    published_at: str | None = None
    funnel: dict = field(default_factory=dict)      # 信息漏斗明细
    source_stats: list = field(default_factory=list)  # 每平台统计
    daily_links: list = field(default_factory=list)   # 周期内日报链接
    lineage: list = field(default_factory=list)       # 信息血缘明细（§8.3）
    reconciliation: dict = field(default_factory=dict)  # 数量对账结果
    _path: str = ""

    @classmethod
    def from_dict(cls, d: dict, path: Path | None = None) -> "WeeklySourceReport":
        obj = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        obj._path = str(path) if path else d.get("_path", "")
        return obj

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.TYPE
        return d


# ---------- 工厂函数 ----------

def _now_iso() -> str:
    return tu.iso(tu.now()) or ""


def new_event_id(d: datetime) -> str:
    return f"AE-{d.strftime('%Y%m%d')}-{tu.to_shanghai(d).strftime('%H%M%S')}"


def event_path(events_dir: Path, event_id: str) -> Path:
    return events_dir / f"{event_id}.md"


def daily_issue_path(issues_dir: Path, issue_date: str) -> Path:
    return issues_dir / f"{issue_date}.md"


def delivery_path(deliveries_dir: Path, issue_date: str, channel: str) -> Path:
    return deliveries_dir / f"DEL-{issue_date}-{channel}.md"


def source_run_path(runs_dir: Path, run_id: str) -> Path:
    return runs_dir / f"{run_id}.md"


def weekly_report_path(reports_dir: Path, period_key: str) -> Path:
    return reports_dir / f"{period_key}.md"


def unique(items: list[DailyIssueItem]) -> list[DailyIssueItem]:
    """强制 UNIQUE(daily_issue_id, announcement_event_id)。重复时报错（防呆）。"""
    seen = set()
    out = []
    for it in items:
        key = (it.daily_issue_id, it.announcement_event_id)
        if key in seen:
            raise ValueError(f"唯一约束冲突 UNIQUE(daily_issue_id, announcement_event_id): {key}")
        seen.add(key)
        out.append(it)
    return out
