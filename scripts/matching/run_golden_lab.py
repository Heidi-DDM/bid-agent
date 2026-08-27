#!/usr/bin/env python3
"""Run the versioned golden rule set without reading private tender material."""
from __future__ import annotations

import json
from pathlib import Path

from engine import evaluate, validate_requirements


HERE = Path(__file__).resolve().parent
REQUIREMENTS = json.loads((HERE / "golden_requirements.json").read_text(encoding="utf-8"))


def main() -> None:
    validation = validate_requirements(REQUIREMENTS, expected_count=16)
    if not validation["valid"]:
        raise SystemExit("规则集校验失败: " + "; ".join(validation["errors"]))
    # Empty evidence is intentional: this validates all declared requirements are
    # executed and reported as gaps, rather than silently passing any item.
    result = evaluate(REQUIREMENTS, {}, as_of="2024-05-15", mode="diagnostic")
    if not result["coverage"]["complete"]:
        raise SystemExit(f"执行覆盖不完整: {result['coverage']}")
    print(json.dumps({"rule_validation": validation, "coverage": result["coverage"],
                      "qualification_result": result["qualification_result"],
                      "pending_ids": [item["requirement_id"] for item in result["pending"]]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
