#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日报生成 CLI（幂等）。

用法：
  python3 scripts/generate_daily_issue.py 2026-08-17
  python3 scripts/generate_daily_issue.py 2026-08-17 --force   # 重新生成并递增版本
  python3 scripts/generate_daily_issue.py 2026-08-17 --channels wechat,web

流程（§10.1）：按北京时间取 D 起止 → 收集事件 → 过滤门禁 → 缺失时间送复核 →
跨平台去重 → 紧迫度/分组 → 创建/更新 DailyIssue → 渲染 HTML → 完整性检查 →
发布稳定链接 → 记录 DeliveryRecord → 更新日报归档索引。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from publish import models as M                  # noqa: E402
from publish import renderer                     # noqa: E402
from publish.config import ROOT, load_config, storage_paths  # noqa: E402
from publish.daily_issue_generator import generate_daily_issue, record_deliveries  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="生成日报（幂等）")
    ap.add_argument("issue_date", help="日报日期 YYYY-MM-DD（北京时间口径）")
    ap.add_argument("--force", action="store_true", help="已发布时重新生成并递增版本号")
    args = ap.parse_args(argv)

    cfg = load_config()
    issue = generate_daily_issue(args.issue_date, force=args.force, cfg=cfg)

    # 渲染报告 + 落盘 + 索引（即使一致性检查通过也确保报告产物存在）
    paths = storage_paths(cfg)
    events = {e.id: e for e in M.load_all(paths["events_dir"], M.AnnouncementEvent)}
    report_path = renderer.write_daily_report(issue, events, cfg)
    # 同步 archive_path 为报告实际路径（一致性检查返回的旧对象可能残留旧路径）
    rel_path = report_path.relative_to(ROOT)
    if issue.archive_path != str(rel_path):
        issue.archive_path = str(rel_path)
        M.save(issue)
    all_issues = M.load_all(paths["issues_dir"], M.DailyIssue)
    renderer.write_report_indexes(all_issues, cfg)
    recs = record_deliveries(issue, cfg)

    print(f"✅ 日报 {issue.issue_date} status={issue.status} v{issue.version} "
          f"items={issue.item_count} urgent={issue.urgent_count}")
    print(f"   报告: {report_path}（稳定路径 {issue.stable_url}）")
    print(f"   分享摘要: {renderer.share_message(issue, cfg)}")
    print(f"   投递: {[(r.channel, r.status) for r in recs]}")
    if issue.pending_review_items:
        print(f"   ⚠️ 复核队列（缺发布时间，不进日报）: {len(issue.pending_review_items)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
