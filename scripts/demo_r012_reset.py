#!/usr/bin/env python3
"""R012 演示数据重置：ND-2025 回到「招标文件已推送、未上传」的干净起点。

用途：浏览器 7 步旅程（上传→解析→复核→确认→自动匹配→风险→补录重算→审批）
全链重走前调用。幂等，可重复执行。

删除（ND-2025 运行产物）：
  - public_data.materials: MAT-ND-TENDER（L2 招标载体）、MAT-A/MAT-B（R021 合成残留）
  - public_data.material_versions: 上述 material 的版本行（必须同步删——漏删会导致重传同 PDF 时
    import_material 幂等分支命中旧 content_hash（decision.created=False）但 material 行已删 →
    ImportedMaterial.material=None → /intake/tender-document 500 AttributeError（2026-09-03 实测）
  - knowledge_data.knowledge_chunks: 上述 material 的 L2 残留
  - public_data.parse_candidates: project_id='ND-2025'
  - admission_data.rule_sets / requirements + public_data.field_traces:
    confirm 产物（RS-MAT-ND-TENDER-v1 等；金标准 RS-ND-2025-1.0.0 保留不删）——漏删会导致
    重走旅程 confirm 409「规则集已存在」（2026-09-03 实测：E2E 撞键修复后第二轮旅程 Step 3 FAIL）
  - admission_data.match_items / match_runs / admission_results: project_id='ND-2025'
  - public.analysis_jobs: project_id='ND-2025'（幂等键释放，重走旅程会重建历史）
  - public_data.projects.admission_status -> NULL（复位无状态）

保留（演示数据底座，勿删）：
  - enterprise_data 5 表（13 条脱敏证据 active + verified_at，R022 演示根基）
  - MAT-ND-L3-* ×12（L3 索引载体）及其 chunks/jobs
  - 金标准规则集 RS-ND-2025-1.0.0（20 条，R025 验收载体；与 confirm 产物 RS-MAT-<MID>-v<N> ID 不冲突）
  - runtime/objects 原文（重传同 sha256 幂等）

验证：重置后 psql 检查 match_runs=0、MAT-ND-TENDER 不存在、projects.admission_status 空。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / "runtime" / ".env"

PROJECT = "ND-2025"
MATERIALS_TO_DROP = ("MAT-ND-TENDER", "MAT-A", "MAT-B")


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        m = re.search(r"^DATABASE_URL=(.+)$", ENV_FILE.read_text(encoding="utf-8"), re.M)
        if not m:
            sys.exit("未找到 DATABASE_URL（env 或 runtime/.env）")
        url = m.group(1).strip().strip('"')
    # SQLAlchemy 方言前缀（postgresql+psycopg://）psycopg 直连不认 → 归一
    return re.sub(r"^postgresql\+psycopg://", "postgresql://", url)


def main() -> None:
    try:
        import psycopg
    except ImportError:
        sys.exit("需要 psycopg（项目 .venv 已装）")
    conn = psycopg.connect(database_url(), autocommit=True)
    cur = conn.cursor()
    before: dict[str, int] = {}

    def count(sql: str, params: tuple) -> int:
        cur.execute(sql, params)
        return cur.fetchone()[0]

    def drop(sql: str, params: tuple) -> int:
        cur.execute(sql, params)
        return cur.rowcount

    # 计数（执行前）
    before["match_runs"] = count(
        "SELECT count(*) FROM admission_data.match_runs WHERE project_id=%s", (PROJECT,))
    before["match_items"] = count(
        "SELECT count(*) FROM admission_data.match_items WHERE run_id IN "
        "(SELECT run_id FROM admission_data.match_runs WHERE project_id=%s)", (PROJECT,))
    before["admission_results"] = count(
        "SELECT count(*) FROM admission_data.admission_results WHERE project_id=%s", (PROJECT,))
    before["materials"] = count(
        "SELECT count(*) FROM public_data.materials WHERE material_id = ANY(%s)",
        (list(MATERIALS_TO_DROP),))
    before["chunks"] = count(
        "SELECT count(*) FROM knowledge_data.knowledge_chunks WHERE material_id = ANY(%s)",
        (list(MATERIALS_TO_DROP),))
    before["material_versions"] = count(
        "SELECT count(*) FROM public_data.material_versions WHERE material_id = ANY(%s)",
        (list(MATERIALS_TO_DROP),))
    before["parse_candidates"] = count(
        "SELECT count(*) FROM public_data.parse_candidates WHERE project_id=%s", (PROJECT,))
    # confirm 产物规则集命名 = RS-<material_id>-v<version>（parse_service.material_ruleset_id）
    rs_patterns = tuple(f"RS-{mid}-%" for mid in MATERIALS_TO_DROP)
    ft_patterns = tuple(f"{mid}:%" for mid in MATERIALS_TO_DROP)
    before["confirm_rule_sets"] = count(
        "SELECT count(*) FROM admission_data.rule_sets WHERE rule_set_id LIKE ANY(%s)", (list(rs_patterns),))
    before["confirm_requirements"] = count(
        "SELECT count(*) FROM admission_data.requirements WHERE rule_set_id LIKE ANY(%s)", (list(rs_patterns),))
    before["confirm_field_traces"] = count(
        "SELECT count(*) FROM public_data.field_traces WHERE object_id LIKE ANY(%s)", (list(ft_patterns),))
    before["jobs"] = count(
        "SELECT count(*) FROM public.analysis_jobs WHERE project_id=%s", (PROJECT,))

    # 删除
    n_items = drop(
        "DELETE FROM admission_data.match_items WHERE run_id IN "
        "(SELECT run_id FROM admission_data.match_runs WHERE project_id=%s)", (PROJECT,))
    n_runs = drop(
        "DELETE FROM admission_data.match_runs WHERE project_id=%s", (PROJECT,))
    n_adm = drop(
        "DELETE FROM admission_data.admission_results WHERE project_id=%s", (PROJECT,))
    n_mat = drop(
        "DELETE FROM public_data.materials WHERE material_id = ANY(%s)",
        (list(MATERIALS_TO_DROP),))
    n_versions = drop(
        "DELETE FROM public_data.material_versions WHERE material_id = ANY(%s)",
        (list(MATERIALS_TO_DROP),))
    n_chunks = drop(
        "DELETE FROM knowledge_data.knowledge_chunks WHERE material_id = ANY(%s)",
        (list(MATERIALS_TO_DROP),))
    n_cand = drop(
        "DELETE FROM public_data.parse_candidates WHERE project_id=%s", (PROJECT,))
    n_crs = drop(
        "DELETE FROM admission_data.requirements WHERE rule_set_id LIKE ANY(%s)", (list(rs_patterns),))
    n_cft = drop(
        "DELETE FROM public_data.field_traces WHERE object_id LIKE ANY(%s)", (list(ft_patterns),))
    n_crsets = drop(
        "DELETE FROM admission_data.rule_sets WHERE rule_set_id LIKE ANY(%s)", (list(rs_patterns),))
    n_jobs = drop(
        "DELETE FROM public.analysis_jobs WHERE project_id=%s", (PROJECT,))
    n_proj = drop(
        "UPDATE public_data.projects SET admission_status=NULL WHERE project_id=%s", (PROJECT,))

    print("=== R012 演示数据重置完成 ===")
    print(f"项目: {PROJECT}")
    for k, v in before.items():
        print(f"  清理前 {k}: {v}")
    print(f"  删除 match_items={n_items} match_runs={n_runs} admission_results={n_adm}")
    print(f"  删除 materials={n_mat} versions={n_versions} chunks={n_chunks} candidates={n_cand} jobs={n_jobs}")
    print(f"  删除 confirm 产物: rule_sets={n_crsets} requirements={n_crs} field_traces={n_cft}")
    print(f"  复位 projects.admission_status 行数={n_proj}")
    print("保留: enterprise_data 13 条 / MAT-ND-L3-* / RS-ND-2025-1.0.0 金标准 / objects 原文")
    conn.close()


if __name__ == "__main__":
    main()
