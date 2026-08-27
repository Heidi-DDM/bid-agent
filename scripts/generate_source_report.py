#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""来源与覆盖周报 CLI（默认生成上一自然周，北京时间）。

用法：
  python3 scripts/generate_source_report.py
  python3 scripts/generate_source_report.py --period 2026-W34
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from publish.weekly_report import build_weekly_report, publish_weekly_report, update_report_index  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="生成来源与覆盖周报（默认上一自然周）")
    ap.add_argument("--period", default=None, help="周期键 2026-W34（缺省=上一自然周）")
    ap.add_argument("--force", action="store_true", help="已发布时重新生成")
    args = ap.parse_args(argv)

    if args.period:
        from publish.weekly_report import period_key_to_dates
        start, end = period_key_to_dates(args.period)
    else:
        start = end = None
    report = build_weekly_report(start, end, force=args.force)
    out = publish_weekly_report(report)
    update_report_index()
    print(f"✅ 周报 {report.id} ｜ {report.period_start[:10]} → {report.period_end[:10]}")
    print(f"   巡检 {report.funnel.get('run_count')} 次，成功率 {report.funnel.get('success_rate')}%")
    print(f"   原始发现 {report.funnel.get('raw_items')} → 去重后 {report.funnel.get('deduplicated_events')}"
          f" → 进日报 {report.funnel.get('delivered_events')} ｜ 迟到 {report.funnel.get('late_events')}"
          f" ｜ 复核 {report.funnel.get('pending_review')}")
    print(f"   对账: {'一致' if report.reconciliation.get('balanced') else '不一致 ' + str(report.reconciliation.get('diff_reasons'))}")
    print(f"   产物: {out['report']}")
    print(f"        {out['xlsx']}")
    print(f"        {out['csv']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
