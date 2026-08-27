#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日报编排生成器（任务书 §10.1 十五步，幂等）。

语义：
  generate_daily_issue(date, force=False)
    force=False：存在已发布日报时只做一致性检查（不重写、不重复创建条目）
    force=True ：允许重新生成，版本号 +1，保存修订原因

日期口径（§4.1）：统一 Asia/Shanghai；日报 D 收录 publish_time ∈ [D 00:00, D+1 00:00)。
publish_time 缺失 → pending_review / missing_publish_time，进后台人工复核队列，绝不直接进日报。
迟到公告（§4.6）：publish_time < D 但当天发现 → late_discovered，按配置策略处置（默认 backend_only）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from . import models as M
from . import timeutil as tu
from . import dedup as DD
from . import urgency as UG
from .config import load_config, storage_paths

SECTION_ORDER = ["urgent", "deadline", "new", "change", "result"]


def _issue_id(issue_date: date) -> str:
    return f"DI-{issue_date.isoformat()}"


def _classify(events: list, start, end, now_dt=None,
              mode: str = "participable", exclude_pushed: bool = True,
              pushed_ids: set | None = None) -> dict:
    """事件分类（v0.3.0 可参与性准入）。

    mode=participable（2026-08-18 用户决策）：
      - participable：deadline_signup 或 deadline_bid ≥ now（当天仍可报名/下载/投递）→ 进日报
      - expired：两个截止时间都已过 → 后台（不进日报）
      - missing_deadline：无截止时间可判定 → 复核队列
      - pushed：曾推送过（历史日报 items 出现过）→ 排除，不重复推送
    兼容历史 mode=day：按 publish_time ∈ [start, end) 分类（迟到/当天/未来）。
    """
    cat = {"participable": [], "expired": [], "missing_deadline": [], "pushed": [],
           "day": [], "late": [], "missing": [], "future": [], "gate_rejected": []}
    now_dt = now_dt or tu.now()
    for e in events:
        if e.fact_gate_status not in ("passed", ""):
            cat["gate_rejected"].append(e)
            continue
        if exclude_pushed and pushed_ids and e.id in pushed_ids:
            cat["pushed"].append(e)
            continue
        if mode == "participable":
            signup = tu.parse_dt(e.deadline_signup)
            bid = tu.parse_dt(e.deadline_bid)
            if signup is None and bid is None:
                cat["missing_deadline"].append(e)
            elif (signup is not None and signup >= now_dt) or (bid is not None and bid >= now_dt):
                cat["participable"].append(e)
            else:
                cat["expired"].append(e)
            continue
        # 兼容旧 mode=day（publish_time 口径）
        pt = tu.parse_dt(e.publish_time)
        if pt is None:
            cat["missing"].append(e)
        elif pt < start:
            cat["late"].append(e)
        elif pt >= end:
            cat["future"].append(e)
        else:
            cat["day"].append(e)
    return cat


def _mark_event(e: M.AnnouncementEvent, **kw) -> None:
    """更新事件字段并落盘（幂等：重复执行不改变已标记状态）。

    测试注入的内存事件（无 _path）只更新内存，不落盘。
    """
    changed = False
    for k, v in kw.items():
        if getattr(e, k, None) != v:
            setattr(e, k, v)
            changed = True
    if changed:
        e.updated_at = tu.iso(tu.now()) or ""
        if getattr(e, "_path", ""):
            M.save(e)


def _consistency_check(issue: M.DailyIssue, expected_items: list[M.DailyIssueItem]) -> dict:
    """force=False 且已发布时的一致性检查。返回 {ok, diffs}。"""
    diffs = []
    exp_keys = {(i.daily_issue_id, i.announcement_event_id) for i in expected_items}
    cur_keys = {(i.daily_issue_id, i.announcement_event_id) for i in issue.items}
    if exp_keys != cur_keys:
        diffs.append(f"条目集合不一致：应为 {len(exp_keys)} 条，现有 {len(cur_keys)} 条")
    if issue.item_count != len(issue.items):
        diffs.append(f"item_count 不一致：记录 {issue.item_count}，实际 {len(issue.items)}")
    return {"ok": not diffs, "diffs": diffs}


def _build_items(issue_id: str, groups: list, cfg: dict, now_dt) -> list[M.DailyIssueItem]:
    """为去重后的每组事件生成日报条目（含 section/urgency/display_order）。"""
    items: list[M.DailyIssueItem] = []
    for grp in groups:
        e = grp["primary"]
        urg = UG.compute_urgency(e.deadline_signup, e.deadline_bid, now_dt,
                                 cfg["delivery"].get("urgent_threshold_hours", 24),
                                 cfg["delivery"].get("high_threshold_hours", 72))
        section = UG.section_for(e.announcement_type, urg)
        items.append(M.DailyIssueItem(
            id=f"{issue_id}-I{len(items) + 1:03d}",
            daily_issue_id=issue_id,
            announcement_event_id=e.id,
            section=section,
            urgency=urg["urgency"],
            display_order=len(items),
            included_at=tu.iso(now_dt) or "",
            inclusion_reason=(f"当天可参与（报名/下载/投递未截止，原发布于 "
                              f"{tu.fmt_dt(tu.parse_dt(e.publish_time), '%Y-%m-%d') if e.publish_time else '未知'}）"
                              + (f"；紧迫度 {UG.URGENCY_LABELS[urg['urgency']]}" if urg["urgency"] != "normal" else "")),
        ))
    # 展示分组顺序（config grouping 顺序优先，其余按序追加）
    order = [s for s in cfg["daily_issue"].get("grouping", SECTION_ORDER) if s in SECTION_ORDER]
    order += [s for s in SECTION_ORDER if s not in order]
    items.sort(key=lambda i: (order.index(i.section), i.display_order))
    for idx, it in enumerate(items):
        it.display_order = idx
    return M.unique(items)


def _collect_pushed_ids(issues_dir: Path) -> set:
    """收集历史日报中已推送的事件 ID（exclude_pushed 去重依据）。

    遍历 data/daily_issues/*.md，取每个 DailyIssue.items 的 announcement_event_id。
    幂等：已推送的事件不再进入后续日报。
    """
    pushed: set = set()
    if not issues_dir.exists():
        return pushed
    for p in sorted(issues_dir.glob("*.md")):
        fm, _ = M.read_doc(p)
        if fm.get("type") != M.DailyIssue.TYPE:
            continue
        for it in fm.get("items", []):
            eid = it.get("announcement_event_id") or it.get("event_id")
            if eid:
                pushed.add(eid)
    return pushed


def _review_ids(cat: dict) -> list:
    """复核队列事件 ID：participable 模式下 = missing_deadline；兼容 day 模式 = missing。"""
    return [e.id for e in (cat.get("missing_deadline") or cat.get("missing") or [])]


def generate_daily_issue(issue_date: date | str, force: bool = False, cfg: dict | None = None,
                         events: list | None = None, now_dt=None) -> M.DailyIssue:
    """幂等生成 D 日日报。

    ``now_dt`` 仅用于可复现的回放和测试；未传入时使用当前北京时间。
    """
    cfg = cfg or load_config()
    paths = storage_paths(cfg)
    d = issue_date if isinstance(issue_date, date) else tu.parse_dt(str(issue_date)).date()
    start, end = tu.day_bounds(d)
    now_dt = now_dt or tu.now()
    issue_id = _issue_id(d)
    issue_path = M.daily_issue_path(paths["issues_dir"], d.isoformat())

    # 已有日报：force=False → 一致性检查；force=True → 重新生成（版本+1）
    existing = None
    if issue_path.exists():
        fm, _ = M.read_doc(issue_path)
        if fm.get("type") == M.DailyIssue.TYPE:
            existing = M.DailyIssue.from_dict(fm, path=issue_path)

    # 步骤 3-4：收集事件并按准入模式分类（v0.3.0 可参与性准入 + 已推送去重）
    evs = events if events is not None else M.load_all(paths["events_dir"], M.AnnouncementEvent)
    adm_mode = cfg["daily_issue"].get("admission_mode", "participable")
    exclude_pushed = cfg["daily_issue"].get("exclude_pushed", True)
    pushed_ids = _collect_pushed_ids(paths["issues_dir"]) if exclude_pushed else set()
    cat = _classify(evs, start, end, now_dt=now_dt, mode=adm_mode,
                    exclude_pushed=exclude_pushed, pushed_ids=pushed_ids)

    # 步骤 5：可参与性无法判定（无截止时间）→ 复核队列（写回事件文件）
    for e in cat["missing_deadline"]:
        _mark_event(e, delivery_status="pending_review", exclusion_reason="missing_deadline")

    # 步骤 5.1：截止时间已过 → 后台 excluded（写回事件文件，不进日报）
    for e in cat["expired"]:
        _mark_event(e, delivery_status="excluded", exclusion_reason="deadline_expired")

    # 步骤 4.6（兼容旧 mode=day）：迟到公告标记
    for e in cat["late"]:
        _mark_event(e, delivery_status="late_discovered",
                    late_discovered_at=tu.iso(now_dt) or "",
                    original_publish_time=e.publish_time)

    # 步骤 6：可参与事件跨平台去重（participable 模式下取 participable；兼容 day 模式取 day）
    day_events = cat["participable"] if adm_mode == "participable" else cat["day"]
    groups = DD.dedupe_for_daily(day_events, cfg["dedup"].get("primary_source_priority", []))

    # 步骤 7：生成条目（section/urgency）
    items = _build_items(issue_id, groups, cfg, now_dt)
    urgent_count = sum(1 for i in items if i.urgency in ("urgent", "high"))

    # 迟到策略（§4.6，配置化）
    policy = cfg["delivery"].get("late_announcement_policy", "backend_only")
    late_items: list[M.DailyIssueItem] = []
    if policy in ("supplement_section", "urgent_only"):
        for e in cat["late"]:
            urg = UG.compute_urgency(e.deadline_signup, e.deadline_bid, now_dt,
                                     cfg["delivery"].get("urgent_threshold_hours", 24),
                                     cfg["delivery"].get("high_threshold_hours", 72))
            if policy == "urgent_only" and urg["urgency"] != "urgent":
                continue
            late_items.append(M.DailyIssueItem(
                id=f"{issue_id}-L{len(late_items) + 1:03d}",
                daily_issue_id=issue_id, announcement_event_id=e.id,
                section="new", urgency=urg["urgency"],
                display_order=len(late_items),
                included_at=tu.iso(now_dt) or "",
                inclusion_reason=f"迟到补录（原发布时间 {tu.fmt_dt(tu.parse_dt(e.publish_time), '%Y-%m-%d')}）",
            ))
    late_items = M.unique(late_items)

    status = "published" if items else "published_empty"
    if not items and not cfg["daily_issue"].get("include_empty_issue", True):
        status = "failed"  # 配置要求不生成空日报

    title = cfg["daily_issue"].get("title_template", "某建设招标信息日报｜{date}").format(date=d.isoformat())
    stable_url = f"/daily/{d.isoformat()}"

    if existing and existing.status in ("published", "published_empty", "revised"):
        if not force:
            chk = _consistency_check(existing, items)
            existing.pending_review_items = _review_ids(cat)
            if not chk["ok"]:
                existing.status = "revised" if existing.status == "revised" else existing.status
                print(f"[一致性检查] {d.isoformat()} 日报存在，差异：{chk['diffs']}（如需重生成请 --force）")
            else:
                print(f"[一致性检查] {d.isoformat()} 日报一致，无需变更（{existing.item_count} 条）")
            M.save(existing)
            return existing

        # force=True：版本递增 + 修订记录
        old_items = [(i.announcement_event_id, i.section) for i in existing.items]
        existing.version += 1
        existing.revisions.append({
            "version": existing.version,
            "at": tu.iso(now_dt) or "",
            "reason": "force 重新生成（人工触发）",
            "actor": "system:generate_daily_issue(force=true)",
            "changes": {
                "removed": [eid for eid, _ in old_items if eid not in {i.announcement_event_id for i in items}],
                "added": [i.announcement_event_id for i in items if i.announcement_event_id not in {eid for eid, _ in old_items}],
                "count_before": len(old_items),
                "count_after": len(items),
            },
        })
        existing.status = "revised"
        existing.items = items
        existing.late_items = late_items
        existing.pending_review_items = _review_ids(cat)
        existing.item_count = len(items)
        existing.urgent_count = urgent_count
        existing.last_updated_at = tu.iso(now_dt) or ""
        existing.title = title
        M.save(existing)
        return existing

    # 新建日报
    issue = M.DailyIssue(
        id=issue_id, issue_date=d.isoformat(), title=title, status=status,
        item_count=len(items), urgent_count=urgent_count, version=1,
        generated_at=tu.iso(now_dt) or "", last_updated_at=tu.iso(now_dt) or "",
        stable_url=f"/daily/{d.isoformat()}", archive_path=f"{cfg['storage'].get('report_daily_dir', '报告/日报')}/{d.isoformat()}.md",
        items=items, late_items=late_items,
        pending_review_items=_review_ids(cat),
    )
    issue.published_at = tu.iso(now_dt) if status in ("published", "published_empty") else None
    issue._path = str(issue_path)
    M.save(issue)
    print(f"[生成] {d.isoformat()} 日报 status={status} items={len(items)} urgent={urgent_count} "
          f"late={len(cat['late'])} pending_review={len(_review_ids(cat))}")
    return issue


def record_deliveries(issue: M.DailyIssue, cfg: dict | None = None) -> list[M.DeliveryRecord]:
    """按配置渠道生成投递记录（§9.4 / §10.1 步骤 13-14）。

    web 渠道：日报页面已生成 → status=sent（rendered_url 即稳定地址）
    wechat/feishu/email：标记 pending，由外部发送器发送后更新 status。
    幂等：同一 (issue, channel) 已存在记录则不重复创建。
    """
    cfg = cfg or load_config()
    paths = storage_paths(cfg)
    out = []
    for ch in cfg["delivery"].get("channels", ["web"]):
        rec_path = M.delivery_path(paths["deliveries_dir"], issue.issue_date, ch)
        if rec_path.exists():
            fm, _ = M.read_doc(rec_path)
            out.append(M.DeliveryRecord.from_dict(fm, path=rec_path))
            continue
        rec = M.DeliveryRecord(
            id=f"DEL-{issue.issue_date}-{ch}",
            daily_issue_id=issue.id,
            channel=ch,
            target=issue.stable_url,
            status="sent" if ch == "web" else "pending",
            sent_at=tu.iso(tu.now()) if ch == "web" else None,
            retry_count=0,
            rendered_url=issue.stable_url,
            created_at=tu.iso(tu.now()) or "",
        )
        rec._path = str(rec_path)
        M.save(rec)
        out.append(rec)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成日报（幂等）")
    ap.add_argument("issue_date", help="日报日期 YYYY-MM-DD（北京时间）")
    ap.add_argument("--force", action="store_true", help="已发布时重新生成并递增版本号")
    args = ap.parse_args(argv)
    generate_daily_issue(args.issue_date, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
