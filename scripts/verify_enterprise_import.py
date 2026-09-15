#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""企业资料入库验收（只读，不写库、不改文件）。

对照三件事：
  1. 库内计数 vs 源台账行数（建造师/职称/岗位证书/业绩/资质三份 .xls 转存件）——差额 = 幂等键折叠的重复行；
  2. 状态分布（全部应为 pending_verification，等数据管理员核验）+ 证据回链（资质→扫描件、业绩→合同验收）;
  3. 抽样对照：随机取 N 行库内记录，打印源台账对应行（按 evidence_refs 里的 ledger 行号回查），人工肉眼核对。

用法：
  .venv/bin/python scripts/verify_enterprise_import.py            # 汇总
  .venv/bin/python scripts/verify_enterprise_import.py --sample 5 # 每类随机抽 5 行对照源表
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from sqlalchemy import create_engine, text  # noqa: E402

from runtime.core.config import database_url  # noqa: E402

OWNER_PREFIX = "某建设集团（20260821"


def _q(conn, sql, **kw):
    return conn.execute(text(sql), kw).all()


def section(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="每类随机抽 N 行与源台账对照")
    args = ap.parse_args()

    import import_enterprise_ledgers as src  # 复用源台账读取（只读）

    engine = create_engine(database_url())
    with engine.connect() as c:
        section("① 库内计数 vs 源台账（本批 data_owner 前缀过滤，不含农大演示数据）")
        counts = {
            "qualifications": _q(c, "select count(*) from enterprise_data.qualifications where data_owner like :o", o=OWNER_PREFIX + "%")[0][0],
            "performances": _q(c, "select count(*) from enterprise_data.performances where data_owner like :o", o=OWNER_PREFIX + "%")[0][0],
            "managers": _q(c, "select count(*) from enterprise_data.managers where data_owner like :o", o=OWNER_PREFIX + "%")[0][0],
        }
        per_cat = dict(_q(c, "select category, count(*) from enterprise_data.personnel where data_owner like :o group by category", o=OWNER_PREFIX + "%"))
        _, b_rows = src._sheet_rows(src.F_BUILDER, 0)
        _, t_rows = src._sheet_rows(src.F_TITLE, 0)
        _, p_rows = src._sheet_rows(src.F_POST, "岗位证书清单")
        _, f_rows = src._sheet_rows(src.F_PERF, 0)
        _, q_rows = src._sheet_rows(src.XLS_DIR_DEFAULT / "资质证书清单.xlsx", 0)
        _, s_rows = src._sheet_rows(src.XLS_DIR_DEFAULT / "安许与信用评级清单.xlsx", 0)
        _, ca_rows = src._sheet_rows(src.XLS_DIR_DEFAULT / "CA证书信息.xlsx", 0)
        rows = [
            ("注册建造师 → personnel(registered_builder)", len(b_rows), per_cat.get("registered_builder", 0)),
            ("注册建造师 → managers", len(b_rows), counts["managers"]),
            ("技术职称 → personnel(technical_title)", len(t_rows), per_cat.get("technical_title", 0)),
            ("岗位证书 → personnel(post_certificate)", len(p_rows), per_cat.get("post_certificate", 0)),
            ("近五年竣工业绩 → performances", len(f_rows), counts["performances"]),
            ("资质+安许信用+CA → qualifications", len(q_rows) + len(s_rows) + len(ca_rows), counts["qualifications"]),
        ]
        print(f"{'源台账 → 库表':46s}{'源行数':>8s}{'库内':>8s}{'差额':>8s}  说明")
        for name, s_n, d_n in rows:
            diff = s_n - d_n
            note = "一致" if diff == 0 else ("重复行被幂等键折叠（姓名+专业+证书号相同）" if diff > 0 else "库内多于源！需排查")
            print(f"{name:46s}{s_n:8d}{d_n:8d}{diff:8d}  {note}")

        section("② 状态分布（应全部 pending_verification；active 只会是核验后或农大演示行）")
        for tbl in ("qualifications", "performances", "managers", "personnel"):
            dist = _q(c, f"select status, count(*) from enterprise_data.{tbl} group by status order by 1")
            print(f"{tbl:16s}", ", ".join(f"{s}={n}" for s, n in dist))

        section("③ 证据回链")
        n_ev = _q(c, "select count(*) from enterprise_data.evidence_files")[0][0]
        ev_types = _q(c, "select file_type, count(*) from enterprise_data.evidence_files group by 1 order by 1")
        print(f"evidence_files 共 {n_ev} 行：", ", ".join(f"{t}={n}" for t, n in ev_types))
        q_linked = _q(c, "select count(*) from enterprise_data.qualifications where data_owner like :o and evidence_refs::text like '%material:MAT-EVID-%'", o=OWNER_PREFIX + "%")[0][0]
        print(f"资质行回链扫描件材料：{q_linked}/{counts['qualifications']}（CA 数字证书无扫描件属正常）")
        p_linked = _q(c, "select project_name from enterprise_data.performances where data_owner like :o and evidence_refs::text like '%material:MAT-EVID-PERF%'", o=OWNER_PREFIX + "%")
        print(f"业绩行回链合同/验收 PDF：{len(p_linked)} 条 →", [r[0] for r in p_linked])
        print("已知未回链证据（台账无对应项目，需公司补业绩台账）：北京新机场(廊坊区域)回迁安置区二标段、江北集中区大龙湾三桥")

        section("④ 材料 / 对象库")
        mats = _q(c, "select material_type, classification, count(*) from public_data.materials where owner_type='enterprise' group by 1,2 order by 1")
        for mt, cl, n in mats:
            print(f"  {mt:20s} {cl:12s} {n}")
        pref = _q(c, "select split_part(material_id,'-',2), count(*) from public_data.materials where material_id like 'MAT-%' group by 1 order by 1")
        print("  按前缀：", ", ".join(f"{p}={n}" for p, n in pref))
        obj_root = ROOT / "runtime" / "objects" / "enterprise"
        missing = 0
        for (uri,) in _q(c, "select mv.object_uri from public_data.material_versions mv join public_data.materials m on m.material_id=mv.material_id and m.version=mv.version where m.owner_type='enterprise'"):
            if not (ROOT / "runtime" / "objects" / uri).is_file():
                missing += 1
        print(f"  对象文件落盘核对：缺失 {missing} 个（应为 0）；目录 {obj_root}")

        section("⑤ 待补占位统计（不推断的代价，如实展示）")
        print("建造师注册编号为待补：", _q(c, "select count(*) from enterprise_data.personnel where category='registered_builder' and cert_no='__待补__'")[0][0], "/", per_cat.get("registered_builder", 0))
        print("项目经理注册编号为待补：", _q(c, "select count(*) from enterprise_data.managers where reg_cert_no='__待补__' and data_owner like :o", o=OWNER_PREFIX + "%")[0][0], "/", counts["managers"])
        print("业绩合同金额为空：", _q(c, "select count(*) from enterprise_data.performances where contract_amount is null and data_owner like :o", o=OWNER_PREFIX + "%")[0][0], "/", counts["performances"])
        print("业绩竣工日期为空：", _q(c, "select count(*) from enterprise_data.performances where completed_at is null and data_owner like :o", o=OWNER_PREFIX + "%")[0][0], "/", counts["performances"])
        print("资质有效期为空：", _q(c, "select count(*) from enterprise_data.qualifications where valid_until is null and data_owner like :o", o=OWNER_PREFIX + "%")[0][0], "/", counts["qualifications"])

        if args.sample > 0:
            section(f"⑥ 随机抽样 {args.sample} 行 × 4 类：库内记录 ← 源台账原行（人工肉眼核对）")
            rnd = random.Random()

            def show(title, db_rows, sources):
                """sources: {ledger 引用中的表名片段: (源行列表, 展示名)}；按 evidence_refs 里的 ledger 引用分流回查。"""
                print(f"\n--- {title} ---")
                for rec in rnd.sample(db_rows, min(args.sample, len(db_rows))):
                    refs = rec[-1] or []
                    ref = next((r for r in refs if r.startswith("ledger:") and ":row" in r), "")
                    print("库内 :", [str(x)[:28] for x in rec[:-1]])
                    hit = next(((rows_, name_) for key, (rows_, name_) in sources.items() if key in ref), None)
                    row_no = int(ref.rsplit("row", 1)[-1]) if ref else None
                    if hit and row_no and 2 <= row_no <= len(hit[0]) + 1:
                        print(f"源表 : {hit[1]} 第 {row_no} 行 →", [x[:28] for x in hit[0][row_no - 2] if x][:9])
                    else:
                        print("源表 : （无可回查的 ledger 行号）")

            show("资质 qualifications",
                 _q(c, "select category, level, valid_until, issuer, evidence_refs from enterprise_data.qualifications where data_owner like :o", o=OWNER_PREFIX + "%"),
                 {"资质证书清单": (q_rows, "资质证书清单.xlsx"), "安许与信用评级清单": (s_rows, "安许与信用评级清单.xlsx"), "CA证书信息": (ca_rows, "CA证书信息.xlsx")})
            show("业绩 performances（金额单位：元）",
                 _q(c, "select project_name, project_type, contract_amount, awarded_at, completed_at, owner_org, evidence_refs from enterprise_data.performances where data_owner like :o", o=OWNER_PREFIX + "%"),
                 {"sheet0": (f_rows, "近五年竣工业绩.xlsx")})
            show("建造师 personnel",
                 _q(c, "select name, specialty, cert_level, cert_no, on_site, on_site_project, evidence_refs from enterprise_data.personnel where category='registered_builder' and data_owner like :o", o=OWNER_PREFIX + "%"),
                 {"sheet0": (b_rows, "3.注册建造师清单.xlsx 首表")})
            show("岗位证书 personnel",
                 _q(c, "select name, specialty, cert_no, issuer, evidence_refs from enterprise_data.personnel where category='post_certificate' and data_owner like :o", o=OWNER_PREFIX + "%"),
                 {"sheet岗位证书清单": (p_rows, "岗位证书清单.xlsx「岗位证书清单」表")})

    print("\n验收口径：①差额只允许为正且可解释；②状态全 pending_verification；③对象文件缺失=0；④抽样与源表逐字一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
