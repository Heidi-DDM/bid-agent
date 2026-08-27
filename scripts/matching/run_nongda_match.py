#!/usr/bin/env python3
"""农大项目（河北农业大学东校区研究生宿舍建设项目施工）真实证据匹配。

事后验证基线：该项目已中标（用户 2026-08-28 提供：客户评判满分、已投标、已施工、已验收）。
引擎判定应与"当年满足全部要求"的事实一致；缺材料处如实标 unverifiable，不推断满足。

运行：
    python scripts/matching/run_nongda_match.py
输出：
    验证受限材料/农大/nongda_match_result.json（受限，不入 Git）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import evaluate, validate_requirements

ROOT = Path(__file__).resolve().parents[2]
REQS = json.loads((ROOT / "scripts/matching/golden_requirements_nongda.json").read_text(encoding="utf-8"))
RESTRICTED = ROOT / "验证受限材料/农大"
EVID = json.loads((RESTRICTED / "real_evidence_nongda.json").read_text(encoding="utf-8"))


def main() -> None:
    validation = validate_requirements(REQS, expected_count=20)
    if not validation["valid"]:
        raise SystemExit("规则集校验失败: " + "; ".join(validation["errors"]))
    result = evaluate(REQS, EVID, as_of="2025-10-30", mode="diagnostic")
    if not result["coverage"]["complete"]:
        raise SystemExit(f"执行覆盖不完整: {result['coverage']}")
    summary = {
        "run": {"date": "2026-08-28", "sample": "河北农业大学东校区研究生宿舍建设项目施工", "as_of": "2025-10-30",
                "mode": "diagnostic", "rule_set": "golden_requirements_nongda.json",
                "evidence": "real_evidence_nongda.json（脱敏，商务标/技术标/投标函/保函/企业资料库）",
                "command": "python scripts/matching/run_nongda_match.py",
                "executor": "开发负责人（自动化）",
                "posthoc_baseline": "实际已中标（客户评判满分、已投标、已施工、已验收）"},
        "rule_validation": validation,
        "coverage": result["coverage"],
        "qualification_result": result["qualification_result"],
        "scoring_result": result["scoring_result"],
        "operational_readiness": result["operational_readiness"],
        "internal_admission_eligible": result["internal_admission_eligible"],
        "matrix": result["matrix"],
        "blocked": [{"requirement_id": m["requirement_id"], "match_reason": m["match_reason"]} for m in result["blocked"]],
        "pending": [{"requirement_id": m["requirement_id"], "match_reason": m["match_reason"]} for m in result["pending"]],
        "review": [{"requirement_id": m["requirement_id"], "match_reason": m["match_reason"]} for m in result["review"]],
    }
    out = RESTRICTED / "nongda_match_result.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("coverage:", result["coverage"])
    print("qualification_result:", result["qualification_result"])
    print("scoring_result:", result["scoring_result"])
    print("operational_readiness:", result["operational_readiness"])
    print("pending:", [m["requirement_id"] for m in result["pending"]])
    print("blocked:", [m["requirement_id"] for m in result["blocked"]])
    print("review:", [m["requirement_id"] for m in result["review"]])
    print("written:", out)


if __name__ == "__main__":
    main()