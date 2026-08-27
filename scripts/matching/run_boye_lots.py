#!/usr/bin/env python3
"""V-007：博野县高标准农田（国债）施工 8 标段黄金样本矩阵验证。

以同一招标文件（HBGR-2024085）的 8 个标段为样本，验证：
- 标段隔离（F008 §4.1 lot_id）：规则与证据按标段过滤
- 保证金金额按标段差异化（须知 3.4.1：1/2/5/6 标 10 万、3 标 24 万、4 标 34 万、
  7 标 22 万、8 标 28 万；boye_tender.txt:397-406）
- 企业资料为项目级通用证据（五标段素材盘点，脱敏）

输出：验证受限材料/博野五标段/boye_lots_matrix.json（受限，不入 Git）
"""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import evaluate, validate_requirements

ROOT = Path(__file__).resolve().parents[2]
RESTRICTED = ROOT / "验证受限材料/博野五标段"
REQS = json.loads((ROOT / "scripts/matching/golden_requirements.json").read_text(encoding="utf-8"))
EVID = json.loads((RESTRICTED / "real_evidence_boye.json").read_text(encoding="utf-8"))

# 招标原文：标段划分 boye_tender.txt:78-86；保证金金额 boye_tender.txt:397-406
LOTS = {
    1: {"villages": "迁庄村、南小王村", "bond": 100000},
    2: {"villages": "套里村、魏庄村", "bond": 100000},
    3: {"villages": "东墟营、东墟村、大墟村、龙堂村", "bond": 240000},
    4: {"villages": "北祝村", "bond": 340000},
    5: {"villages": "白塔村、徐家营村", "bond": 100000},
    6: {"villages": "大苑村、大营村", "bond": 100000},
    7: {"villages": "南田村、西田村、胡庄村、小西章村", "bond": 220000},
    8: {"villages": "北杨村、北小王村、邓庄村", "bond": 280000},
}
BOND_CLAUSE = "须知 3.4.1（1-8标段金额，招标文件原文 397-406 行）"
LOT_CLAUSE = "公告 §2.1（标段划分，招标文件原文 78-86 行）"


def lot_requirements(lot: int) -> list[dict]:
    """五标段基线 16 条 → 标段 N 规则集：RQ-H-008/RQ-A-002 替换为标段版本（金额、lot_id）。"""
    reqs = [r for r in deepcopy(REQS) if r["requirement_id"] not in ("RQ-H-008", "RQ-A-002")]
    bond = LOTS[lot]["bond"]
    reqs.append({
        "requirement_id": "RQ-H-008", "req_type": "hard_requirement", "category": "保证金",
        "clause_ref": BOND_CLAUSE, "lot_id": lot,
        "assertion": f"投标保证金 {bond} 元且形式、到账时间符合要求（{lot} 标段）",
        "rule": {"type": "bid_bond", "amount": bond, "forms": ["保函", "银行电汇", "电子保函", "保证保险"]},
        "evidence_required": ["bid_bond"]})
    reqs.append({
        "requirement_id": "RQ-A-002", "req_type": "action_requirement", "category": "保证金到账",
        "required_by_stage": "approval_ready", "clause_ref": BOND_CLAUSE, "lot_id": lot,
        "assertion": f"保证金到账（{lot} 标段 {bond} 元）",
        "action_status": "not_started", "evidence_required": []})
    return reqs


def main() -> None:
    base = validate_requirements(REQS, expected_count=16)
    if not base["valid"]:
        raise SystemExit("五标段基线规则集校验失败: " + "; ".join(base["errors"]))
    lots = []
    for lot in sorted(LOTS):
        reqs = lot_requirements(lot)
        validation = validate_requirements(reqs, expected_count=16)
        if not validation["valid"]:
            raise SystemExit(f"标段 {lot} 规则集校验失败: {validation['errors']}")
        result = evaluate(reqs, EVID, as_of="2024-05-15", mode="diagnostic", lot_id=lot)
        bond_entry = next(m for m in result["matrix"] if m["requirement_id"] == "RQ-H-008")
        lots.append({
            "lot_id": lot, "villages": LOTS[lot]["villages"], "bond_amount": LOTS[lot]["bond"],
            "clause_ref": LOT_CLAUSE + " / " + BOND_CLAUSE,
            "coverage": result["coverage"],
            "qualification_result": result["qualification_result"],
            "bond_match_result": bond_entry["match_result"],
            "bond_match_reason": bond_entry["match_reason"],
            "internal_admission_eligible": result["internal_admission_eligible"],
        })
        print(f"标段 {lot}: coverage={result['coverage']['executed']}/{result['coverage']['declared']} "
              f"资格={result['qualification_result']} 保证金({LOTS[lot]['bond']})={bond_entry['match_result']}")

    summary = {
        "_meta": {"date": "2026-08-27", "sample": "博野县高标准农田建设项目（国债）施工（1-8标段）",
                  "as_of": "2024-05-15", "tender": "HBGR-2024085（同一招标文件，无澄清）",
                  "note": "标段隔离验证：8 标段共用招标文件与项目级证据；仅五标段为实际投标，其余标段为规则与证据隔离演练（非实际投标事实）",
                  "evidence": "real_evidence_boye.json（脱敏，五标段素材盘点）",
                  "command": "python scripts/matching/run_boye_lots.py"},
        "lots": lots,
        # 标段证据隔离实证：lot_id=5 的保证金回执只在标段 5 内有效，跨标段被过滤
        "lot_isolation_proof": isolation_proof(),
    }
    out = RESTRICTED / "boye_lots_matrix.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("written:", out)


def isolation_proof() -> dict:
    """同一保证金回执（lot_id=5）在标段 5 内 satisfied，在标段 3 被过滤 → unverifiable。"""
    ev = deepcopy(EVID)
    ev.setdefault("bid_bond", []).append({
        "amount": 100000, "form": "银行电汇", "status": "valid",
        "verified_at": "2024-05-01", "valid_until": "2024-06-01", "lot_id": 5,
        "evidence_ref": "标段5保证金回执（演练样例）"})
    r5 = evaluate(lot_requirements(5), ev, as_of="2024-05-15", mode="diagnostic", lot_id=5)
    r3 = evaluate(lot_requirements(3), ev, as_of="2024-05-15", mode="diagnostic", lot_id=3)
    m5 = next(m for m in r5["matrix"] if m["requirement_id"] == "RQ-H-008")
    m3 = next(m for m in r3["matrix"] if m["requirement_id"] == "RQ-H-008")
    return {
        "scenario": "lot_id=5 的保证金回执（¥100000）",
        "lot_5_result": m5["match_result"], "lot_5_reason": m5["match_reason"],
        "lot_3_result": m3["match_result"], "lot_3_reason": m3["match_reason"],
        "assert": "标段 5 内 satisfied / 标段 3 被过滤 → unverifiable（跨标段证据不得混用）"}


if __name__ == "__main__":
    main()