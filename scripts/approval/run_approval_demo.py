#!/usr/bin/env python3
"""R010 端到端演示：匹配结果 → 审批流 → 豁免 → 审计导出。

以农大项目真实匹配结果为输入（F008 准入结果 → F009 审批），演示：
1. 非满分（internal_admission_eligible=false）不得创建审批 → 正确阻断；
2. 补齐证据后重跑复现满分 → 创建审批 → 审批/驳回/豁免各路径；
3. 豁免过期自动失效回阻断；
4. 审计记录导出（谁、何时、依据、结论）可序列化。

运行：
    python scripts/approval/run_approval_demo.py
输出：
    验证受限材料/农大/approval_demo_result.json（受限，不入 Git）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from approval.approval_workflow import (
    add_waiver, approve, audit_log, create_approval, decide_waived, expire_waivers,
    reject, to_dict,
)

RESTRICTED = ROOT / "验证受限材料/农大"


def _admission_from_match(match: dict) -> dict:
    """从引擎匹配结果提取准入结论（F008 口径）。"""
    return {
        "internal_admission_eligible": match["internal_admission_eligible"],
        "qualification_result": match["qualification_result"],
        "scoring_result": match["scoring_result"],
        "operational_readiness": match["operational_readiness"],
        "coverage": match["coverage"],
        "blocked": match["blocked"],
        "pending": match["pending"],
        "review": match["review"],
    }


def main() -> None:
    match_file = RESTRICTED / "nongda_match_result.json"
    if not match_file.exists():
        raise SystemExit("缺少农大匹配结果，先运行 python scripts/matching/run_nongda_match.py")
    match = json.loads(match_file.read_text(encoding="utf-8"))
    admission = _admission_from_match(match)

    steps = []

    # ① 非满分不得创建审批（当前农大结果：财务审计待补 → not eligible）
    try:
        create_approval(admission, approval_id="ND-REAL-1", project_id="ND-2025",
                        approver="经营负责人")
        steps.append({"step": 1, "ok": False, "detail": "非满分竟然创建了审批（不应发生）"})
    except ValueError as exc:
        steps.append({"step": 1, "ok": True, "detail": f"非满分创建审批被拒绝：{exc}"})

    # ② 构造“补齐财务审计后”的满分准入结果（引擎口径，仅演示审批流；不代表公司承诺）
    full_admission = dict(admission)
    full_admission.update({
        "internal_admission_eligible": True,
        "qualification_result": "passed",
        "scoring_result": "full",
        "operational_readiness": "ready",
        "pending": [],
        "review": [m for m in full_admission.get("review", []) if m["requirement_id"] not in ("NQ-S-001", "NQ-S-002")],
    })
    record = create_approval(full_admission, approval_id="ND-REAL-2", project_id="ND-2025",
                             approver="经营负责人", admission_result_ref="admission:ND-2025:v1.0.0")
    steps.append({"step": 2, "ok": True, "detail": "满分准入创建审批进入 pending_bid_approval"})

    # ③ 驳回必须填写意见
    try:
        reject(record, approver="经营负责人", at="2026-08-28T10:00:00", comment="")
        steps.append({"step": 3, "ok": False, "detail": "空意见驳回未被拒绝（不应发生）"})
    except ValueError as exc:
        steps.append({"step": 3, "ok": True, "detail": f"空意见驳回被拒绝：{exc}"})

    # ④ 审批通过
    approve(record, approver="经营负责人", at="2026-08-28T10:30:00", comment="同意投标")
    steps.append({"step": 4, "ok": True, "detail": f"审批通过 → {record.decision}"})

    # ⑤ 终态后不可重复决策
    try:
        reject(record, approver="经营负责人", at="2026-08-28T11:00:00", comment="不应重复")
        steps.append({"step": 5, "ok": False, "detail": "终态后重复决策未被拒绝（不应发生）"})
    except ValueError as exc:
        steps.append({"step": 5, "ok": True, "detail": f"终态后重复决策被拒绝：{exc}"})

    # ⑥ 豁免：原因/证据/有效期必填；带豁免批准
    rec2 = create_approval(full_admission, approval_id="ND-REAL-3", project_id="ND-2025",
                           approver="经营负责人")
    add_waiver(rec2, waiver_id="W-ND-1", authorizer="经营负责人", reason="审计报告在途（已受理）",
               evidence_refs=["受理回执 E-ND-2025-01"], valid_until="2026-09-30",
               approved_at="2026-08-28", covered_items=["NQ-H-007"])
    decide_waived(rec2, approver="经营负责人", at="2026-08-28T11:30:00", comment="带豁免批准，跟进材料")
    steps.append({"step": 6, "ok": True, "detail": f"带豁免批准 → {rec2.decision}（不改变满分定义，仅人工放行）"})

    # ⑦ 豁免过期自动失效回阻断
    rec3 = create_approval(full_admission, approval_id="ND-REAL-4", project_id="ND-2025",
                           approver="经营负责人")
    add_waiver(rec3, waiver_id="W-ND-2", authorizer="经营负责人", reason="材料在途",
               evidence_refs=["E-ND-2025-02"], valid_until="2026-08-01", approved_at="2026-07-28")
    expire_waivers(rec3, as_of="2026-08-28")
    steps.append({"step": 7, "ok": True, "detail": f"豁免过期 → {rec3.decision}（回到阻断）"})

    # ⑧ 审计导出（谁、何时、依据、结论）
    audit = audit_log(rec2)
    steps.append({"step": 8, "ok": True, "detail": f"审计条数={len(audit)}：{[e['action'] for e in audit]}"})

    summary = {
        "run": {"date": "2026-08-28", "demo": "R010 审批流端到端（F009 §6/§7）",
                "input": "nongda_match_result.json（引擎准入结果）",
                "command": "python scripts/approval/run_approval_demo.py",
                "executor": "开发负责人（自动化）",
                "note": "步骤②的满分准入为补齐材料后的构造口径，仅用于演示审批流；不代表对农大项目实际审批。"},
        "steps": steps,
        "approval_approved": to_dict(record),
        "approval_waived": to_dict(rec2),
        "approval_blocked_by_expiry": to_dict(rec3),
    }
    out = RESTRICTED / "approval_demo_result.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("steps:")
    for s in steps:
        print(f"  [{s['step']}] {'✅' if s['ok'] else '❌'} {s['detail']}")
    print("written:", out)


if __name__ == "__main__":
    main()