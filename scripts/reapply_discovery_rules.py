#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预筛规则变更后的存量待选池重筛（2026-09-24 用户排除关键词需求）。

背景：排除关键词（如 咨询/科研/监理/勘察设计/设备采购）在规则层只影响
**新搜索**的候选；已入池的存量 pending 条目不会自动降级。本脚本按当前
生效规则对存量「未人工处理」条目（pending / needs_manual_review）重放
screen_candidate，只做**降级方向**的迁移：

  - pending 且新规则命中排除词 / 非招标类型 → needs_manual_review（或 expired）
  - needs_manual_review 不做自动晋级（晋级交给 AI 预筛，且策略排除条目
    已被 llm_prescreen 跳过，2026-09-24 修复）
  - 已深入 / 已导入 / 已删除 / superseded / expired 一律不动（人工态不覆盖）

screening 合并语义：保留 llm_assist 等历史结论，替换规则层字段
（rule_version / screen_state / reason / matched_* / screened_at / source）。

用法（仓库根运行）：
  .venv/bin/python scripts/reapply_discovery_rules.py            # 执行
  .venv/bin/python scripts/reapply_discovery_rules.py --dry-run  # 只预览
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from runtime.core.config import database_url
from runtime.db.models import AnnouncementCandidate, SelectionPoolItem
from runtime.discovery.service import (
    POOL_EXPIRED, POOL_NEEDS_REVIEW, POOL_PENDING,
    active_rule, screen_candidate,
)

# 规则层字段：重筛时整体替换；其余键（llm_assist / restored_by 等）保留
RULE_FIELDS = ("rule_version", "screen_state", "reason", "announcement_type",
               "announcement_type_label", "matched_industries", "matched_exclusions",
               "missing_fields", "screened_at", "source")


def main() -> int:
    ap = argparse.ArgumentParser(description="存量待选池按生效规则重筛（只降级不晋级）")
    ap.add_argument("--dry-run", action="store_true", help="只预览不落库")
    ap.add_argument("--demote-ai-categories", default="勘察设计咨询,设备采购",
                    help="同时把这些 AI 分类的主列表（pending）条目降级为待人工确认；"
                         "逗号分隔；传空字符串关闭")
    args = ap.parse_args()

    engine = create_engine(database_url())
    S = sessionmaker(bind=engine)
    session = S()
    version, config = active_rule(session)
    print(f"生效规则：{version}")
    print(f"  行业关键词：{config.get('industry_keywords')}")
    print(f"  排除关键词：{config.get('exclude_keywords')}")

    rows = session.execute(
        select(SelectionPoolItem, AnnouncementCandidate)
        .join(AnnouncementCandidate,
              AnnouncementCandidate.candidate_id == SelectionPoolItem.candidate_id)
        .where(SelectionPoolItem.pool_status.in_([POOL_PENDING, POOL_NEEDS_REVIEW]))
        .order_by(AnnouncementCandidate.publish_date.desc().nullslast())
    ).all()
    print(f"待重筛（pending + needs_manual_review）：{len(rows)} 条")

    changes = []
    demoted_cats = [c.strip() for c in args.demote_ai_categories.split(",") if c.strip()]
    for item, cand in rows:
        result = screen_candidate(cand, rule_version=version, config=config)
        new_status = result.pop("pool_status")
        # ① 标题命中排除关键词的 pending → 待人工确认（本次需求）
        if (item.pool_status == POOL_PENDING
                and result.get("matched_exclusions")
                and new_status == POOL_NEEDS_REVIEW):
            changes.append((item, cand, item.pool_status, new_status, result))
            continue
        # ② AI 分类属经营策略排除类型（勘察设计咨询/设备采购等）的 pending → 待人工确认
        #    （标题无关键词但项目类型不符；分类事实来自 AI 预筛留档）
        ai_cat = ((item.screening or {}).get("llm_assist") or {}).get("category")
        if item.pool_status == POOL_PENDING and ai_cat in demoted_cats:
            changes.append((item, cand, item.pool_status, POOL_NEEDS_REVIEW, {
                **result,
                "screen_state": "needs_manual_review",
                "reason": f"AI 分类「{ai_cat}」属经营策略排除类型（勘察设计/设备采购等不投）",
            }))

    print(f"将降级条目：{len(changes)} 条")
    for item, cand, old, new, result in changes:
        flag = "→ 待人工确认" if new == POOL_NEEDS_REVIEW else "→ 已排除"
        print(f"  [{cand.publish_date}] {cand.title[:44]} {flag}（{result['reason']}）")
    if args.dry_run or not changes:
        session.rollback()
        return 0

    for item, cand, old, new, result in changes:
        screening = dict(item.screening or {})
        for f in RULE_FIELDS:
            screening.pop(f, None)
        screening.update(result)
        item.pool_status = new
        item.screening = screening  # JSONB 整体赋值
    session.commit()
    print(f"已落库 {len(changes)} 条（llm_assist 历史结论保留；人工处理态未触碰）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
