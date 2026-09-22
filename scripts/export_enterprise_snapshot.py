#!/usr/bin/env python
"""导出本机企业资料全量快照（人名脱敏）供云端演示环境导入（2026-09-22 业主口径变更）。

业主要求：云端除**人名脱敏**外，其余必须与本机一致。本脚本从本机库导出
enterprise_data 五表全量行 + enterprise 侧 materials/material_versions，输出：
  1. SQL 文件（BEGIN + TRUNCATE 目标表 + INSERT + COMMIT，可重复执行）；
  2. 对象文件清单（stdout 列出需一并拷贝的 runtime/objects 相对路径）。

脱敏规则（仅人名，其余字段逐字一致）：
  - personnel.name：首字保留，其余每字一个 *（张伟→张*，李海锋→李**）；
  - managers.display_name：「姓名·行号」→「脱敏名·行号」；非中文姓名（如 M-ND-01）原样；
  - `__待补__` 哨兵原样。
用法：
  DATABASE_URL=... python scripts/export_enterprise_snapshot.py out.sql [--manifest manifest.txt]
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from runtime.db.models import (  # noqa: E402
    EvidenceFile,
    Manager,
    Material,
    MaterialVersion,
    Performance,
    Personnel,
    Qualification,
)

NAME_RE = re.compile(r"^([\u4e00-\u9fa5·]{2,5})(·\d{1,5})?$")


def mask_name(name: str | None) -> str | None:
    if not name or name == "__待补__":
        return name
    m = NAME_RE.match(str(name))
    if not m:
        return name  # 非人名格式（演示 ID 等）原样
    base, suffix = m.group(1), m.group(2) or ""
    masked = base[0] + "*" * (len(base) - 1)
    return f"{masked}{suffix}"


def q(value) -> str:
    """SQL 字面量（NULL/数字/字符串/JSON 数组对象）。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (date, datetime)):
        return f"'{value.isoformat()}'"
    if isinstance(value, (list, dict)):
        return "'" + json.dumps(value, ensure_ascii=False).replace("'", "''") + "'::jsonb"
    return "'" + str(value).replace("'", "''") + "'"


def row_insert(table: str, columns: list[str], row: dict) -> str:
    return (f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
            f"({', '.join(q(row[c]) for c in columns)});")


def dump_table(session, model, table: str, *, transform=None) -> list[str]:
    columns = [c.name for c in model.__table__.columns]
    out = []
    for r in session.scalars(select(model)).all():
        row = {c: getattr(r, c) for c in columns}
        if transform:
            transform(row)
        out.append(row_insert(table, columns, row))
    return out


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
    manifest: set[str] = set()
    with Session(engine) as session:
        statements += dump_table(session, Qualification, "enterprise_data.qualifications")
        statements += dump_table(session, Performance, "enterprise_data.performances")
        statements += dump_table(
            session, Manager, "enterprise_data.managers",
            transform=lambda row: row.update(display_name=mask_name(row["display_name"])))
        statements += dump_table(
            session, Personnel, "enterprise_data.personnel",
            transform=lambda row: row.update(name=mask_name(row["name"])))
        for e in session.scalars(select(EvidenceFile)).all():
            if e.object_uri:
                manifest.add(e.object_uri)
        statements += dump_table(session, EvidenceFile, "enterprise_data.evidence_files")
        # enterprise 侧材料与其版本（当前本机仅 1 行演示证据材料；如实复制）
        ent_mats = session.scalars(
            select(Material).where(Material.owner_type == "enterprise")).all()
        if ent_mats:
            ids = [m.material_id for m in ent_mats]
            columns = [c.name for c in Material.__table__.columns]
            for m in ent_mats:
                row = {c: getattr(m, c) for c in columns}
                statements.append(row_insert("public_data.materials", columns, row))
            for mv in session.scalars(
                select(MaterialVersion).where(MaterialVersion.material_id.in_(ids))
            ).all():
                columns = [c.name for c in MaterialVersion.__table__.columns]
                row = {c: getattr(mv, c) for c in columns}
                statements.append(row_insert("public_data.material_versions", columns, row))
                if mv.object_uri:
                    manifest.add(mv.object_uri)

    body = "\n".join(statements)
    sql = (
        "BEGIN;\n"
        "-- 企业资料全量快照（人名脱敏；来源：本机 bid_agent 库，export_enterprise_snapshot.py）\n"
        "TRUNCATE enterprise_data.qualifications, enterprise_data.performances, "
        "enterprise_data.managers, enterprise_data.personnel, enterprise_data.evidence_files;\n"
        "DELETE FROM public_data.materials WHERE owner_type = 'enterprise';\n"
        "DELETE FROM public_data.material_versions WHERE material_id NOT IN "
        "(SELECT material_id FROM public_data.materials);\n"
        + body + "\nCOMMIT;\n"
    )
    sql_path.write_text(sql, encoding="utf-8")
    manifest_path = sql_path.with_suffix(".manifest.txt")
    manifest_path.write_text("\n".join(sorted(manifest)) + "\n", encoding="utf-8")
    counts = {
        "qualifications": sql.count("INSERT INTO enterprise_data.qualifications"),
        "performances": sql.count("INSERT INTO enterprise_data.performances"),
        "managers": sql.count("INSERT INTO enterprise_data.managers"),
        "personnel": sql.count("INSERT INTO enterprise_data.personnel"),
        "evidence_files": sql.count("INSERT INTO enterprise_data.evidence_files"),
        "objects": len(manifest),
    }
    print(f"导出完成 → {sql_path}（{counts}）；对象清单 → {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
