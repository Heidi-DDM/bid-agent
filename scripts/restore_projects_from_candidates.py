#!/usr/bin/env python
"""修复 2026-09-22 集成测试误清开发库：从候选表重建 projects 行与公告材料。

背景：runtime/tests 的真库集成 fixture 会 TRUNCATE public_data.projects /
materials 等表。2026-09-22 曾把 DATABASE_URL 指向开发库 bid_agent 执行集成套件，
用户已建档的 PJ-* 项目、公告/招标材料与匹配审批数据被清空（announcement_candidates
与待选池未受影响，仍引用这些 project_id，导致「上传招标文件」页 404 空态）。

本脚本只做**事实恢复**：
- projects 行：project_id / project_name=候选标题 / bid_deadline（仅当候选
  detail_summary.deadline_bid 有可解析日期，不推断）；
- 公告材料：对象库 objects/public/MAT-{candidate_id}/ 存有原文文件时，
  用既有导入服务重放（幂等），parse_status 恢复为 parsed（候选 detail_summary
  存在即证明初筛曾完成）；
- 无法恢复（如实报告，不伪造）：用户上传的招标文件材料行（需在页面重传，
  同内容重传幂等复用对象）、match_runs/admission_results/approvals（重跑匹配即重建）。

用法：
  DATABASE_URL=postgresql+psycopg://... python scripts/restore_projects_from_candidates.py [--dry-run]
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from runtime.core.config import object_store_root  # noqa: E402
from runtime.db import api_service, material_service  # noqa: E402
from runtime.db.models import AnnouncementCandidate, Material, Project  # noqa: E402


def _summary_deadline(candidate: AnnouncementCandidate) -> date | None:
    """仅当候选详情初筛给出可解析的投标截止日期时返回（不推断）。"""
    field = (candidate.detail_summary or {}).get("deadline_bid") or {}
    value = field.get("value") if isinstance(field, dict) else None
    if not value:
        return None
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("缺少 DATABASE_URL")
        return 2
    dry_run = "--dry-run" in sys.argv
    engine = create_engine(database_url)
    store_root = str(object_store_root())
    restored_projects, restored_materials, missing_projects = 0, 0, []
    with Session(engine) as session:
        candidates = session.scalars(
            select(AnnouncementCandidate).where(
                AnnouncementCandidate.project_id.is_not(None),
                AnnouncementCandidate.import_status == "imported",
            )
        ).all()
        for c in candidates:
            project = session.get(Project, c.project_id)
            if project is None:
                print(f"[project] 重建 {c.project_id} ← 「{c.title[:40]}」")
                if not dry_run:
                    project = Project(project_id=c.project_id, project_name=c.title[:250])
                    deadline = _summary_deadline(c)
                    if deadline is not None:
                        project.bid_deadline = deadline
                    session.add(project)
                    api_service.audit(
                        session, actor="restore-script", action="project.restore",
                        basis="2026-09-22 集成测试误清开发库；从 announcement_candidates 事实重建",
                        outcome=f"restored deadline={deadline or 'unknown'}", object_ref=c.project_id,
                    )
                restored_projects += 1
            # 公告材料：对象文件存在则重放导入（同内容幂等）
            mat_id = f"MAT-{c.candidate_id}"
            existing_mat = session.scalars(
                select(Material).where(Material.material_id == mat_id)
            ).first()
            obj_file = next(iter(sorted(Path(store_root, "public", mat_id).glob("*/*"))), None) \
                if Path(store_root, "public", mat_id).is_dir() else None
            if existing_mat is None and obj_file is not None:
                print(f"[material] 重放公告材料 {mat_id}（对象 {obj_file.name[:12]}…）")
                if not dry_run:
                    imported = material_service.import_material(
                        session, src_path=str(obj_file), material_id=mat_id,
                        material_type="announcement", source_type="official_platform",
                        owner_type="public", classification="public",
                        permission_scope="public_read", data_owner="restore-script",
                        store_root=store_root, project_id=c.project_id,
                        actor="restore-script",
                    )
                    # 候选 detail_summary 存在即证明初筛完成过 → 恢复 parsed（事实恢复）
                    imported.material.parse_status = "parsed"
                    api_service.audit(
                        session, actor="restore-script", action="material.restore",
                        basis="对象库原文文件重放导入（清库后修复）",
                        outcome=f"{mat_id} parse_status=parsed", object_ref=mat_id,
                    )
                restored_materials += 1
            elif existing_mat is None and obj_file is None:
                missing_projects.append((c.project_id, c.candidate_id))
        if not dry_run:
            session.commit()
    print(f"\n完成：重建项目 {restored_projects} 个、公告材料 {restored_materials} 份"
          f"{'（dry-run，未写库）' if dry_run else ''}。")
    if missing_projects:
        print("以下项目未找到公告原文对象（材料不可恢复，需页面重传招标文件）：")
        for pid, cid in missing_projects:
            print(f"  - {pid}（候选 {cid}）")
    print("不可恢复（不伪造）：用户上传的招标文件材料行（重传幂等复用对象）、"
          "匹配运行/准入结果/审批记录（重跑匹配重建）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
