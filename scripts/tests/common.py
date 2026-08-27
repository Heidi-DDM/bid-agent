#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布层测试公共设施：临时配置 + 事件构造器（隔离真实 data/ 目录）。"""

from __future__ import annotations

import tempfile
from datetime import date, datetime
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from publish import models as M
from publish import timeutil as tu

PRIORITY = [
    {"level": "法定官方平台", "keywords": ["中国招标投标公共服务平台", "全国公共资源交易平台", "ggzy.gov.cn"]},
    {"level": "招标人或招标代理官方平台", "keywords": ["ebidding.hebtig.com", "招标与采购服务平台"]},
    {"level": "政府公共资源平台", "keywords": ["公共资源交易", "政府采购", "惠招标"]},
    {"level": "权威聚合平台", "keywords": ["千里马", "剑鱼", "标讯"]},
    {"level": "其他转载平台", "keywords": []},
]

VISIBLE = ["project_name", "tender_no", "tenderee", "agency", "region", "project_type",
           "industry", "procurement_method", "announcement_type", "publish_time",
           "deadline_signup", "deadline_bid", "open_date", "budget_amount",
           "ceiling_price", "bid_bond_amount", "qualification", "personnel",
           "joint_venture", "dark_bid", "submission_method", "tender_doc_method",
           "tender_doc_window", "tender_doc_link"]
FORBIDDEN = ["confidence_score", "parser_version", "crawler_task_id", "raw_record_id",
             "fact_gate_status", "intake_id", "G0", "G1", "G2", "G3", "G4", "L0", "L1", "L2", "L3"]


def make_cfg(tmp: Path) -> dict:
    storage = {
        "events_dir": tmp / "events", "issues_dir": tmp / "issues",
        "deliveries_dir": tmp / "deliveries", "source_runs_dir": tmp / "runs",
        "weekly_reports_dir": tmp / "reports", "report_daily_dir": tmp / "daily-reports",
        "report_source_dir": tmp / "source-reports",
    }
    return {
        "timezone": "Asia/Shanghai",
        "daily_issue": {"enabled": True, "publish_time": "18:00", "include_empty_issue": True,
                        "grouping": ["urgent", "deadline", "new", "change", "result"],
                        "title_template": "某建设招标信息日报｜{date}"},
        "delivery": {"late_announcement_policy": "backend_only", "channels": ["web", "wechat"],
                     "urgent_threshold_hours": 24, "high_threshold_hours": 72,
                     "share_message": "某建设招标信息日报｜{date}｜今日新增 {count} 条，紧急 {urgent} 条"},
        "dedup": {"content_hash_length": 16, "primary_source_priority": PRIORITY},
        "source_report": {"enabled": True, "frequency": "weekly", "week_start": "monday",
                          "generation_time": "09:00", "export_formats": ["html", "xlsx", "csv"]},
        "render": {"visible_fields": VISIBLE, "forbidden_fields": FORBIDDEN},
        "storage": storage,
    }


class TmpCtx:
    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pubtest_"))
        return self.tmp

    def __exit__(self, *a):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


def ev(**kw) -> M.AnnouncementEvent:
    """构造 AnnouncementEvent（默认当天发布、当天发现、门禁通过）。"""
    base = dict(
        id="AE-TEST-0001",
        project_id="T-PROJ-TEST-0001",
        announcement_type="招标公告",
        title="测试项目招标公告",
        publish_time=tu.iso(tu.parse_dt("2026-08-17 09:00:00")),
        primary_source_url="https://example.com/notice/1",
        discovered_at=tu.iso(tu.parse_dt("2026-08-17 10:00:00")),
        fact_gate_status="passed",
        delivery_status="eligible",
        project_name="测试项目",
        tenderee="测试招标人有限公司",
        region="河北省·石家庄市",
        deadline_bid="2026-09-10 09:00",
        source_platform="全国公共资源交易平台（河北）",
        created_at=tu.iso(tu.now()),
        updated_at=tu.iso(tu.now()),
        _path="",
    )
    base.update(kw)
    return M.AnnouncementEvent(**base)


def same_day(hh: int = 9, mm: int = 0, d: date = date(2026, 8, 17)) -> str:
    return tu.iso(tu.parse_dt(f"{d.isoformat()} {hh:02d}:{mm:02d}:00"))
