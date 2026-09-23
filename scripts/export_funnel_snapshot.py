#!/usr/bin/env python
"""导出本机「搜索漏斗 + 准入状态」全量快照供云端演示环境导入（2026-09-23 业主指令：同步上云）。

覆盖（云端整表替换，达到与本机一致）：
  public_data: projects / announcement_candidates / selection_pool_items /
               materials / material_versions / parse_candidates / field_traces /
               project_identities
  admission_data: rule_sets / requirements / match_runs / match_items /
               admission_results / approvals / waivers / quick_prescreen_runs /
               preparation_records / remediation_tasks / discovery_rule_profiles
  public: analysis_jobs（搜索任务历史）

不触碰（云端运行时自有状态）：enterprise_data（已由 export_enterprise_snapshot.py
同步）、audit_data（云端自身审计）、worker_heartbeats、knowledge_data（可重建索引）。
漏斗数据不含自然人姓名，无需脱敏；对象文件（objects/public + objects/enterprise）
另行 rsync 整目录同步。幂等（TRUNCATE + INSERT，可重跑）。

用法：DATABASE_URL=... python scripts/export_funnel_snapshot.py out.sql
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 复用企业快照脚本的 SQL 字面量/插值工具（scripts 非包，按文件加载）
_spec = importlib.util.spec_from_file_location(
    "_ent_snap", Path(__file__).resolve().parent / "export_enterprise_snapshot.py")
_ent = importlib.util.module_from_spec(_spec)
sys.modules["_ent_snap"] = _ent
_spec.loader.exec_module(_ent)

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from runtime.db.models import (  # noqa: E402
    AnalysisJob,
    AdmissionResult,
    AnnouncementCandidate,
    Approval,
    FieldTrace,
    MatchItem,
    MatchRun,
    ParseCandidate,
    PreparationRecord,
    Project,
    ProjectIdentity,
    QuickPrescreenRun,
    RemediationTask,
    Requirement,
    Material,
    MaterialVersion,
    RuleSet,
    SelectionPoolItem,
    Waiver,
)
from runtime.db.models import DiscoveryRuleProfile  # noqa: E402

# (模型, 表名) —— TRUNCATE 顺序与 INSERT 一致；无外键约束，顺序仅为可读
TABLES = [
    (Project, "public_data.projects"),
    (AnnouncementCandidate, "public_data.announcement_candidates"),
    (SelectionPoolItem, "public_data.selection_pool_items"),
    (Material, "public_data.materials"),
    (MaterialVersion, "public_data.material_versions"),
    (ParseCandidate, "public_data.parse_candidates"),
    (FieldTrace, "public_data.field_traces"),
    (ProjectIdentity, "public_data.project_identities"),
    (RuleSet, "admission_data.rule_sets"),
    (Requirement, "admission_data.requirements"),
    (MatchRun, "admission_data.match_runs"),
    (MatchItem, "admission_data.match_items"),
    (AdmissionResult, "admission_data.admission_results"),
    (Approval, "admission_data.approvals"),
    (Waiver, "admission_data.waivers"),
    (QuickPrescreenRun, "admission_data.quick_prescreen_runs"),
    (PreparationRecord, "admission_data.preparation_records"),
    (RemediationTask, "admission_data.remediation_tasks"),
    (DiscoveryRuleProfile, "admission_data.discovery_rule_profiles"),
    (AnalysisJob, "analysis_jobs"),
]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("缺少 DATABASE_URL")
        return 2
    sql_path = Path(sys.argv[1])
    engine = create_engine(database_url)
    statements: list[str] = []
    counts: dict[str, int] = {}
    with Session(engine) as session:
        for model, table in TABLES:
            columns = [c.name for c in model.__table__.columns]
            n = 0
            for r in session.scalars(select(model)).all():
                row = {c: getattr(r, c) for c in columns}
                statements.append(_ent.row_insert(table, columns, row))
                n += 1
            counts[table.split(".")[-1]] = n
    truncate_list = ", ".join(t for _, t in TABLES)
    sql = (
        "BEGIN;\n"
        f"-- 漏斗+准入状态全量快照（来源：本机 bid_agent 库，export_funnel_snapshot.py）\n"
        f"TRUNCATE {truncate_list};\n"
        + "\n".join(statements) + "\nCOMMIT;\n"
    )
    sql_path.write_text(sql, encoding="utf-8")
    print(f"导出完成 → {sql_path}")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
