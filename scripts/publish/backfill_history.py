#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史回填：从现有 raw/intake、_dropped、情报库主卡/事实卡 构造发布层数据。

只读上游（绝不修改）：
  raw/intake/*.md、raw/_dropped/*.md、情报库/T-CARD-*.md、情报库/归档/T-CARD-*.md、raw/cards/*.md
产物（幂等，已存在则跳过）：
  data/announcement_events/AE-*.md        ← AnnouncementEvent（§9.1）
  data/source_runs/RUN-*.md               ← SourceRun（§7.2）

历史回填说明（兼容策略）：
  - publish_time 一律取自 intake.publish_date（官方公告发布日期），缺失不推测
  - discovered_at = intake_date（首次入库日）
  - source_run.started_at/finished_at 为推定值（仅日期取自 intake_date，时分取采集典型时段），
    在说明中标注"历史回填推定"
  - 已截止项目（报名期已过）delivery_status 交日报生成器判定（迟到/复核），不在此处写死
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from publish import models as M           # noqa: E402
from publish import timeutil as tu        # noqa: E402
from publish.config import ROOT, load_config, storage_paths  # noqa: E402

RAW = ROOT / "raw"
INTEL = ROOT / "情报库"
CARDS = RAW / "cards"


def _read_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    return yaml.safe_load(parts[1]) or {} if len(parts) >= 3 else {}


def _main_card(project_id: str) -> dict | None:
    for d in (INTEL, INTEL / "归档"):
        if not d.exists():
            continue
        for p in d.glob("T-CARD-*.md"):
            fm = _read_frontmatter(p)
            if fm.get("project_id") == project_id or fm.get("card_id") == project_id:
                return fm
    return None


def _fact_assertion(project_id: str, fact_type: str) -> str:
    """从事实卡提取某类型断言（如 qualification）。"""
    for p in sorted(CARDS.glob("*.md")):
        fm = _read_frontmatter(p)
        if fm.get("parent_project") == project_id and fm.get("fact_type") == fact_type:
            return fm.get("assertion", "")
    return ""


def _detail_fields(project_id: str) -> dict:
    card = _main_card(project_id) or {}
    d = {}
    for k in ("tender_no", "tenderee", "agency", "region", "project_type", "industry",
              "procurement_method", "budget_amount", "ceiling_price", "bid_bond_amount",
              "deadline_signup", "deadline_bid", "open_date", "qualification",
              "personnel", "performance", "joint_venture", "review_standard",
              "dark_bid", "submission_method", "tender_doc_method", "tender_doc_window",
              "tender_doc_link"):
        v = card.get(k)
        if v and str(v).strip() not in ("待补", "待补（公告未载明编号）", "暂无", "未知", "None", ""):
            d[k] = v
    if "qualification" not in d:
        q = _fact_assertion(project_id, "qualification")
        if q:
            d["qualification"] = q
    return d


def _load_intake_files() -> list[dict]:
    """汇总 intake + dropped 的 frontmatter（按 intake_id 去重，dropped 优先标记）。"""
    out = {}
    for d in (RAW / "intake", RAW / "_dropped"):
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            fm = _read_frontmatter(p)
            if not fm.get("intake_id"):
                continue
            fm["_dropped"] = d.name == "_dropped"
            out[fm["intake_id"]] = fm
    return list(out.values())


def backfill_events(cfg: dict) -> list[Path]:
    """构造 AnnouncementEvent（委托 sync_events 增量同步器）。

    sync_events 为长期接入点（G3 门禁后调用）：幂等、计算 content_hash/dedup_key、
    按 dedup_key 归并跨平台来源、不覆盖发布层状态。此处保留兼容入口。
    返回本次新建事件文件列表。
    """
    from publish.sync_events import sync_events
    r = sync_events(cfg)
    created = []
    for ev_id in r["created"]:
        created.append(M.event_path(storage_paths(cfg)["events_dir"], ev_id))
    return created


def backfill_source_runs(cfg: dict) -> list[Path]:
    """按平台构造 2026-08-13 巡检运行记录（来源：intake/_dropped 文档事实）。

    数字口径（以文档为准）：
      全国公共资源交易平台（河北）：3 条 intake（001/002/003，全部 G0 时效丢弃）
      河北交投招标与采购服务平台：1 条 intake（004，入库）
    started_at/finished_at 为推定（历史回填标注）。
    """
    paths = storage_paths(cfg)
    intakes = _load_intake_files()
    created = []
    runs_spec = [
        {
            "source_id": "ggzy-hebei",
            "source_name": "全国公共资源交易平台（河北）",
            "started_at": "2026-08-13T09:00:00+08:00",
            "finished_at": "2026-08-13T09:40:00+08:00",
            "status": "success",
            "pages_scanned": 12,
            "raw_items_found": 3,
            "valid_events_found": 3,
            "duplicate_items": 0,
            "rejected_items": 3,          # 001/002/003 报名期已过，G0 时效判定不予采纳
            "late_items": 3,
            "note": "历史回填：raw_items 与 rejected 来自 raw/_dropped/ 三份 intake 文档；pages_scanned 与起止时间为推定",
        },
        {
            "source_id": "hebtig",
            "source_name": "河北交投招标与采购服务平台",
            "started_at": "2026-08-13T10:00:00+08:00",
            "finished_at": "2026-08-13T10:15:00+08:00",
            "status": "success",
            "pages_scanned": 6,
            "raw_items_found": 1,
            "valid_events_found": 1,
            "duplicate_items": 0,
            "rejected_items": 0,
            "late_items": 1,              # 康保 publish 08-10，08-13 发现 → 迟到
            "note": "历史回填：来自 raw/intake/zb-20260813-004.md；pages_scanned 与起止时间为推定",
        },
    ]
    for spec in runs_spec:
        run_id = f"RUN-20260813-{spec['source_id'].replace('-', '')}"
        run_path = M.source_run_path(paths["source_runs_dir"], run_id)
        if run_path.exists():
            continue
        now_iso = tu.iso(tu.now()) or ""
        run = M.SourceRun(
            id=run_id,
            source_id=spec["source_id"],
            source_name=spec["source_name"],
            started_at=spec["started_at"],
            finished_at=spec["finished_at"],
            status=spec["status"],
            pages_scanned=spec["pages_scanned"],
            raw_items_found=spec["raw_items_found"],
            valid_events_found=spec["valid_events_found"],
            duplicate_items=spec["duplicate_items"],
            rejected_items=spec["rejected_items"],
            late_items=spec["late_items"],
            error_category=None,
            error_summary=spec["note"],
            retry_count=0,
            last_success_at=spec["finished_at"],
            task_version="backfill-v1",
            created_at=now_iso,
        )
        run._path = str(run_path)
        M.save(run)
        created.append(run_path)
        print(f"  [巡检] {run_id} {spec['source_name']} status={spec['status']} raw={spec['raw_items_found']}")
    return created


def main() -> int:
    cfg = load_config()
    print("== 回填 AnnouncementEvent ==")
    evs = backfill_events(cfg)
    print(f"  新建 {len(evs)} 条事件")
    print("== 回填 SourceRun ==")
    runs = backfill_source_runs(cfg)
    print(f"  新建 {len(runs)} 条巡检记录")
    print("完成。上游文档未被修改（只读）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
