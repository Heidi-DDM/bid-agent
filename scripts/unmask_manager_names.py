#!/usr/bin/env python
"""按业主 2026-09-22 决策恢复项目经理真名（display_name 去脱敏）。

背景：2026-09-14 台账导入时 display_name 生成规则为「脱敏姓+**·台账行号」
（如 `李**·0002`）。业主要求企业资料库人名按公司提供的台账原文展示。
本脚本读取《注册建造师清单（含在施状态）.xlsx》主表，按 display_name
内嵌的台账行号回填真实姓名（如 行2=李海锋 → `李海锋`），保留行号后缀
（消歧重名，如 张伟 ×8）。演示行 M-ND-01 不带行号格式，跳过。

用法：
  DATABASE_URL=... python scripts/unmask_manager_names.py [--dry-run]
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openpyxl  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from runtime.db import api_service  # noqa: E402
from runtime.db.models import Manager  # noqa: E402

LEDGER = (Path(__file__).resolve().parent.parent
          / "企业资料台账20260821"
          / "3.注册建造师清单（含在施状态)未完善"
          / "3.注册建造师清单（含在施状态）.xlsx")
# 「姓（1-2 字）+ * 或 **」·「4 位行号」
NAME_RE = re.compile(r"^(.+?\*+)·(\d{1,5})$")


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("缺少 DATABASE_URL")
        return 2
    dry_run = "--dry-run" in sys.argv
    if not LEDGER.exists():
        print(f"未找到源台账：{LEDGER}")
        return 2
    wb = openpyxl.load_workbook(LEDGER, read_only=True)
    ws = wb.worksheets[0]
    row_name: dict[int, str] = {}
    for i, r in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if r and r[0]:
            row_name[i] = str(r[0]).strip()

    engine = create_engine(database_url)
    updated = missing = skipped = 0
    with Session(engine) as session:
        managers = session.scalars(select(Manager).order_by(Manager.manager_id)).all()
        for m in managers:
            mat = NAME_RE.match(m.display_name or "")
            if not mat:
                skipped += 1
                continue
            row_no = int(mat.group(2))
            real = row_name.get(row_no)
            if not real:
                missing += 1
                print(f"  ! {m.display_name}：台账第 {row_no} 行无姓名，保持原样")
                continue
            new_name = f"{real}·{row_no:04d}"
            print(f"  {m.display_name} → {new_name}")
            if not dry_run:
                m.display_name = new_name
                updated += 1
        if not dry_run:
            api_service.audit(
                session, actor="unmask-script", action="enterprise.manager.unmask",
                basis="2026-09-22 业主决策：企业资料库人名不脱敏；按源台账行号回填真名",
                outcome=f"updated={updated} missing={missing} skipped={skipped}",
                object_ref="managers.display_name",
            )
            session.commit()
    print(f"\n完成：更新 {updated} 条、行号无对应 {missing} 条、非台账格式跳过 {skipped} 条"
          f"{'（dry-run，未写库）' if dry_run else ''}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
