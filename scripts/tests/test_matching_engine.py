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

    def test_verification_after_as_of_is_valid_but_window_enforced(self):
        # F023 v1.6（2026-09-23）：核验是对既有事实的事后确认，核验时间可晚于判定时点；
        # 有效性仍严格按 valid_from/valid_until 对 as_of 判定（docs/12 §3.2 状态拆分：
        # 过期属 certificate_expired 事实，不覆盖窗口属待核验）。
        evidence = {"status": "active", "verified_at": "2026-08-27", "valid_until": "2029-01-01"}
        self.assertTrue(evidence_is_valid(evidence, "2024-05-15"))
        self.assertTrue(evidence_is_valid(evidence, "2026-08-27"))
        expired = {"status": "active", "verified_at": "2026-08-27", "valid_until": "2024-01-01"}
        self.assertFalse(evidence_is_valid(expired, "2024-05-15"))

    def test_financial_year_and_credit_checks_are_enforced(self):
        evidence = {
            "financial_report": [{"year": 2021, "status": "valid", "verified_at": "2024-01-01"}],
            "credit_check_report": [{"checks": ["credit_china"], "status": "valid", "verified_at": "2024-01-01"}],
        }
        result = evaluate(GOLDEN[:4], evidence, as_of="2024-05-15", mode="diagnostic")
        self.assertEqual(result["matrix"][2]["match_result"], "unverifiable")
        # 2026-09-29 用户裁定：失信=状态自查项——有已核验无失信记录时如实「满足」；
        # 无记录时为 not_applicable 自查项，均不再作缺证阻断
        self.assertEqual(result["matrix"][3]["match_result"], "satisfied")

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
        # 专业与规则要求不符 → 事实性不满足（docs/12 §3.2 profession_mismatch），
        # 不再以「无一人同时满足」合写为 unverifiable
        self.assertEqual(result["matrix"][0]["match_result"], "not_satisfied")
        self.assertEqual(result["matrix"][0]["reason_code"], "profession_mismatch")

    def test_expired_evidence_is_unverifiable(self):
        # 证据有效期不覆盖 as_of → unverifiable，不得判满足（F008 §9.1 时点门禁）
        evidence = {
            "qualification_record": [{"category": "市政公用工程施工总承包", "level": "一级", "status": "active",
                                        "verified_at": "2024-01-01", "valid_until": "2024-03-01"}]
        }
        result = evaluate([GOLDEN[0]], evidence, as_of="2024-05-15")
        self.assertEqual(result["matrix"][0]["match_result"], "unverifiable")

    def test_manager_active_project_conflict_is_not_satisfied(self):
        # 专业/等级/B证全满足但在建 → 明确不满足（docs/12 §3.2：onsite_conflict 属事实性
        # 资源冲突，处置是调整在施/换人，不得伪装缺证据 unverifiable）
        evidence = {
            "manager_profile": [{"manager_id": "PM-2", "specialty": ["市政公用工程"], "cert_level": "一级", "b_cert": True,
                                  "status": "active", "availability": "available", "active_projects": ["某在施项目"],
                                  "verified_at": "2024-01-01", "valid_until": "2028-01-01"}]
        }
        result = evaluate([GOLDEN[4]], evidence, as_of="2024-05-15")
        self.assertEqual(result["matrix"][0]["match_result"], "not_satisfied")
        self.assertEqual(result["matrix"][0]["reason_code"], "onsite_conflict")

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
        # 报价未人工录入 → not_calculable（docs/12 §3.2：authorized_input_missing），
        # 系统不生成/推荐报价
        result = evaluate([GOLDEN[9]], {}, as_of="2024-05-15")
        self.assertEqual(result["matrix"][0]["match_result"], "not_calculable")
        self.assertEqual(result["matrix"][0]["reason_code"], "authorized_input_missing")
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
        # 2026-09-29 用户裁定：保证金无凭证=投标执行事项（递交前办理），不再缺证阻断
        self.assertEqual(m3["match_result"], "not_applicable")
        self.assertEqual(r5["coverage"]["declared"], 16)
        self.assertEqual(r3["coverage"]["declared"], 16)
        self.assertEqual(r5["coverage"]["complete"], True)


    def test_scored_without_score_formula_is_not_calculable(self):
        # R021 真库回归（2026-09-02）：自动锚点抽取的 scored requirement 无评分细则
        # （score_formula/max_score 列 NULL，DB 行转 dict 后为 None）→ engine 不得崩
        # （None.get AttributeError），落 manual_review「评分公式或输入不完整」等人工补公式
        item = {
            "requirement_id": "RQ-S-NULL-SF",
            "req_type": "scored_requirement",
            "category": "企业业绩",
            "clause_ref": "评标办法 第三章 四(5)",
            "assertion": "单体建筑面积≥2万平方米的房屋建筑类业绩，每项得 2.5 分，最高 5 分",
            "rule": {"type": "generic", "anchor_key": "scored"},
            "evidence_required": [],
            "score_formula": None,
            "max_score": None,
            "score_nature": None,
        }
        result = evaluate([item], {}, as_of="2024-05-15", mode="diagnostic")
        m = result["matrix"][0]
        self.assertEqual(m["match_result"], "not_calculable")
        self.assertEqual(m["reason_code"], "formula_missing")
        self.assertIn("评分公式", m["match_reason"])
        self.assertIsNone(m["score"])
        self.assertTrue(result["review"])


class NongdaRulesTests(unittest.TestCase):
    """农大版规则（20 条）与新规则类型（安全员/技术团队/报价上限/社保/类似业绩）回归。"""

    @classmethod
    def setUpClass(cls):
        cls.REQS = json.loads((ROOT / "matching/golden_requirements_nongda.json").read_text(encoding="utf-8"))

    def test_nongda_ruleset_complete_and_unique(self):
        result = validate_requirements(self.REQS, expected_count=20)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["counts"], {"hard_requirement": 12, "scored_requirement": 4, "action_requirement": 4})

    def test_quote_cap_rejects_above_limit(self):
        rule = [r for r in self.REQS if r["requirement_id"] == "NQ-H-012"][0]
        over = {"bid_price_input": [{"type": "quoted_price", "amount": 130000000.0, "entered_by": "人工", "status": "valid", "verified_at": "2025-10-20"}]}
        result = evaluate([rule], over, as_of="2025-10-30", mode="diagnostic")
        self.assertEqual(result["matrix"][0]["match_result"], "not_satisfied")
        self.assertTrue(result["blocked"])

    def test_quote_cap_accepts_within_limit(self):
        rule = [r for r in self.REQS if r["requirement_id"] == "NQ-H-012"][0]
        ok = {"bid_price_input": [{"type": "quoted_price", "amount": 121598056.76, "entered_by": "人工", "status": "valid", "verified_at": "2025-10-20"}]}
        result = evaluate([rule], ok, as_of="2025-10-30", mode="diagnostic")
        self.assertEqual(result["matrix"][0]["match_result"], "satisfied")

    def test_safety_officer_count_insufficient(self):
        # docs/12 8.1-1 状态拆分：已核验 1 人 < 要求 3 人 → 事实性不满足（数量不足），
        # 不再与「证据缺失」合写为 unverifiable
        rule = [r for r in self.REQS if r["requirement_id"] == "NQ-H-005"][0]
        ev = {"safety_officer_cert": [{"cert_type": "C", "status": "valid", "verified_at": "2025-09-01"}]}
        result = evaluate([rule], ev, as_of="2025-10-30", mode="diagnostic")
        m = result["matrix"][0]
        self.assertEqual(m["match_result"], "not_satisfied")
        self.assertEqual(m["reason_code"], "verified_quantity_insufficient")
        self.assertEqual(m["observed_value"]["value"], 1)
        self.assertEqual(m["required_value"]["value"], 2)

    def test_technical_team_missing_specialty(self):
        rule = [r for r in self.REQS if r["requirement_id"] == "NQ-H-006"][0]
        ev = {"technical_team_member": [
            {"specialty": "建筑工程", "status": "valid", "verified_at": "2025-09-01"},
            {"specialty": "给排水", "status": "valid", "verified_at": "2025-09-01"}]}
        result = evaluate([rule], ev, as_of="2025-10-30", mode="diagnostic")
        self.assertEqual(result["matrix"][0]["match_result"], "unverifiable")

    def test_social_security_insufficient_months(self):
        rule = [r for r in self.REQS if r["requirement_id"] == "NQ-H-004"][0]
        ev = {"social_security_proof": [{"subject": "project_manager", "continuous_months": 1, "status": "valid", "verified_at": "2025-09-01"}]}
        result = evaluate([rule], ev, as_of="2025-10-30", mode="diagnostic")
        self.assertEqual(result["matrix"][0]["match_result"], "unverifiable")

    def test_similar_performance_matches_subject(self):
        rule = [r for r in self.REQS if r["requirement_id"] == "NQ-S-003"][0]
        ev = {"similar_performance": [
            {"subject": "bidder", "project_name": "某医院项目", "area": 35724.0, "project_type": "房屋建筑",
             "completed_at": "2023-10-11", "status": "valid", "verified_at": "2025-09-01"}]}
        result = evaluate([rule], ev, as_of="2025-10-30", mode="diagnostic")
        self.assertEqual(result["matrix"][0]["match_result"], "satisfied")
        self.assertEqual(result["matrix"][0]["score"], 2.5)

    def test_similar_performance_area_too_small(self):
        rule = [r for r in self.REQS if r["requirement_id"] == "NQ-S-003"][0]
        ev = {"similar_performance": [
            {"subject": "bidder", "project_name": "小面积项目", "area": 15000.0, "project_type": "房屋建筑",
             "completed_at": "2023-10-11", "status": "valid", "verified_at": "2025-09-01"}]}
        result = evaluate([rule], ev, as_of="2025-10-30", mode="diagnostic")
        self.assertEqual(result["matrix"][0]["match_result"], "not_calculable")

    def test_similar_performance_outside_window(self):
        rule = [r for r in self.REQS if r["requirement_id"] == "NQ-S-003"][0]
        ev = {"similar_performance": [
            {"subject": "bidder", "project_name": "窗口外项目", "area": 30000.0, "project_type": "房屋建筑",
             "completed_at": "2022-06-01", "status": "valid", "verified_at": "2025-09-01"}]}
        result = evaluate([rule], ev, as_of="2025-10-30", mode="diagnostic")
        self.assertEqual(result["matrix"][0]["match_result"], "not_calculable")

    def test_similar_performance_subject_mismatch_not_shared(self):
        # 投标人业绩与项目经理业绩不可通用（招标文件 第三章四(5) 备注 c）
        rule = [r for r in self.REQS if r["requirement_id"] == "NQ-S-004"][0]
        ev = {"similar_performance": [
            {"subject": "bidder", "project_name": "仅投标人业绩", "area": 30000.0, "project_type": "房屋建筑",
             "completed_at": "2023-10-11", "status": "valid", "verified_at": "2025-09-01"}]}
        result = evaluate([rule], ev, as_of="2025-10-30", mode="diagnostic")
        self.assertEqual(result["matrix"][0]["match_result"], "not_calculable")

    def test_nongda_real_evidence_no_blocked(self):
        # 真实脱敏证据重跑：无阻断；仅财务审计素材缺口待补（当年必有 → 待公司补充）
        from matching.engine import evaluate as ev
        restricted = ROOT.parent / "验证受限材料/农大"
        if not (restricted / "real_evidence_nongda.json").exists():
            self.skipTest("受限证据未就位（本地验证用）")
        evidence = json.loads((restricted / "real_evidence_nongda.json").read_text(encoding="utf-8"))
        result = ev(self.REQS, evidence, as_of="2025-10-30", mode="diagnostic")
        self.assertEqual(result["coverage"], {"executed": 20, "declared": 20, "complete": True})
        self.assertEqual(result["blocked"], [])
        self.assertEqual(result["operational_readiness"]["status"], "ready")
        self.assertEqual([m["requirement_id"] for m in result["pending"]], ["NQ-H-007"])


if __name__ == "__main__":
    unittest.main()
