#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""来源与覆盖周报生成器（任务书 §八 / §10.2）。

默认周期：每周一汇总上一自然周（周一 00:00 至周日 23:59:59，北京时间）——配置化。
核心能力：
- 聚合 SourceRun → 巡检成功率、每平台最后成功时间（§8.3）
- 聚合 DailyIssue/DailyIssueItem → 信息漏斗（原始发现→去重→进日报→迟到→复核）
- 信息血缘明细（公告名称/类型/来源平台/原始发布时间/采集时间/是否跨平台重复/所属日报/链接/状态）
- 数量对账：周报"进入日报数量" == 该周所有日报去重后公告事件数；差异单独列出原因
- 输出：HTML + XLSX + CSV（§8.4）
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import models as M
from . import timeutil as tu
from .config import ROOT, load_config, storage_paths
from . import renderer

MONTH_CN = "一二三四五六七八九十"


def _esc(v) -> str:
    import html
    return html.escape(str(v)) if v not in (None, "") else ""


def _prev_week(d: date, week_start: str = "monday") -> tuple[date, date]:
    """d 所在周的上一自然周 [start, end]。week_start=monday。"""
    wd = d.weekday()  # 0=周一
    this_monday = d - timedelta(days=wd)
    start = this_monday - timedelta(days=7)
    return start, start + timedelta(days=6)


def _week_key(start: date) -> str:
    iso = start.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def period_key_to_dates(key: str) -> tuple[date, date]:
    """'2026-W34' → (周一日期, 周日日期)。"""
    year_s, week_s = key.split("-W")
    year, week = int(year_s), int(week_s)
    # ISO 周：周四所在周
    jan4 = date(year, 1, 4)
    monday = jan4 - timedelta(days=jan4.weekday()) + timedelta(weeks=week - 1)
    return monday, monday + timedelta(days=6)


def build_weekly_report(period_start: date | None = None, period_end: date | None = None,
                        cfg: dict | None = None, force: bool = False) -> M.WeeklySourceReport:
    """生成周报记录（不落盘展示产物，展示由 publish_weekly_report 完成）。"""
    cfg = cfg or load_config()
    paths = storage_paths(cfg)
    if period_start is None or period_end is None:
        period_start, period_end = _prev_week(tu.today(), cfg["source_report"].get("week_start", "monday"))
    start_dt = tu.to_shanghai(datetime.combine(period_start, datetime.min.time()))
    end_dt = tu.to_shanghai(datetime.combine(period_end + timedelta(days=1), datetime.min.time()))

    # 1. 聚合 SourceRun（started_at ∈ 周期）
    runs = [r for r in M.load_all(paths["source_runs_dir"], M.SourceRun)
            if _in_period(r.started_at, start_dt, end_dt)]
    # 2. 聚合 DailyIssue（issue_date ∈ 周期）
    issues = [i for i in M.load_all(paths["issues_dir"], M.DailyIssue)
              if period_start <= tu.parse_dt(i.issue_date).date() <= period_end]
    # 3. 聚合 AnnouncementEvent（用于血缘明细）
    events = M.load_all(paths["events_dir"], M.AnnouncementEvent)
    ev_map = {e.id: e for e in events}

    # 4. 平台统计
    src_stats = _source_stats(runs)
    # 5. 信息漏斗
    funnel = _funnel(runs, issues)
    # 6. 血缘明细
    lineage = _lineage(issues, ev_map, start_dt, end_dt)
    # 7. 对账
    recon = _reconcile(issues, lineage)

    period_key = _week_key(period_start)
    report = M.WeeklySourceReport(
        id=f"WSR-{period_key}",
        period_start=tu.iso(start_dt) or "",
        period_end=tu.iso(end_dt - timedelta(microseconds=1)) or "",
        status="published",
        version=1,
        source_count=len(src_stats),
        successful_source_count=sum(1 for s in src_stats if s["status"] == "success"),
        partial_source_count=sum(1 for s in src_stats if s["status"] == "partial"),
        failed_source_count=sum(1 for s in src_stats if s["status"] == "failed"),
        raw_item_count=funnel["raw_items"],
        deduplicated_event_count=funnel["deduplicated_events"],
        delivered_event_count=funnel["delivered_events"],
        late_event_count=funnel["late_events"],
        pending_review_count=funnel["pending_review"],
        generated_at=tu.iso(tu.now()) or "",
        published_at=tu.iso(tu.now()) or "",
        funnel=funnel,
        source_stats=src_stats,
        daily_links=[{"date": i.issue_date, "count": i.item_count, "url": i.stable_url} for i in issues],
        lineage=lineage,
        reconciliation=recon,
    )
    report._path = str(M.weekly_report_path(paths["weekly_reports_dir"], period_key))
    M.save(report)
    return report


def _in_period(iso_ts: str, start_dt, end_dt) -> bool:
    dt = tu.parse_dt(iso_ts)
    return dt is not None and start_dt <= dt < end_dt


def _source_stats(runs: list[M.SourceRun]) -> list[dict]:
    """每平台聚合：总次数/成功/部分/失败/最后成功时间。"""
    by_src: dict[str, dict] = {}
    for r in runs:
        s = by_src.setdefault(r.source_id, {
            "source_id": r.source_id, "source_name": r.source_name,
            "runs": 0, "success": 0, "partial": 0, "failed": 0,
            "raw_items": 0, "valid_events": 0, "duplicates": 0, "rejected": 0, "late": 0,
            "last_success_at": None, "last_run_at": None,
        })
        s["runs"] += 1
        s[r.status] += 1
        s["raw_items"] += r.raw_items_found
        s["valid_events"] += r.valid_events_found
        s["duplicates"] += r.duplicate_items
        s["rejected"] += r.rejected_items
        s["late"] += r.late_items
        if r.status == "success" and (s["last_success_at"] is None or (r.finished_at or "") > s["last_success_at"]):
            s["last_success_at"] = r.finished_at
        if r.last_success_at and (s["last_success_at"] is None or r.last_success_at > s["last_success_at"]):
            s["last_success_at"] = r.last_success_at
        if s["last_run_at"] is None or (r.finished_at or "") > s["last_run_at"]:
            s["last_run_at"] = r.finished_at
    for s in by_src.values():
        if s["failed"] and not s["success"] and not s["partial"]:
            s["status"] = "failed"
        elif s["success"] and not s["failed"] and not s["partial"]:
            s["status"] = "success"
        else:
            s["status"] = "partial"
    return list(by_src.values())


def _funnel(runs: list[M.SourceRun], issues: list[M.DailyIssue]) -> dict:
    """信息漏斗（§8.3）：原始发现 → 有效事件 → 去重后 → 进日报 → 迟到 → 复核。"""
    raw = sum(r.raw_items_found for r in runs)
    valid = sum(r.valid_events_found for r in runs)
    dedup = sum(r.valid_events_found - r.duplicate_items for r in runs)
    delivered = sum(i.item_count for i in issues)
    late = sum(r.late_items for r in runs)
    pending = sum(len(i.pending_review_items) for i in issues)
    excluded = max(0, valid - dedup - late)
    return {
        "raw_items": raw,
        "valid_events": valid,
        "deduplicated_events": dedup,
        "delivered_events": delivered,
        "late_events": late,
        "pending_review": pending,
        "excluded_others": excluded,
        "success_rate": _success_rate(runs),
        "run_count": len(runs),
    }


def _success_rate(runs: list[M.SourceRun]) -> float:
    if not runs:
        return 0.0
    ok = sum(1 for r in runs if r.status == "success")
    return round(ok / len(runs) * 100, 1)


def _lineage(issues: list[M.DailyIssue], ev_map: dict, start_dt, end_dt) -> list[dict]:
    """信息血缘明细（§8.3）：公告 ↔ 来源平台 ↔ 采集时间 ↔ 所属日报 ↔ 原文链接。"""
    rows = []
    for issue in issues:
        for it in issue.items:
            e = ev_map.get(it.announcement_event_id)
            if e is None:
                continue
            pt = tu.parse_dt(e.publish_time)
            rows.append({
                "event_id": e.id,
                "title": e.title or e.project_name,
                "announcement_type": e.announcement_type,
                "source_platform": e.source_platform or (e.source_links[0].get("source_name") if e.source_links else ""),
                "publish_time": tu.fmt_dt(pt, "%Y-%m-%d %H:%M") if pt else "",
                "discovered_at": tu.fmt_dt(tu.parse_dt(e.discovered_at), "%Y-%m-%d %H:%M"),
                "cross_platform_dup": "是" if e.source_links else "否",
                "issue_date": issue.issue_date,
                "issue_url": issue.stable_url,
                "source_url": e.primary_source_url,
                "status": it.inclusion_reason or e.delivery_status,
            })
    # 补充周期内发现的迟到/复核事件（未进日报的）
    for e in sorted(ev_map.values(), key=lambda x: x.discovered_at or ""):
        dt = tu.parse_dt(e.discovered_at)
        if dt is None or not (start_dt <= dt < end_dt):
            continue
        if any(r["event_id"] == e.id for r in rows):
            continue
        if e.delivery_status in ("late_discovered", "pending_review", "excluded"):
            pt = tu.parse_dt(e.publish_time)
            rows.append({
                "event_id": e.id,
                "title": e.title or e.project_name,
                "announcement_type": e.announcement_type,
                "source_platform": e.source_platform or "",
                "publish_time": tu.fmt_dt(pt, "%Y-%m-%d %H:%M") if pt else "",
                "discovered_at": tu.fmt_dt(dt, "%Y-%m-%d %H:%M"),
                "cross_platform_dup": "是" if e.source_links else "否",
                "issue_date": "",
                "issue_url": "",
                "source_url": e.primary_source_url,
                "status": e.delivery_status + (f"（{e.exclusion_reason}）" if e.exclusion_reason else ""),
            })
    return rows


def _reconcile(issues: list[M.DailyIssue], lineage: list[dict]) -> dict:
    """数量对账：周报 delivered == 该周日报去重后事件数。差异列原因。"""
    total_items = sum(i.item_count for i in issues)
    in_issue = sum(1 for r in lineage if r["issue_date"])
    late_supplement = sum(1 for r in lineage if r["issue_date"] and "迟到补录" in r["status"])
    diffs = []
    if total_items != in_issue:
        diffs.append(f"日报 item_count 合计 {total_items} 与血缘明细去重事件数 {in_issue} 不一致")
    return {
        "delivered_total": in_issue,
        "daily_item_sum": total_items,
        "late_supplement_in_issue": late_supplement,
        "balanced": total_items == in_issue,
        "diff_reasons": diffs or ["无（数量一致）"],
    }


def publish_weekly_report(report: M.WeeklySourceReport, cfg: dict | None = None) -> dict:
    """落盘周报产物：Markdown 报告 + XLSX + CSV → 报告/来源与覆盖/<period>/（不再生成 HTML）。"""
    cfg = cfg or load_config()
    site_dir = ROOT / cfg["storage"].get("report_source_dir", "报告/来源与覆盖")
    period_key = report.id.replace("WSR-", "")
    out_dir = site_dir / period_key.replace("-W", "/W")  # 2026/W34
    out_dir.mkdir(parents=True, exist_ok=True)

    md_path = out_dir / "来源与覆盖周报.md"
    md_path.write_text(renderer.render_weekly_report_md(report, cfg), encoding="utf-8")

    csv_path = out_dir / "来源与覆盖周报.csv"
    export_lineage_csv(report, csv_path)

    xlsx_path = out_dir / "来源与覆盖周报.xlsx"
    export_lineage_xlsx(report, xlsx_path)

    report.report_url = f"报告/来源与覆盖/{period_key.replace('-W', '/W')}/来源与覆盖周报.md"
    try:
        rel = out_dir.relative_to(ROOT)
        report.export_path = f"{rel}/来源与覆盖周报.xlsx"
    except ValueError:  # 测试用临时目录时退化为绝对路径
        report.export_path = str(xlsx_path)
    M.save(report)
    return {"report": md_path, "xlsx": xlsx_path, "csv": csv_path}


def export_lineage_csv(report: M.WeeklySourceReport, path: Path) -> None:
    """血缘明细 CSV（UTF-8 BOM，Excel 直接打开）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["报告周期", report.period_start[:10], "至", report.period_end[:10]])
        w.writerow([])
        w.writerow(["公告名称", "公告类型", "来源平台", "原始发布时间", "系统采集时间",
                    "是否跨平台重复", "所属日报日期", "日报链接", "原文链接", "当前处理状态"])
        for r in report.lineage:
            w.writerow([r["title"], r["announcement_type"], r["source_platform"],
                        r["publish_time"], r["discovered_at"], r["cross_platform_dup"],
                        r["issue_date"], r["issue_url"], r["source_url"], r["status"]])


def export_lineage_xlsx(report: M.WeeklySourceReport, path: Path) -> None:
    """血缘明细 XLSX（openpyxl）：sheet1 概览 + sheet2 明细。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "巡检覆盖"
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="1A56A8")
    ws.append(["报告周期", f"{report.period_start[:10]} 至 {report.period_end[:10]}", "时区", "Asia/Shanghai"])
    ws.append([])
    ws.append(["指标", "数值"])
    for k, v in report.funnel.items():
        ws.append([k, v])
    ws.append([])
    ws.append(["平台", "巡检次数", "成功", "部分成功", "失败", "原始发现", "有效事件", "去重", "迟到", "最后成功巡检"])
    for s in report.source_stats:
        ws.append([s["source_name"], s["runs"], s["success"], s["partial"], s["failed"],
                   s["raw_items"], s["valid_events"], s["duplicates"], s["late"],
                   tu.fmt_dt(tu.parse_dt(s["last_success_at"])) if s["last_success_at"] else ""])

    ws2 = wb.create_sheet("信息血缘明细")
    ws2.append(["公告名称", "公告类型", "来源平台", "原始发布时间", "系统采集时间",
                "是否跨平台重复", "所属日报日期", "日报链接", "原文链接", "当前处理状态"])
    for r in report.lineage:
        ws2.append([r["title"], r["announcement_type"], r["source_platform"],
                    r["publish_time"], r["discovered_at"], r["cross_platform_dup"],
                    r["issue_date"], r["issue_url"], r["source_url"], r["status"]])
    for row in ws2.iter_rows(min_row=1, max_row=1):
        for c in row:
            c.font = head_font
            c.fill = head_fill
    wb.save(path)


def update_report_index(cfg: dict | None = None) -> Path:
    """更新 报告/来源与覆盖/_索引.md 固定索引入口（列出全部周报）。"""
    cfg = cfg or load_config()
    site_dir = ROOT / cfg["storage"].get("report_source_dir", "报告/来源与覆盖")
    site_dir.mkdir(parents=True, exist_ok=True)
    reports = M.load_all(storage_paths(cfg)["weekly_reports_dir"], M.WeeklySourceReport)
    lines = ["# 来源与覆盖周报索引", "",
             "| 报告周期 | 进入日报 | 迟到公告 | 复核队列 | 报告 | 明细 |",
             "|------|:--:|:--:|:--:|------|------|"]
    for r in sorted(reports, key=lambda x: x.period_start, reverse=True):
        key = r.id.replace("WSR-", "").replace("-W", "/W")
        lines.append(f"| {r.period_start[:10]} ~ {r.period_end[:10]} | {r.delivered_event_count} "
                     f"| {r.late_event_count} | {r.pending_review_count} "
                     f"| [打开报告]({key}/来源与覆盖周报.md) | [XLSX]({key}/来源与覆盖周报.xlsx) |")
    lines += ["", "固定索引入口：报告/来源与覆盖/_索引.md ｜ 时间口径：北京时间", ""]
    idx = site_dir / "_索引.md"
    idx.write_text("\n".join(lines), encoding="utf-8")
    return idx


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成来源与覆盖周报（默认上一自然周）")
    ap.add_argument("--period", default=None, help="周期键 2026-W34（缺省=上一自然周）")
    ap.add_argument("--force", action="store_true", help="已发布时重新生成")
    args = ap.parse_args(argv)
    if args.period:
        start, end = period_key_to_dates(args.period)
    else:
        start, end = None, None
    report = build_weekly_report(start, end, force=args.force)
    out = publish_weekly_report(report)
    print(f"[周报] {report.id} 已生成：{out['html']} ｜ 明细 {out['xlsx']} / {out['csv']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
