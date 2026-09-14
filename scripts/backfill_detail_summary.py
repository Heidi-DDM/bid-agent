#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存量候选 detail_summary 补算（2026-09-09）。

背景：公告详情初筛抽取（announcement_prescreen）在 2026-09-09 上线，此前导入的候选
detail_summary 为空。本脚本对已 imported 且 detail_summary 为空的候选，从对象存储里
已存档的净化文本（MAT-{candidate_id}/…原文件）重跑抽取并回填，幂等可重复执行。

用法（仓库根运行，先 unset PYTHONPATH && unset DATABASE_URL）：
  .venv/bin/python scripts/backfill_detail_summary.py            # 回填 detail_summary 为空的存量
  .venv/bin/python scripts/backfill_detail_summary.py --all      # 全部重算（含已有值，谨慎）
  .venv/bin/python scripts/backfill_detail_summary.py --dry-run  # 只预览不改库
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from runtime.core.config import object_store_root
from runtime.db.models import AnnouncementCandidate, MaterialVersion
from runtime.parsing.provenance import build_detail_summary


def backfill(session, *, force_all: bool = False, dry_run: bool = False) -> dict:
    """对存量 imported 候选回填 detail_summary（幂等）。

    E2（2026-09-10）：直读 material_versions 表按 MAT-{candidate_id} 反查最新版本
    的 object_uri，不依赖 materials 表头（存量库里 materials 行可能缺失或与版本
    表不同步，逐版本表才是原文事实）；对存量候选用当前最新抽取锚点重算详情。
    """
    store_root = object_store_root()
    rows = session.scalars(select(AnnouncementCandidate).where(
        AnnouncementCandidate.import_status == "imported"
    )).all()
    res = {"backfilled": 0, "skipped_no_material": 0, "failed": []}
    for c in rows:
        if not force_all and c.detail_summary:
            continue  # 已有值，跳过
        ver = session.scalars(select(MaterialVersion).where(
            MaterialVersion.material_id == f"MAT-{c.candidate_id}"
        ).order_by(MaterialVersion.version.desc())).first()
        if ver is None:
            res["skipped_no_material"] += 1
            continue
        obj_path = Path(store_root) / ver.object_uri
        if not obj_path.exists():
            res["skipped_no_material"] += 1
            continue
        try:
            # P1（2026-09-11）：与 service.import_candidate_detail 同口径——quote/start/end
            # 偏移 + content_hash 绑定该版本（ver.content_hash），落库前 round-trip 硬校验
            text = obj_path.read_text(encoding="utf-8", errors="ignore")
            detail = build_detail_summary(c.title, text, content_hash=ver.content_hash)
        except Exception as exc:  # 单条失败不阻断其余
            res["failed"].append(f"{c.candidate_id}: {exc}")
            continue
        if dry_run:
            print(f"  [dry-run] {c.candidate_id} {c.title[:24]} → {len(detail)} 字段")
            res["backfilled"] += 1
            continue
        c.detail_summary = detail
        # C4：详情「建设地点」命中 → 同步回填候选 region（公告事实口径）
        region_field = detail.get("region") or {}
        if region_field and not region_field.get("missing") and region_field.get("value"):
            c.region = str(region_field["value"])[:64]
        session.add(c)
        res["backfilled"] += 1
    if not dry_run:
        session.commit()
    return res


def _main() -> int:
    ap = argparse.ArgumentParser(description="存量候选 detail_summary 补算")
    ap.add_argument("--all", action="store_true", help="对全部 imported 候选强制重算（含已有值）")
    ap.add_argument("--dry-run", action="store_true", help="只预览不改库")
    args = ap.parse_args()
    from runtime.core.config import database_url
    engine = create_engine(database_url())
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as session:
        result = backfill(session, force_all=args.all, dry_run=args.dry_run)
    print(f"补算结果: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())