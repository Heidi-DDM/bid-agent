#!/usr/bin/env python
"""修复在施状态与在施项目矛盾（2026-09-22 项目经理M实测问题）。

根因：台账导入时主表「在施项目名称」为空则无条件回退采纳 Sheet1 项目名，
未与主表「在施状态」做一致性校验 → 147 名经理（含项目经理M·0035）与 140 条人员
出现「状态=否（可用）却登记在施项目」的矛盾。

规则（业主口径：源文件主表「在施状态」为准）：
- 主表在施状态=否 → active_projects/on_site_project 清空（不采纳项目名）；
- =是 → 保持 occupied（项目名可缺，待补）；主表空+Sheet1 有项目 → 保持 occupied。

用法：
  DATABASE_URL=... python scripts/fix_onsite_conflicts.py [--dry-run]
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
from runtime.db.models import Manager, Personnel  # noqa: E402

LEDGER = (Path(__file__).resolve().parent.parent
          / "企业资料台账20260821"
          / "3.注册建造师清单（含在施状态)未完善"
          / "3.注册建造师清单（含在施状态）.xlsx")
ROW_RE = re.compile(r"^ledger:xlsx:sheet0:row(\d+)$")


def _main_status() -> dict[int, tuple[str, str]]:
    """主表行号 → (姓名, 在施状态)。"""
    wb = openpyxl.load_workbook(LEDGER, read_only=True)
    ws = wb.worksheets[0]
    out: dict[int, tuple[str, str]] = {}
    for i, r in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if r and r[0]:
            out[i] = (str(r[0]).strip(),
                      str(r[14]).strip() if r[14] is not None else "")
    return out


def _row_of(evidence_refs: list) -> int | None:
    for ref in evidence_refs or []:
        m = ROW_RE.match(str(ref))
        if m:
            return int(m.group(1))
    return None


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("缺少 DATABASE_URL")
        return 2
    dry_run = "--dry-run" in sys.argv
    main_status = _main_status()
    engine = create_engine(database_url)
    mgr_fixed = pnl_fixed = 0
    samples: list[str] = []
    with Session(engine) as session:
        managers = session.scalars(select(Manager).order_by(Manager.manager_id)).all()
        for m in managers:
            mat = re.search(r"·(\d{1,5})$", m.display_name or "")
            row_no = int(mat.group(1)) if mat else _row_of(m.evidence_refs)
            if row_no not in main_status:
                continue
            _, onsite = main_status[row_no]
            if onsite == "否" and (m.active_projects or []):
                samples.append(f"{m.display_name}（状态=否，清除项目：{str(m.active_projects[0])[:30]}…）")
                if not dry_run:
                    m.active_projects = None
                    api_service.audit(session, actor="onsite-fix",
                                      action="enterprise.manager.onsite_fix",
                                      basis=f"主表在施状态=否，项目名不采纳（{m.display_name}）",
                                      outcome="active_projects cleared", object_ref=m.manager_id)
                mgr_fixed += 1
        personnel = session.scalars(
            select(Personnel).where(Personnel.category == "registered_builder")
        ).all()
        for p in personnel:
            row_no = _row_of(p.evidence_refs)
            if row_no not in main_status:
                continue
            name, onsite = main_status[row_no]
            if onsite == "否" and p.on_site_project:
                if not dry_run:
                    p.on_site_project = None
                    api_service.audit(session, actor="onsite-fix",
                                      action="enterprise.personnel.onsite_fix",
                                      basis=f"主表在施状态=否，项目名不采纳（{name}）",
                                      outcome="on_site_project cleared", object_ref=p.personnel_id)
                pnl_fixed += 1
        if not dry_run:
            api_service.audit(session, actor="onsite-fix",
                              action="enterprise.onsite_conflict_fix",
                              basis="2026-09-22 项目经理M实测：状态=否却登记在施项目；以主表在施状态为准（147 经理/140 人员）",
                              outcome=f"managers={mgr_fixed} personnel={pnl_fixed}",
                              object_ref="managers+personnel")
            session.commit()
    for s in samples[:5]:
        print(" ", s)
    print(f"\n完成：经理修复 {mgr_fixed} 条、人员修复 {pnl_fixed} 条"
          f"{'（dry-run，未写库）' if dry_run else ''}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
