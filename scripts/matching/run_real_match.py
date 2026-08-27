#!/usr/bin/env python3
"""V-005：真实脱敏证据重跑博野五标段匹配，保存脱敏结果摘要（F008 §9.1 覆盖门禁）。

运行：
    python scripts/matching/run_real_match.py
输出：
    验证受限材料/博野五标段/real_match_result_boye.json（受限，不入 Git）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import evaluate, validate_requirements

ROOT = Path(__file__).resolve().parents[2]
REQS = json.loads((ROOT / "scripts/matching/golden_requirements.json").read_text(encoding="utf-8"))
RESTRICTED = ROOT / "验证受限材料/博野五标段"
EVID = json.loads((RESTRICTED / "real_evidence_boye.json").read_text(encoding="utf-8"))


def main() -> None:
    validation = validate_requirements(REQS, expected_count=16)
    if not validation["valid"]:
        raise SystemExit("规则集校验失败: " + "; ".join(validation["errors"]))
    result = evaluate(REQS, EVID, as_of="2024-05-15", mode="diagnostic")
    if not result["coverage"]["complete"]:
        raise SystemExit(f"执行覆盖不完整: {result['coverage']}")
    summary = {
        "run": {"date": "2026-08-27", "sample": "博野五标段", "as_of": "2024-05-15",
                "mode": "diagnostic", "rule_set": "golden_requirements.json",
                "evidence": "real_evidence_boye.json（脱敏，素材盘点）",
                "command": "python scripts/matching/run_real_match.py",
                "executor": "开发负责人（自动化）"},
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
    out = RESTRICTED / "real_match_result_boye.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("coverage:", result["coverage"])
    print("qualification_result:", result["qualification_result"])
    print("pending:", [m["requirement_id"] for m in result["pending"]])
    print("blocked:", [m["requirement_id"] for m in result["blocked"]])
    print("review:", [m["requirement_id"] for m in result["review"]])
    print("written:", out)


if __name__ == "__main__":
    main()