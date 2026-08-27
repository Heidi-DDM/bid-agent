import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from matching.engine import evaluate, evidence_is_valid, validate_requirements

GOLDEN = json.loads((ROOT / "matching/golden_requirements.json").read_text(encoding="utf-8"))


class MatchingEngineTests(unittest.TestCase):
    def test_golden_is_complete_and_unique(self):
        result = validate_requirements(GOLDEN, expected_count=16)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["counts"], {"hard_requirement": 9, "scored_requirement": 3, "action_requirement": 4})

    def test_verification_cannot_be_after_as_of(self):
        evidence = {"status": "active", "verified_at": "2026-08-27", "valid_until": "2029-01-01"}
        self.assertFalse(evidence_is_valid(evidence, "2024-05-15"))
        self.assertTrue(evidence_is_valid(evidence, "2026-08-27"))

    def test_financial_year_and_credit_checks_are_enforced(self):
        evidence = {
            "financial_report": [{"year": 2021, "status": "valid", "verified_at": "2024-01-01"}],
            "credit_check_report": [{"checks": ["credit_china"], "status": "valid", "verified_at": "2024-01-01"}],
        }
        result = evaluate(GOLDEN[:4], evidence, as_of="2024-05-15", mode="diagnostic")
        self.assertEqual(result["matrix"][2]["match_result"], "unverifiable")
        self.assertEqual(result["matrix"][3]["match_result"], "unverifiable")

    def test_diagnostic_executes_all_sixteen_and_gate_short_circuits(self):
        evidence = {
            "qualification_record": [{"category": "建筑工程施工总承包", "level": "一级", "status": "active",
                                        "verified_at": "2024-01-01", "valid_until": "2028-01-01"}]
        }
        diagnostic = evaluate(GOLDEN, evidence, as_of="2024-05-15", mode="diagnostic")
        gate = evaluate(GOLDEN, evidence, as_of="2024-05-15", mode="gate")
        self.assertEqual(diagnostic["coverage"], {"executed": 16, "declared": 16, "complete": True})
        self.assertLess(gate["coverage"]["executed"], 16)

    def test_manager_specialty_and_b_cert_are_required(self):
        evidence = {
            "manager_profile": [{"manager_id": "PM-1", "specialty": ["建筑工程"], "cert_level": "一级", "b_cert": False,
                                  "status": "active", "availability": "available", "active_projects": [],
                                  "verified_at": "2024-01-01", "valid_until": "2028-01-01"}]
        }
        result = evaluate([GOLDEN[4]], evidence, as_of="2024-05-15")
        self.assertEqual(result["matrix"][0]["match_result"], "unverifiable")

    def test_expired_evidence_is_unverifiable(self):
        # 证据有效期不覆盖 as_of → unverifiable，不得判满足（F008 §9.1 时点门禁）
        evidence = {
            "qualification_record": [{"category": "市政公用工程施工总承包", "level": "一级", "status": "active",
                                        "verified_at": "2024-01-01", "valid_until": "2024-03-01"}]
        }
        result = evaluate([GOLDEN[0]], evidence, as_of="2024-05-15")
        self.assertEqual(result["matrix"][0]["match_result"], "unverifiable")

    def test_manager_active_project_conflict_is_unverifiable(self):
        # 专业/等级/B证全满足但在建 → 不得判满足（一票否决语义；缺无在建证据 → unverifiable）
        evidence = {
            "manager_profile": [{"manager_id": "PM-2", "specialty": ["市政公用工程"], "cert_level": "一级", "b_cert": True,
                                  "status": "active", "availability": "available", "active_projects": ["某在施项目"],
                                  "verified_at": "2024-01-01", "valid_until": "2028-01-01"}]
        }
        result = evaluate([GOLDEN[4]], evidence, as_of="2024-05-15")
        self.assertEqual(result["matrix"][0]["match_result"], "unverifiable")

    def test_bid_bond_amount_or_form_mismatch_is_not_satisfied(self):
        # 金额不符 → not_satisfied（阻断）；形式不符 → not_satisfied
        wrong_amount = {"bid_bond": [{"amount": 50000, "form": "银行电汇", "status": "valid",
                                      "verified_at": "2024-05-01", "valid_until": "2024-06-01"}]}
        wrong_form = {"bid_bond": [{"amount": 100000, "form": "现金", "status": "valid",
                                    "verified_at": "2024-05-01", "valid_until": "2024-06-01"}]}
        r1 = evaluate([GOLDEN[7]], wrong_amount, as_of="2024-05-15", mode="diagnostic")
        r2 = evaluate([GOLDEN[7]], wrong_form, as_of="2024-05-15", mode="diagnostic")
        self.assertEqual(r1["matrix"][0]["match_result"], "not_satisfied")
        self.assertEqual(r2["matrix"][0]["match_result"], "not_satisfied")
        self.assertTrue(r1["blocked"])

    def test_quote_must_be_entered_by_person(self):
        # 报价未人工录入 → unverifiable，系统不生成/推荐报价
        result = evaluate([GOLDEN[9]], {}, as_of="2024-05-15")
        self.assertEqual(result["matrix"][0]["match_result"], "unverifiable")
        self.assertIn("人员录入", result["matrix"][0]["match_reason"])

    def test_response_document_missing_is_unverifiable(self):
        # 响应性证据缺失（投标有效期承诺/响应文件）→ unverifiable
        r1 = evaluate([GOLDEN[6]], {}, as_of="2024-05-15")
        r2 = evaluate([GOLDEN[8]], {}, as_of="2024-05-15")
        self.assertEqual(r1["matrix"][0]["match_result"], "unverifiable")
        self.assertEqual(r2["matrix"][0]["match_result"], "unverifiable")

    def test_bank_credit_alternative_satisfies_financial(self):
        # 财务：银行资信证明（2023）可替代审计报告（条款为"或"）
        evidence = {"bank_credit": [{"year": 2023, "status": "valid", "verified_at": "2024-01-01"}]}
        result = evaluate([GOLDEN[2]], evidence, as_of="2024-05-15")
        self.assertEqual(result["matrix"][0]["match_result"], "satisfied")

    def test_credit_negative_record_is_not_satisfied(self):
        # 信用三查存在失信记录 → not_satisfied（阻断）
        evidence = {
            "credit_check_report": [{"checks": ["credit_china", "business_credit_info", "enforcement_info"],
                                     "negative": True, "status": "valid", "verified_at": "2024-01-01"}]
        }
        result = evaluate([GOLDEN[3]], evidence, as_of="2024-05-15", mode="diagnostic")
        self.assertEqual(result["matrix"][0]["match_result"], "not_satisfied")
        self.assertTrue(result["blocked"])

    def test_lot_isolation_filters_rules_and_evidence(self):
        # 标段隔离（F008 §4.1）：lot_id=5 的保证金回执只在标段 5 内有效，跨标段被过滤
        base = [r for r in GOLDEN if r["requirement_id"] != "RQ-H-008"]
        bond_5 = dict(GOLDEN[7], lot_id=5)
        bond_3 = dict(GOLDEN[7], lot_id=3, rule={"type": "bid_bond", "amount": 240000,
                                                  "forms": ["保函", "银行电汇", "电子保函", "保证保险"]})
        reqs = base + [bond_5, bond_3]
        evidence = {"bid_bond": [{"amount": 100000, "form": "银行电汇", "status": "valid",
                                  "verified_at": "2024-05-01", "valid_until": "2024-06-01", "lot_id": 5}]}
        r5 = evaluate(reqs, evidence, as_of="2024-05-15", mode="diagnostic", lot_id=5)
        r3 = evaluate(reqs, evidence, as_of="2024-05-15", mode="diagnostic", lot_id=3)
        m5 = next(m for m in r5["matrix"] if m["requirement_id"] == "RQ-H-008")
        m3 = next(m for m in r3["matrix"] if m["requirement_id"] == "RQ-H-008")
        self.assertEqual(m5["match_result"], "satisfied")
        self.assertEqual(m3["match_result"], "unverifiable")
        self.assertEqual(r5["coverage"]["declared"], 16)
        self.assertEqual(r3["coverage"]["declared"], 16)
        self.assertEqual(r5["coverage"]["complete"], True)


if __name__ == "__main__":
    unittest.main()
