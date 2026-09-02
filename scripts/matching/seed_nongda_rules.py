#!/usr/bin/env python3
"""农大试点（ND-2025）20 条规则入库：golden_requirements_nongda.json → admission_data.rule_sets/requirements

依据：
- 规则源：`scripts/matching/golden_requirements_nongda.json`（受 Git 管理的唯一规则源，20 条 = hard 12 / scored 4 / action 4）
- 判定时点 as_of=2025-10-30：对齐 `验证受限材料/农大/real_evidence_nongda.json`（as_of_note）与
  `nongda_match_result.json`（run.as_of）——投标截止/证据快照时点，不得默认当前时间（F008 §4.1）
- 表契约：F019 §3 rule_sets/requirements；字段与 F008 §4.1 一致（缺失处置默认 blocked_missing_data）

幂等：rule_set_id / requirement_id 已存在则跳过（不重复插入、不改写既有快照）。

用法（真库环境，先 unset PYTHONPATH）：
    DATABASE_URL=$(grep '^DATABASE_URL=' runtime/.env | cut -d= -f2-) \
        python scripts/matching/seed_nongda_rules.py [--as-of 2025-10-30]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

RULE_SET_ID = "RS-ND-2025-1.0.0"
PROJECT_ID = "ND-2025"
VERSION = "1.0.0"
DEFAULT_AS_OF = "2025-10-30"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=DEFAULT_AS_OF, help="判定时点（ISO 日期），默认 2025-10-30")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("缺少 DATABASE_URL（读取 runtime/.env 后重跑）", file=sys.stderr)
        return 2

    reqs = json.loads((ROOT / "scripts/matching/golden_requirements_nongda.json").read_text(encoding="utf-8"))
    if not isinstance(reqs, list) or len(reqs) != 20:
        print(f"规则源异常：期望 20 条，实际 {len(reqs) if isinstance(reqs, list) else type(reqs)}", file=sys.stderr)
        return 2

    from sqlalchemy import create_engine, func, select, text
    from sqlalchemy.orm import Session

    from runtime.db.models import Requirement, RuleSet

    engine = create_engine(url)
    with Session(engine) as session:
        existing = session.get(RuleSet, RULE_SET_ID)
        if existing is not None:
            n = session.scalar(select(func.count()).select_from(Requirement).where(Requirement.rule_set_id == RULE_SET_ID))
            print(f"规则集 {RULE_SET_ID} 已存在，跳过（requirements={n}）")
            return 0

        snapshot = {"source": "scripts/matching/golden_requirements_nongda.json",
                    "as_of": args.as_of, "count": len(reqs),
                    "seed_script": "scripts/matching/seed_nongda_rules.py"}
        session.add(RuleSet(
            rule_set_id=RULE_SET_ID, project_id=PROJECT_ID, version=VERSION,
            effective_from=None, created_by="seed_nongda_rules.py",
            snapshot={"requirements": reqs, "meta": snapshot},
        ))
        seen: set[str] = set()
        for r in reqs:
            rid = r["requirement_id"]
            if rid in seen:
                print(f"重复 requirement_id: {rid}", file=sys.stderr)
                return 2
            seen.add(rid)
            session.add(Requirement(
                requirement_id=rid,
                rule_set_id=RULE_SET_ID,
                req_type=r.get("req_type", "hard_requirement"),
                category=r.get("category"),
                lot_id=None,
                clause_ref=r.get("clause_ref", ""),
                assertion=r.get("assertion", ""),
                rule=r.get("rule") or {},
                evidence_required=r.get("evidence_required") or [],
                as_of=r.get("as_of") or args.as_of,
                missing_action=r.get("missing_action") or "blocked_missing_data",
                failure_effect=r.get("failure_effect"),
                priority=r.get("priority"),
                logic_group=r.get("logic_group"),
                operator=r.get("operator"),
                consortium_role="none",
                max_score=r.get("max_score"),
                weight=r.get("weight"),
                score_nature=r.get("score_nature"),
                score_formula=r.get("score_formula"),
                required_by_stage=r.get("required_by_stage"),
                action_status=r.get("action_status"),
                owner=r.get("owner"),
            ))
        session.commit()

        # 校验
        rows = session.scalars(select(Requirement).where(Requirement.rule_set_id == RULE_SET_ID)).all()
        by_type: dict[str, int] = {}
        bad_asof = 0
        for r in rows:
            by_type[r.req_type] = by_type.get(r.req_type, 0) + 1
            if r.as_of != args.as_of:
                bad_asof += 1
        print(f"入库完成：rule_set={RULE_SET_ID} requirements={len(rows)} "
              f"hard={by_type.get('hard_requirement', 0)} scored={by_type.get('scored_requirement', 0)} "
              f"action={by_type.get('action_requirement', 0)} as_of=全部{args.as_of}（异常 {bad_asof}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
