#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ADR-006：LLM 辅助预筛存量回填脚本。

对池内所有 needs_manual_review 且尚无 llm_assist 结论的条目跑大模型分类：
- 明确非招标（招租/结果/政策/新闻）且置信度 ≥ 中 → 移出待处理（可经 restore 恢复）；
- 相关/行业类别 → 写入 screening.llm_assist（前端排序与徽标）；
- 开关关闭 / 门禁不过 / 失败 → 零副作用。

用法：
  .venv/bin/python scripts/backfill_llm_prescreen.py [--limit 400] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / "runtime" / ".env")

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from runtime.db.models import SelectionPoolItem  # noqa: E402
from runtime.discovery import llm_prescreen  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 预筛存量回填")
    parser.add_argument("--limit", type=int, default=400, help="最多处理条数（默认 400）")
    parser.add_argument("--reset", action="store_true",
                        help="先清空已有 AI 结论并复位 AI 排除条目，再按当前企业业务画像全量重判")
    parser.add_argument("--dry-run", action="store_true", help="只统计待处理条数，不调用模型")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("缺少 DATABASE_URL（先 source runtime/.env 或导出）")
        return 2
    engine = create_engine(database_url)
    with Session(engine) as session:
        todo = session.scalars(
            select(SelectionPoolItem)
            .where(SelectionPoolItem.pool_status.in_(["pending", "needs_manual_review"]))
            .order_by(SelectionPoolItem.last_seen_at.desc())
        ).all()
        pending = [it for it in todo if not (it.screening or {}).get("llm_assist")]
        print(f"待分类（pending+needs_manual_review）总数 {len(todo)}，其中无 AI 结论 {len(pending)} 条")
        if args.dry_run:
            return 0
        if not llm_prescreen.is_enabled():
            print("LLM 预筛开关未启用（LLM_FALLBACK_ENABLED / DeepSeek 出域门禁），退出")
            return 1
        stats = llm_prescreen.prescreen_backlog(session, limit=max(1, args.limit), reset=args.reset)
        print(f"回填完成：processed={stats['processed']} classified={stats['classified']} "
              f"promoted={stats.get('promoted', 0)} ai_excluded={stats['ai_excluded']} "
              f"kept_manual={stats['kept_manual']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
