import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from approval.approval_workflow import (
    add_waiver, approve, audit_log, create_approval, decide_waived, expire_waivers, reject, to_dict,
)


def _full_score():
    """构造一个内部准入满分结果（引擎口径）。"""
    return {
        "internal_admission_eligible": True,
        "qualification_result": "passed",
        "scoring_result": "full",
        "operational_readiness": "ready",
        "coverage": {"executed": 16, "declared": 16, "complete": True},
        "blocked": [], "pending": [], "review": [],
    }


class ApprovalWorkflowTests(unittest.TestCase):
    def test_not_full_score_cannot_enter_approval(self):
        # 未满分 → 不得创建审批（F009 §7）
        with self.assertRaises(ValueError):
            create_approval({"internal_admission_eligible": False}, approval_id="A-1",
                            project_id="P-1", approver="经营负责人")

    def test_approve_flow_with_audit(self):
        record = create_approval(_full_score(), approval_id="A-1", project_id="P-1", approver="经营负责人")
        approve(record, approver="经营负责人", at="2026-08-28T10:00:00", comment="同意投标")
        self.assertEqual(record.decision, "approved")
        entries = audit_log(record)
        self.assertEqual([e["action"] for e in entries], ["create_approval", "approve"])
        self.assertEqual(entries[-1]["outcome"], "approved_for_bidding")
        self.assertEqual(entries[-1]["actor"], "经营负责人")

    def test_reject_requires_comment(self):
        record = create_approval(_full_score(), approval_id="A-2", project_id="P-2", approver="经营负责人")
        with self.assertRaises(ValueError):
            reject(record, approver="经营负责人", at="2026-08-28T10:00:00", comment="")
        reject(record, approver="经营负责人", at="2026-08-28T10:00:00", comment="风险过高，暂不投标")
        self.assertEqual(record.decision, "rejected")

    def test_double_decision_forbidden(self):
        record = create_approval(_full_score(), approval_id="A-3", project_id="P-3", approver="经营负责人")
        approve(record, approver="经营负责人", at="2026-08-28T10:00:00")
        with self.assertRaises(ValueError):
            reject(record, approver="经营负责人", at="2026-08-28T10:05:00", comment="不应重复决策")

    def test_waiver_requires_reason_evidence_validity(self):
        record = create_approval(_full_score(), approval_id="A-4", project_id="P-4", approver="经营负责人")
        with self.assertRaises(ValueError):
            add_waiver(record, waiver_id="W-1", authorizer="经营负责人", reason="", evidence_refs=["E-1"],
                       valid_until="2026-09-01", approved_at="2026-08-28")
        with self.assertRaises(ValueError):
            add_waiver(record, waiver_id="W-2", authorizer="经营负责人", reason="材料在途", evidence_refs=[],
                       valid_until="2026-09-01", approved_at="2026-08-28")
        with self.assertRaises(ValueError):
            add_waiver(record, waiver_id="W-3", authorizer="经营负责人", reason="材料在途",
                       evidence_refs=["E-1"], valid_until="bad-date", approved_at="2026-08-28")

    def test_waived_requires_active_waiver(self):
        record = create_approval(_full_score(), approval_id="A-5", project_id="P-5", approver="经营负责人")
        with self.assertRaises(ValueError):
            decide_waived(record, approver="经营负责人", at="2026-08-28T10:00:00", comment="无豁免不可带豁免批准")

    def test_expired_waiver_blocks_pending_approval(self):
        record = create_approval(_full_score(), approval_id="A-6", project_id="P-6", approver="经营负责人")
        add_waiver(record, waiver_id="W-9", authorizer="经营负责人", reason="材料在途",
                   evidence_refs=["E-1"], valid_until="2026-08-01", approved_at="2026-07-28")
        expire_waivers(record, as_of="2026-08-28")
        self.assertEqual(record.decision, "blocked_waiver_expired")

    def test_waived_flow_keeps_audit(self):
        record = create_approval(_full_score(), approval_id="A-7", project_id="P-7", approver="经营负责人")
        add_waiver(record, waiver_id="W-7", authorizer="经营负责人", reason="审计报告在途",
                   evidence_refs=["受理回执 E-7"], valid_until="2026-09-30", approved_at="2026-08-28",
                   covered_items=["NQ-H-007"])
        decide_waived(record, approver="经营负责人", at="2026-08-28T11:00:00", comment="带豁免批准，跟进材料")
        self.assertEqual(record.decision, "waived")
        actions = [e["action"] for e in audit_log(record)]
        self.assertEqual(actions, ["create_approval", "add_waiver", "decide_waived"])

    def test_export_is_serializable(self):
        record = create_approval(_full_score(), approval_id="A-8", project_id="P-8", approver="经营负责人")
        approve(record, approver="经营负责人", at="2026-08-28T10:00:00")
        data = to_dict(record)
        self.assertEqual(data["state"], "approved")
        self.assertEqual(len(data["audit"]), 2)


if __name__ == "__main__":
    unittest.main()