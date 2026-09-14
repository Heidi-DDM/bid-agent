#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""企业私有资料全量入库（离线脚本，2026-09-14；F006 §6.7 / F022 §2 / F019 §4）。

把 `企业资料台账20260821/`（.gitignore 排除，本脚本只读它）的全部资料通过**既有**
导入服务落库——不绕过任何门禁：
  A. 台账（7 份）→ 结构化记录（幂等，默认 pending_verification，由数据管理员核验后 active）
     建造师 xlsx(首表 + Sheet1 在施信息合并) → personnel(registered_builder) + managers
     职称 xlsx → personnel(technical_title)；岗位证书 xlsx(「岗位证书清单」表) → personnel(post_certificate)
     竣工业绩 xlsx → performances；资质/安许+信用/CA 三份 .xls（先经 Excel 转存 .xlsx，见 --xls-dir）→ qualifications
     每份台账原件同时作为不可变企业材料（MAT-LEDGER-*）落对象库并回链 material_id / ledger 行号。
  B. 证据扫描件（资质 6 + 安许信用 2 + 典型项目合同验收 5）→ Material(qualification_cert/performance_record)
     + evidence_files 元数据；资质行 evidence_refs 回链扫描件（按台账「扫描件文件名」列）。
  C. 历史招投标文件（目录 8/9 全部文件）→ Material(tender_document / evidence_file，enterprise/confidential)；
     同内容不同路径只存一份（按 sha256 去重）；> 200MB（F019 §4 对象上限）如实登记为「超限未入库」，不放宽。

不推断、不编造：占位符「后续补充/不用填」→ __待补__；日期越界 → NULL（待核实）；岗位证书「有效期」
按 2026-08-27 公司口径长期有效 → NULL 且在 evidence_refs 留痕。所有动作写审计（既有服务负责）。

用法：
  .venv/bin/python scripts/import_enterprise_ledgers.py --dry-run          # 只解析、报数，不写库
  .venv/bin/python scripts/import_enterprise_ledgers.py                    # 全量入库（幂等，可重跑）
  .venv/bin/python scripts/import_enterprise_ledgers.py --only ledgers     # ledgers | evidence | bidbooks
报告写入 runtime/.tmp-import/import_report_<ts>.json（gitignore），stdout 只打印计数。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openpyxl import load_workbook  # noqa: E402

BASE = ROOT / "企业资料台账20260821"
XLS_DIR_DEFAULT = ROOT / "runtime" / ".tmp-import"
DATA_OWNER_DEFAULT = "某建设集团（20260821 资料包提供方，责任人待指定）"
ACTOR_DEFAULT = "import.script:enterprise_ledgers_20260821"

F_BUILDER = BASE / "3.注册建造师清单（含在施状态)未完善" / "3.注册建造师清单（含在施状态）.xlsx"
F_TITLE = BASE / "4.技术职称人员清单" / "技术职称人员清单.xlsx"
F_POST = BASE / "5.岗位证书清单" / "岗位证书清单.xlsx"
F_PERF = BASE / "6.已完工项目清单（结构化业绩库）未完善" / "近五年竣工业绩.xlsx"
F_QUAL_XLS = BASE / "1.资质证书清单及扫描件" / "清单.xls"
F_SAFE_XLS = BASE / "2.安全生产许可证 + 信用评级" / "清单.xls"
F_CA_XLS = BASE / "10.CA证书信息.xls"
D_QUAL_SCANS = BASE / "1.资质证书清单及扫描件"
D_SAFE_SCANS = BASE / "2.安全生产许可证 + 信用评级"
D_PERF_EVID = BASE / "7.中标合同+验收文件（典型项目）"
D_BIDBOOK_8 = BASE / "8.典型招标投标文件（工民建）未完善"
D_BIDBOOK_9 = BASE / "9.历史中标标书（各类型5-10份）"

MAX_OBJECT_BYTES = 200 * 1024 * 1024


# ── 工具 ─────────────────────────────────────────────────────────────


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    return str(v).strip()


def _sheet_rows(path: Path, sheet: str | int | None = None) -> tuple[list[str], list[list[str]]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0] if sheet is None else (wb.worksheets[sheet] if isinstance(sheet, int) else wb[sheet])
        it = ws.iter_rows(values_only=True)
        headers: list[str] = []
        for raw in it:  # 跳过前导空行（安许清单表头在第 2 行）
            headers = [_cell(h) for h in (raw or [])]
            if any(headers):
                break
        rows = [[_cell(v) for v in r] for r in it]
        return headers, [r for r in rows if any(r)]
    finally:
        wb.close()


def _col(headers: list[str], *names: str) -> int | None:
    norm = [h.replace(" ", "") for h in headers]
    for n in names:
        for i, h in enumerate(norm):
            if h.startswith(n):
                return i
    return None


def _get(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return row[idx]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _slug_id(prefix: str, *parts: str) -> str:
    return f"{prefix}-{hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()[:12]}"


def _mask(name: str) -> str:
    name = (name or "").strip()
    return name if len(name) <= 1 else name[0] + "*" * (len(name) - 1)


_QUAL_FAMILY = (
    ("施工总承包", "施工总承包"), ("专业承包", "专业承包"), ("工程设计", "工程设计"),
    ("公路养护", "公路养护"), ("安全生产许可证", "安全生产许可"), ("信用等级", "企业信用"), ("CA", "数字证书"),
)


def _num_or_none(v: str) -> str | None:
    """金额/规模文本 → 纯数值串；解析不出 → None（待补，不推断）。"""
    s = (v or "").replace("，", "").replace(",", "").strip()
    m = re.search(r"-?[0-9]+(?:\.[0-9]+)?", s)
    return m.group(0) if m else None


def _qual_family(name: str) -> str:
    for kw, fam in _QUAL_FAMILY:
        if kw in name:
            return fam
    return "__待补__"


# ── 台账行构造（纯函数，dry-run 可验）──────────────────────────────────


def build_builder_rows() -> tuple[list[dict], list[dict], dict]:
    """建造师首表 + Sheet1 在施信息 → (personnel rows, manager rows, stats)。"""
    h, rows = _sheet_rows(F_BUILDER, 0)
    c_name = _col(h, "姓名")
    spec_cols = [i for i, x in enumerate(h) if x.endswith("专业")]
    c_level, c_phone, c_reg = _col(h, "等级"), _col(h, "联系电话"), _col(h, "注册编号")
    c_onsite, c_proj = _col(h, "在施状态"), _col(h, "在施项目名称")
    # 第 19 列（index 18）无表头，实测为所属区域/单位（如「云贵」）
    # Sheet1：姓名 → 在施项目名称（补首表空缺）
    onsite_map: dict[str, str] = {}
    try:
        h1, rows1 = _sheet_rows(F_BUILDER, "Sheet1")
        n1, p1 = _col(h1, "姓名"), _col(h1, "在施状态")  # Sheet1 实测第 2 列即项目名（表头错位）
        for r in rows1:
            nm, pj = _get(r, n1), _get(r, p1)
            if nm and pj and not re.search(r"后续补充|不用填", pj) and not re.fullmatch(r"[是否]", pj):
                onsite_map.setdefault(nm, pj)
    except Exception:
        pass
    personnel, managers = [], []
    no_level = 0
    for i, r in enumerate(rows, start=2):
        name = _get(r, c_name)
        if not name:
            continue
        specs = [r[j] for j in spec_cols if j < len(r) and r[j]]
        specialty = "/".join(dict.fromkeys(specs)) or None
        level = _get(r, c_level)
        if re.search(r"后续补充|不用填|待补", level):
            level = ""
        onsite_raw = _get(r, c_onsite)
        project = _get(r, c_proj) or onsite_map.get(name, "")
        org = r[18].strip() if len(r) > 18 and r[18] else None
        ref = f"ledger:xlsx:sheet0:row{i}"
        personnel.append({
            "name": name, "organization": org, "specialty": specialty, "cert_level": level or None,
            "cert_no": _get(r, c_reg) or None, "phone": _get(r, c_phone) or None,
            "on_site": onsite_raw or None, "on_site_project": project or None,
            "evidence_refs": [ref] + ([f"ledger:xlsx:Sheet1:onsite"] if name in onsite_map else []),
        })
        avail = "occupied" if onsite_raw.startswith("是") else ("available" if onsite_raw.startswith("否") else None)
        if project and avail is None:
            avail = "occupied"
        if not level:
            no_level += 1  # 无等级不造 Manager（服务层缺省「一级建造师」属推断，禁止）；仍入 personnel
            continue
        managers.append({
            "display_name": f"{_mask(name)}·{i:04d}",  # 脱敏 + 台账行号消歧（F006 §4.3 / F022 §4）
            "organization": org or "某建设集团", "specialty": specialty,
            "reg_cert_type": level or None, "reg_cert_no": _get(r, c_reg) or None,
            "availability": avail, "active_projects": [project] if project else [],
            "evidence_refs": [ref],
        })
    stats = {"rows": len(rows), "personnel": len(personnel), "managers": len(managers), "managers_skipped_no_level": no_level,
             "onsite_from_sheet1": sum(1 for p in personnel if p["on_site_project"] and "Sheet1" in " ".join(p["evidence_refs"]))}
    return personnel, managers, stats


def build_title_rows() -> tuple[list[dict], dict]:
    h, rows = _sheet_rows(F_TITLE, 0)
    c_name, c_spec, c_level, c_no, c_date = _col(h, "姓名"), _col(h, "专业方向"), _col(h, "职称级别"), _col(h, "职称证书编号"), _col(h, "发证日期")
    out = []
    for i, r in enumerate(rows, start=2):
        name = _get(r, c_name)
        if not name:
            continue
        out.append({"name": name, "specialty": _get(r, c_spec) or None, "cert_level": _get(r, c_level) or None,
                    "cert_no": _get(r, c_no) or None, "valid_from": _get(r, c_date) or None,
                    "evidence_refs": [f"ledger:xlsx:sheet0:row{i}"]})
    return out, {"rows": len(rows), "personnel": len(out)}


def build_post_cert_rows() -> tuple[list[dict], dict]:
    h, rows = _sheet_rows(F_POST, "岗位证书清单")
    c_name, c_type, c_no, c_valid, c_issuer = _col(h, "姓名"), _col(h, "证书类型"), _col(h, "证书编号"), _col(h, "有效期"), _col(h, "发证机关")
    out = []
    for i, r in enumerate(rows, start=2):
        name = _get(r, c_name)
        if not name:
            continue
        valid_raw = _get(r, c_valid)
        refs = [f"ledger:xlsx:sheet岗位证书清单:row{i}"]
        if valid_raw and re.search(r"后续解决|不会过期|定时", valid_raw):
            refs.append("valid_until:长期有效（2026-08-27 公司口径：各单位定时培训考试，不设到期）")
            valid_raw = ""
        out.append({"name": name, "specialty": _get(r, c_type) or None, "cert_no": _get(r, c_no) or None,
                    "valid_until": valid_raw or None, "issuer": _get(r, c_issuer) or None, "evidence_refs": refs})
    return out, {"rows": len(rows), "personnel": len(out)}


def build_performance_rows(perf_evidence: dict[str, str]) -> tuple[list[dict], dict]:
    """perf_evidence: 典型项目证据 PDF 文件名 → material_id。按文件名前 8 字匹配项目名，多命中取最短
    （最接近）者；台账无对应项目的证据记入 unlinked（真实缺口：需公司补业绩台账，不推断）。"""
    h, rows = _sheet_rows(F_PERF, 0)
    c_name, c_type, c_amt, c_scale = _col(h, "项目名称"), _col(h, "类型"), _col(h, "合同金额"), _col(h, "规模")
    c_owner, c_start, c_end, c_pm, c_scan = _col(h, "业主名称"), _col(h, "开工日期"), _col(h, "竣工日期"), _col(h, "负责项目经理"), _col(h, "合同扫描件文件名")
    c_branch = 9 if len(h) > 9 else None  # 第 10 列无表头，实测为承建分公司（石家庄/一分/建安/内蒙/钢结构…）
    def _k(x: str) -> str:
        return re.sub(r"[()（）\s]", "", x or "")
    names = [_get(r, c_name) for r in rows]
    evid_for_name: dict[str, list[tuple[str, str]]] = {}
    unlinked: list[str] = []
    for fname, mid in perf_evidence.items():
        key = _k(Path(fname).stem)[:8]
        hits = [n for n in names if key and key in _k(n)]
        if not hits:
            unlinked.append(fname)
            continue
        best = min(hits, key=len)
        evid_for_name.setdefault(best, []).append((fname, mid))
    out, linked = [], 0
    for i, r in enumerate(rows, start=2):
        name = _get(r, c_name)
        if not name:
            continue
        refs = [f"ledger:xlsx:sheet0:row{i}"]
        scan = _get(r, c_scan)
        if scan:
            refs.append(f"contract_scan:{scan}")
        for fname, mid in evid_for_name.get(name, []):
            refs.append(f"material:{mid}")
            refs.append(f"7.中标合同+验收文件（典型项目）/{fname}")
            linked += 1
        metrics = {}
        branch = _get(r, c_branch)
        pm = _get(r, c_pm)
        if branch:
            metrics["branch"] = branch
        if pm:
            metrics["project_manager"] = pm
        out.append({"project_name": name, "project_type": _get(r, c_type) or None,
                    "contract_amount_wan": _num_or_none(_get(r, c_amt)), "scale": _num_or_none(_get(r, c_scale)),
                    "owner_org": _get(r, c_owner) or None, "awarded_at": _get(r, c_start) or None,
                    "completed_at": _get(r, c_end) or None, "scale_metrics": metrics or None,
                    "evidence_refs": refs})
    return out, {"rows": len(rows), "performances": len(out), "evidence_linked_rows": linked,
                 "evidence_unlinked_no_ledger_row": unlinked,
                 "contract_scan_named": sum(1 for x in out if any(s.startswith("contract_scan:") for s in x["evidence_refs"]))}


def build_qualification_rows(xls_dir: Path, scan_evidence: dict[str, str]) -> tuple[list[dict], dict]:
    """三份转存 .xlsx → qualifications 行。scan_evidence: 「1.资质证书清单及扫描件/N.pdf」→ material_id。"""
    out = []
    # 资质证书（25 行）
    h, rows = _sheet_rows(xls_dir / "资质证书清单.xlsx", 0)
    c_no, c_name, c_level, c_issuer, c_cert, c_until, c_scan = (_col(h, "序号"), _col(h, "证书名称"), _col(h, "等级"),
                                                                 _col(h, "发证机关"), _col(h, "证书编号"), _col(h, "有效期截止日"), _col(h, "扫描件文件名"))
    for i, r in enumerate(rows, start=2):
        name = _get(r, c_name)
        if not name:
            continue
        refs = [f"ledger:xls→xlsx:资质证书清单:row{i}", f"cert_no:{_get(r, c_cert)}"]
        scan = _get(r, c_scan)
        if scan:
            key = f"1.资质证书清单及扫描件/{scan}.pdf"
            refs.append(key)
            if key in scan_evidence:
                refs.append(f"material:{scan_evidence[key]}")
        out.append({"category": name, "level": _get(r, c_level) or None, "specialty": _qual_family(name),
                    "issuer": _get(r, c_issuer) or None, "valid_until": _get(r, c_until) or None, "evidence_refs": refs})
    n_qual = len(out)
    # 安许证 + 企业信用等级
    h, rows = _sheet_rows(xls_dir / "安许与信用评级清单.xlsx", 0)
    c_type, c_cert, c_until, c_grade, c_from = _col(h, "证书类型"), _col(h, "证书编号"), _col(h, "有效期"), _col(h, "评级等级"), _col(h, "发证日期")
    for i, r in enumerate(rows, start=2):
        name = _get(r, c_type)
        if not name:
            continue
        refs = [f"ledger:xls→xlsx:安许与信用评级清单:row{i}", f"cert_no:{_get(r, c_cert)}"]
        scan_key = f"2.安全生产许可证 + 信用评级/{i - 1}.pdf"  # 行 2→1.pdf（安许）、行 3→2.pdf（信用）
        refs.append(scan_key)
        if scan_key in scan_evidence:
            refs.append(f"material:{scan_evidence[scan_key]}")
        out.append({"category": name, "level": _get(r, c_grade) or None, "specialty": _qual_family(name),
                    "issuer": None, "valid_from": _get(r, c_from) or None, "valid_until": _get(r, c_until) or None,
                    "evidence_refs": refs})
    # CA 数字证书
    h, rows = _sheet_rows(xls_dir / "CA证书信息.xlsx", 0)
    c_vendor, c_count, c_platform, c_until = _col(h, "CA厂商名称"), _col(h, "证书数量"), _col(h, "支持平台"), _col(h, "有效期")
    for i, r in enumerate(rows, start=2):
        vendor = _get(r, c_vendor)
        if not vendor:
            continue
        out.append({"category": f"CA数字证书（{vendor}）", "level": "不分等级", "specialty": _get(r, c_platform) or None,
                    "issuer": vendor, "valid_until": _get(r, c_until) or None,
                    "evidence_refs": [f"ledger:xls→xlsx:CA证书信息:row{i}", f"count:{_get(r, c_count)}"]})
    return out, {"qualifications": n_qual, "safety_credit": len(out) - n_qual - 1, "ca": 1, "total": len(out)}


# ── 入库 ───────────────────────────────────────────────────────────────


def _session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from runtime.core.config import database_url
    return sessionmaker(bind=create_engine(database_url()))()


def _import_material(session, path: Path, *, material_id: str, material_type: str, classification: str,
                     data_owner: str, actor: str, report: dict, source_type: str = "uploaded"):
    from runtime.core.config import object_store_root
    from runtime.db import material_service
    size = path.stat().st_size
    if size > MAX_OBJECT_BYTES:
        report.setdefault("oversized_skipped", []).append({"path": str(path.relative_to(BASE)), "bytes": size})
        return None
    imported = material_service.import_material(
        session, src_path=str(path), material_id=material_id, material_type=material_type,
        source_type=source_type, owner_type="enterprise", classification=classification,
        permission_scope="enterprise_read", data_owner=data_owner,
        store_root=object_store_root(), project_id=None, actor=actor,
    )
    report.setdefault("materials", {}).setdefault("created" if imported.created else "reused", 0)
    report["materials"]["created" if imported.created else "reused"] += 1
    return imported


def run_evidence(session, *, data_owner: str, actor: str, report: dict, dry: bool) -> tuple[dict[str, str], dict[str, str]]:
    """B：证据扫描件 → Material + evidence_files。返回 (资质/安许扫描件 key→material_id, 典型项目 PDF 名→material_id)。"""
    from runtime.db import enterprise_service
    from runtime.db.models import EvidenceFile
    from sqlalchemy import select

    scan_map: dict[str, str] = {}
    perf_map: dict[str, str] = {}
    plan = []
    for d, kind, mtype, ftype in ((D_QUAL_SCANS, "QUAL", "qualification_cert", "资质证书扫描件"),
                                  (D_SAFE_SCANS, "SAFE", "qualification_cert", "安许证/信用证书扫描件"),
                                  (D_PERF_EVID, "PERF", "performance_record", "中标合同+验收文件")):
        for f in sorted(d.iterdir()):
            if f.suffix.lower() != ".pdf":
                continue
            rel = f"{d.name}/{f.name}"
            plan.append((f, rel, kind, mtype, ftype))
    report["evidence"] = {"planned": len(plan), "registered": 0, "evidence_rows_created": 0}
    for f, rel, kind, mtype, ftype in plan:
        mid = _slug_id(f"MAT-EVID-{kind}", rel)
        if kind == "PERF":
            perf_map[f.name] = mid
        else:
            scan_map[rel] = mid
        if dry:
            continue
        imported = _import_material(session, f, material_id=mid, material_type=mtype, classification="confidential",
                                    data_owner=data_owner, actor=actor, report=report)
        if imported is None:
            continue
        report["evidence"]["registered"] += 1
        exists = session.scalar(select(EvidenceFile).where(EvidenceFile.material_id == mid,
                                                           EvidenceFile.source_hash == imported.material.content_hash))
        if exists is None:
            enterprise_service.import_evidence_file(
                session, file_type=ftype, object_uri=imported.version_record.object_uri,
                source_hash=imported.material.content_hash, uploaded_by=actor, material_id=mid,
                classification="confidential")
            report["evidence"]["evidence_rows_created"] += 1
        session.commit()
    return scan_map, perf_map


def run_ledgers(session, *, xls_dir: Path, data_owner: str, actor: str, report: dict, dry: bool,
                scan_map: dict[str, str], perf_map: dict[str, str]) -> None:
    from runtime.db import enterprise_service

    personnel_b, managers_b, st_b = build_builder_rows()
    titles, st_t = build_title_rows()
    posts, st_p = build_post_cert_rows()
    perfs, st_f = build_performance_rows(perf_map)
    quals, st_q = build_qualification_rows(xls_dir, scan_map)
    report["ledgers"] = {"builder": st_b, "title": st_t, "post_cert": st_p, "performance": st_f, "qualification": st_q}
    if dry:
        return

    def ledger_material(path: Path, kind: str):
        mid = _slug_id(f"MAT-LEDGER-{kind}", str(path.relative_to(ROOT)))
        imp = _import_material(session, path, material_id=mid, material_type="evidence_file",
                               classification="internal", data_owner=data_owner, actor=actor, report=report)
        return imp.material.material_id if imp else None

    results = {}
    m = ledger_material(F_BUILDER, "BUILDER")
    for row in personnel_b:
        row["material_id"] = m
    results["personnel.registered_builder"] = enterprise_service.import_personnel(
        session, personnel_b, category="registered_builder", data_owner=data_owner, actor=actor, source=F_BUILDER.name)
    results["managers"] = enterprise_service.import_managers(
        session, managers_b, data_owner=data_owner, actor=actor, source=F_BUILDER.name)
    session.commit()

    ledger_material(F_TITLE, "TITLE")
    results["personnel.technical_title"] = enterprise_service.import_personnel(
        session, titles, category="technical_title", data_owner=data_owner, actor=actor, source=F_TITLE.name)
    session.commit()

    ledger_material(F_POST, "POST")
    results["personnel.post_certificate"] = enterprise_service.import_personnel(
        session, posts, category="post_certificate", data_owner=data_owner, actor=actor, source=F_POST.name)
    session.commit()

    m = ledger_material(F_PERF, "PERF")
    results["performances"] = enterprise_service.import_performances(
        session, perfs, data_owner=data_owner, actor=actor, source=F_PERF.name, material_id=m)
    session.commit()

    for src in (F_QUAL_XLS, F_SAFE_XLS, F_CA_XLS):  # 原始 .xls 作为不可变材料留存（转存件仅为解析工作面）
        ledger_material(src, "QUALXLS")
    m = _slug_id("MAT-LEDGER-QUALXLS", str(F_QUAL_XLS.relative_to(ROOT)))
    results["qualifications"] = enterprise_service.import_qualifications(
        session, quals, data_owner=data_owner, actor=actor, source="资质/安许/CA 清单.xls（Excel 转存 .xlsx 解析）", material_id=m)
    session.commit()
    report["ledger_import"] = results


def run_bidbooks(session, *, data_owner: str, actor: str, report: dict, dry: bool) -> None:
    """C：目录 8/9 全部文件 → Material（按内容 sha256 去重；> 200MB 如实登记未入库）。"""
    files = []
    for d in (D_BIDBOOK_8, D_BIDBOOK_9):
        for root, _, names in os.walk(d):
            for n in sorted(names):
                if n.startswith(".") or n == ".DS_Store":
                    continue
                files.append(Path(root) / n)
    seen: dict[str, str] = {}
    stat = {"files": len(files), "registered": 0, "duplicate_content": 0, "oversized": 0, "by_type": {}}
    for f in files:
        rel = str(f.relative_to(BASE))
        mtype = "tender_document" if ("招标文件" in rel or "招标" in f.name) else "evidence_file"
        stat["by_type"][mtype] = stat["by_type"].get(mtype, 0) + 1
        if f.stat().st_size > MAX_OBJECT_BYTES:
            stat["oversized"] += 1
            report.setdefault("oversized_skipped", []).append({"path": rel, "bytes": f.stat().st_size})
            continue
        if dry:
            continue
        digest = _sha256(f)
        if digest in seen:
            stat["duplicate_content"] += 1
            report.setdefault("duplicates", []).append({"path": rel, "same_as": seen[digest]})
            continue
        mid = _slug_id("MAT-BIDBOOK", rel)
        imp = _import_material(session, f, material_id=mid, material_type=mtype, classification="confidential",
                               data_owner=data_owner, actor=actor, report=report)
        if imp is not None:
            seen[digest] = rel
            stat["registered"] += 1
    report["bidbooks"] = stat


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", choices=["all", "ledgers", "evidence", "bidbooks"], default="all")
    ap.add_argument("--xls-dir", default=str(XLS_DIR_DEFAULT), help="三份 .xls 经 Excel 转存的 .xlsx 所在目录")
    ap.add_argument("--data-owner", default=DATA_OWNER_DEFAULT)
    ap.add_argument("--actor", default=ACTOR_DEFAULT)
    args = ap.parse_args()

    if not BASE.is_dir():
        print(f"资料目录不存在: {BASE}", file=sys.stderr)
        return 2
    xls_dir = Path(args.xls_dir)
    for n in ("资质证书清单.xlsx", "安许与信用评级清单.xlsx", "CA证书信息.xlsx"):
        if not (xls_dir / n).is_file():
            print(f"缺少转存文件 {xls_dir / n}（先用 Excel 把对应 .xls 另存为 .xlsx）", file=sys.stderr)
            return 2

    report: dict = {"started_at": datetime.now(timezone.utc).isoformat(), "dry_run": args.dry_run, "only": args.only}
    session = None if args.dry_run else _session()
    t0 = time.time()
    scan_map, perf_map = {}, {}
    if args.only in ("all", "evidence", "ledgers"):
        scan_map, perf_map = run_evidence(session, data_owner=args.data_owner, actor=args.actor, report=report,
                                          dry=args.dry_run or args.only == "ledgers")
    if args.only in ("all", "ledgers"):
        run_ledgers(session, xls_dir=xls_dir, data_owner=args.data_owner, actor=args.actor, report=report,
                    dry=args.dry_run, scan_map=scan_map, perf_map=perf_map)
    if args.only in ("all", "bidbooks"):
        run_bidbooks(session, data_owner=args.data_owner, actor=args.actor, report=report, dry=args.dry_run)
    report["elapsed_seconds"] = round(time.time() - t0, 1)

    XLS_DIR_DEFAULT.mkdir(parents=True, exist_ok=True)
    out = XLS_DIR_DEFAULT / f"import_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    summary = {k: v for k, v in report.items() if k not in ("duplicates", "oversized_skipped")}
    summary["duplicates"] = len(report.get("duplicates", []))
    summary["oversized_skipped"] = report.get("oversized_skipped", [])
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(f"report → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
