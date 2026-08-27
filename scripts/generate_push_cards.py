#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 方案：飞书推送卡片生成 CLI（2026-08-17）。

用法：
  python3 scripts/generate_push_cards.py                 # 今日日报 → 推送卡片（stdout）
  python3 scripts/generate_push_cards.py 2026-08-17      # 指定日期
  python3 scripts/generate_push_cards.py 2026-08-17 --no-check-links   # 跳过浏览器级链接复检

流程（B 方案：一条管线一次校验，cron 只触发巡检+入库+取卡片）：
  读 DailyIssue → 事件（build_customer_context 数据边界）→ 链接浏览器级复检
  （check_links_browser.py，三态标注）→ renderer.render_push_cards_md → stdout

设计原则：
  - 卡片内容 100% 由 renderer 生成（模板驱动），Agent/cron 不得手工改写卡片文本
  - 链接复检结果缓存到 /tmp/push_link_checks_<date>.json（同日多次生成复用，
    避免每次推送重复启动 Chrome headless；--force-check 可强制重检）
  - 输出即飞书推送内容；同时落盘 推送日志/PUSH-CARDS-<date>.txt 留档
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from publish import models as M                  # noqa: E402
from publish import renderer                     # noqa: E402
from publish.config import ROOT, load_config, storage_paths  # noqa: E402

CACHE_PREFIX = "/tmp/push_link_checks_"
LOG_DIR = ROOT / "推送日志"


def _today() -> str:
    # 北京时间
    import zoneinfo
    from datetime import datetime
    tz = zoneinfo.ZoneInfo("Asia/Shanghai")
    return datetime.now(tz).strftime("%Y-%m-%d")


def _load_link_checks(issue_date: str, events: dict, force: bool = False) -> dict:
    """收集当天事件全部链接 → check_links_browser.py 复检 → {url: verdict}。

    verdict 映射：browser_ok / browser_blocked / browser_fail / unreachable。
    缓存：同日复用；force 强制重检。
    """
    cache_path = Path(f"{CACHE_PREFIX}{issue_date}.json")
    if cache_path.exists() and not force:
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    urls = set()
    for e in events.values():
        if e.primary_source_url:
            urls.add(e.primary_source_url)
        if e.tender_doc_link:
            urls.add(e.tender_doc_link)

    checks: dict[str, str] = {}
    if urls:
        script = ROOT / "scripts" / "check_links_browser.py"
        proc = subprocess.run(
            [sys.executable, str(script)] + sorted(urls),
            capture_output=True, text=True, timeout=600, cwd=str(ROOT / "scripts"),
        )
        # 解析 JSON 输出（--- json --- 之后）
        out = proc.stdout or ""
        if "--- json ---" in out:
            payload = out.split("--- json ---", 1)[1].strip()
            try:
                results = json.loads(payload)
                for r in results:
                    checks[r["url"]] = r["verdict"]
            except Exception:
                print(f"[warn] 链接复检 JSON 解析失败，卡片链接标注降级为默认", file=sys.stderr)
        else:
            print(f"[warn] 链接复检脚本无 JSON 输出（exit={proc.returncode}），标注降级", file=sys.stderr)
    try:
        cache_path.write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return checks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成飞书推送卡片（B 方案）")
    ap.add_argument("issue_date", nargs="?", default=None, help="日报日期 YYYY-MM-DD（默认今天，北京时间）")
    ap.add_argument("--no-check-links", action="store_true", help="跳过浏览器级链接复检")
    ap.add_argument("--force-check", action="store_true", help="强制重新链接复检（忽略缓存）")
    ap.add_argument("--out", default=None, help="输出文件路径（默认同时写 推送日志/PUSH-CARDS-<date>.txt）")
    args = ap.parse_args(argv)

    issue_date = args.issue_date or _today()
    cfg = load_config()
    paths = storage_paths(cfg)

    issues = {i.issue_date: i for i in M.load_all(paths["issues_dir"], M.DailyIssue)}
    if issue_date not in issues:
        print(f"❌ 日报不存在: {issue_date}（先运行 python3 scripts/generate_daily_issue.py {issue_date}）", file=sys.stderr)
        return 2
    issue = issues[issue_date]
    events = {e.id: e for e in M.load_all(paths["events_dir"], M.AnnouncementEvent)}

    link_checks = None
    if not args.no_check_links:
        link_checks = _load_link_checks(issue_date, events, force=args.force_check)

    text = renderer.render_push_cards_md(issue, events, cfg, link_checks=link_checks)
    print(text)

    # 留档
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = Path(args.out) if args.out else LOG_DIR / f"PUSH-CARDS-{issue_date}.txt"
    log_path.write_text(text, encoding="utf-8")
    print(f"\n📄 卡片已留档: {log_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
