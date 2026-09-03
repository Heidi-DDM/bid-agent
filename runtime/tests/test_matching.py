# docs/07 方案 §3.5 / F023 §2.3/§6：匹配接入纯逻辑测试（matching.py）
# 覆盖：active 快照过滤、证据快照 hash 稳定性、规则约束提取、
# 候选核验 blocked 记录剔除、规则引擎接入（可注入）、缺规则集报错。
from __future__ import annotations

from decimal import Decimal

import pytest

from runtime.core import matching
from runtime.core.matching import MatchNotRunnableError
from runtime.rag import verification

pytestmark = pytest.mark.rag


def _qualification(**overrides):
    base = {
        "material_id": "MAT-Q-1", "category": "建筑工程施工总承包", "level": "二级",
        "status": "active", "verified_at": "2025-01-10",
        "valid_from": "2024-01-01", "valid_until": "2027-01-01",
        "evidence_refs": ["EV-Q-1"],
    }
    base.update(overrides)
    return base


# ---------- 快照构建 ----------

def test_build_enterprise_evidence_filters_inactive():
    evidence = matching.build_enterprise_evidence(
        qualifications=[_qualification(), _qualification(material_id="MAT-Q-2", status="expired")],
        as_of="2025-10-30",
    )
    ids = [r["material_id"] for r in evidence["qualification_record"]]
    assert ids == ["MAT-Q-1"]


def test_build_enterprise_evidence_filters_unverified():
    evidence = matching.build_enterprise_evidence(
        qualifications=[_qualification(verified_at=None), _qualification(verified_at="2026-01-01")],
        as_of="2025-10-30",
    )
    assert "qualification_record" not in evidence  # 未核验/核验晚于 as_of 均不入快照


def test_build_enterprise_evidence_filters_validity():
    evidence = matching.build_enterprise_evidence(
        qualifications=[
            _qualification(valid_until="2025-06-01"),   # as_of 前过期
            _qualification(valid_from="2026-06-01"),    # as_of 后生效
        ],
        as_of="2025-10-30",
    )
    assert "qualification_record" not in evidence


def test_performance_missing_verified_at_not_inferred():
    # Performance 未核验（verified_at 为空，0005 补齐后仍可缺核验）：不入证据快照，
    # engine 判 unverifiable（缺失阻断，不推断满足）
    evidence = matching.build_enterprise_evidence(
        performances=[{
            "material_id": "MAT-P-1", "project_name": "宿舍楼", "project_type": "房屋建筑",
            "scale_metrics": {"area": 12000.0}, "contract_amount": Decimal("80000000"),
            "completed_at": "2024-06-01", "status": "active", "verified_at": None,
        }],
        as_of="2025-10-30",
    )
    assert "similar_performance" not in evidence  # 未核验不入快照


def test_performance_verified_at_enters_snapshot():
    # F022 补齐后：verified_at <= as_of 的 active 业绩进入时点有效证据快照，
    # 类似业绩客观项（NQ-S-003/004）在运行时恢复可判定
    evidence = matching.build_enterprise_evidence(
        performances=[{
            "material_id": "MAT-P-1", "project_name": "石家庄学院实训基地项目施工",
            "project_type": "房屋建筑", "scale_metrics": {"area": 34199.93},
            "contract_amount": Decimal("120000000"), "completed_at": "2022-09-30",
            "status": "active", "verified_at": "2025-10-15",
        }],
        as_of="2025-10-30",
    )
    records = evidence["similar_performance"]
    assert len(records) == 1
    assert records[0]["verified_at"] == "2025-10-15"
    assert records[0]["area"] == 34199.93


def test_manager_profile_mapping():
    evidence = matching.build_enterprise_evidence(
        managers=[{
            "material_id": "MAT-M-1", "manager_id": "PM-0001", "display_name": "张**",
            "specialty": "建筑工程", "cert_level": "一级", "cert_valid_until": "2027-01-01",
            "active_projects": [], "availability": "available", "status": "active",
            "verified_at": "2025-01-10", "evidence_refs": [],
        }],
        as_of="2025-10-30",
    )
    records = evidence.get("manager_profile", [])
    assert records and records[0]["specialty"] == ["建筑工程"]
    assert records[0]["valid_until"] == "2027-01-01"


def test_manager_active_projects_string_jsonb_tolerated():
    """JSONB 列被 normalize 污染成字符串 '[]' 的历史数据 → 容错为空列表（R022 回归）。"""
    evidence = matching.build_enterprise_evidence(
        managers=[{
            "manager_id": "PM-0001", "display_name": "张**", "specialty": "建筑工程",
            "cert_level": "一级", "b_cert_no": "冀建安B(2023)0001",
            "active_projects": "[]", "availability": "available", "status": "active",
            "verified_at": "2025-01-10",
        }],
        as_of="2025-10-30",
    )
    records = evidence.get("manager_profile", [])
    assert records and records[0]["active_projects"] == []


def test_safety_license_and_safety_officer_split():
    """R022：安全生产许可证单列 safety_license；C 证安全员单列 safety_officer_cert。"""
    evidence = matching.build_enterprise_evidence(
        qualifications=[
            {"category": "建筑工程施工总承包", "level": "特级", "status": "active",
             "verified_at": "2025-09-15", "evidence_refs": ["E1"]},
            {"category": "安全生产许可证", "level": "不分等级", "status": "active",
             "verified_at": "2025-09-15", "evidence_refs": ["E2"]},
        ],
        personnel=[
            {"personnel_id": "P1", "specialty": "安全", "cert_level": "C",
             "status": "active", "verified_at": "2025-09-20"},
            {"personnel_id": "P2", "specialty": "暖通", "cert_level": "中级",
             "status": "active", "verified_at": "2025-09-20"},
        ],
        as_of="2025-10-30",
    )
    assert len(evidence.get("qualification_record", [])) == 1
    assert len(evidence.get("safety_license", [])) == 1
    assert evidence["safety_license"][0]["category"] == "安全生产许可证"
    assert len(evidence.get("safety_officer_cert", [])) == 1
    assert evidence["safety_officer_cert"][0]["cert_type"] == "C"
    assert len(evidence.get("technical_team_member", [])) == 1


# ---------- 快照哈希 ----------

def test_snapshot_hash_stable_and_sensitive():
    snapshot = {"qualification_record": [{"category": "A", "level": "一级"}]}
    assert matching.snapshot_hash(snapshot) == matching.snapshot_hash(snapshot)
    changed = {"qualification_record": [{"category": "A", "level": "二级"}]}
    assert matching.snapshot_hash(snapshot) != matching.snapshot_hash(changed)


# ---------- 约束提取与候选核验 ----------

def test_candidate_required_from_rule():
    required = matching.candidate_required_from_rule(
        {"type": "qualification", "level": "二级"}, "2025-10-30"
    )
    assert required["level_min"] == "二级"
    assert required["valid_as_of"] == "2025-10-30"
    required = matching.candidate_required_from_rule(
        {"type": "similar_performance", "min_area": 10000, "since": "2022-09-01"}, "2025-10-30"
    )
    assert required["amount_min"] == Decimal("10000")
    assert required["date_after"] == "2022-09-01"
    assert matching.candidate_required_from_rule({}, "2025-10-30") == {"valid_as_of": "2025-10-30"}


def test_gate_candidates_blocked_refs():
    candidates = [
        {"evidence_ref": "MAT-1", "level": "待核实"},
        {"evidence_ref": "MAT-2", "level": "一级"},
    ]
    result = matching.gate_candidates(candidates, {"level_min": "二级", "valid_as_of": "2025-10-30"})
    assert matching.blocked_material_ids(result) == ["MAT-1"]  # 无法判定 → 剔除
    assert all(item.evidence_ref == "MAT-2" for item in result.passed)


def test_verification_summary_serializable():
    result = matching.gate_candidates([{"evidence_ref": "MAT-1", "level": "一级"}],
                                      {"level_min": "二级"})
    summary = matching.verification_summary(result)
    assert summary["passed"] and summary["passed"][0]["evidence_ref"] == "MAT-1"
    assert summary["all_passed"] is True


# ---------- 规则引擎接入 ----------

def test_run_match_requires_requirements():
    with pytest.raises(MatchNotRunnableError):
        matching.run_match(requirements=[], evidence={}, as_of="2025-10-30", evaluate_fn=lambda *a, **k: {})


def test_run_match_injects_evaluate_fn():
    seen = {}

    def fake_evaluate(requirements, evidence, *, as_of, mode="gate", lot_id=None):
        seen.update(as_of=as_of, mode=mode, count=len(requirements))
        return {"coverage": {"executed": 1, "declared": 1, "complete": True}, "matrix": []}

    result = matching.run_match(
        requirements=[{"requirement_id": "R1", "req_type": "hard_requirement"}],
        evidence={}, as_of="2025-10-30", evaluate_fn=fake_evaluate,
    )
    assert seen == {"as_of": "2025-10-30", "mode": "gate", "count": 1}
    assert result["coverage"]["complete"] is True


def test_run_match_default_engine_rejects_blocked_evidence():
    # 默认引擎（scripts.matching.engine）：有资质证据但等级不足 → not_satisfied
    requirements = [{
        "requirement_id": "NQ-H-001", "req_type": "hard_requirement", "category": "资质",
        "clause_ref": "R1", "assertion": "具备建筑工程施工总承包一级资质",
        "evidence_required": ["qualification_record"],
        "rule": {"type": "qualification", "qualification_type": "建筑工程施工总承包", "level": "一级"},
    }]
    evidence = {
        "qualification_record": [{
            "category": "建筑工程施工总承包", "level": "二级", "status": "active",
            "verified_at": "2025-01-10", "valid_from": "2024-01-01", "valid_until": "2027-01-01",
        }]
    }
    result = matching.run_match(requirements=requirements, evidence=evidence, as_of="2025-10-30")
    assert result["coverage"]["complete"] is True
    assert result["matrix"][0]["match_result"] == "not_satisfied"
    assert result["internal_admission_eligible"] is False


def test_run_match_default_engine_missing_evidence_unverifiable():
    requirements = [{
        "requirement_id": "NQ-H-002", "req_type": "hard_requirement", "category": "资质",
        "clause_ref": "R2", "assertion": "具备建筑工程施工总承包一级资质",
        "evidence_required": ["qualification_record"],
        "rule": {"type": "qualification", "qualification_type": "建筑工程施工总承包", "level": "一级"},
    }]
    result = matching.run_match(requirements=requirements, evidence={}, as_of="2025-10-30")
    # 召回不到证据 → unverifiable（缺失阻断），不得由语义相似证据满足
    assert result["matrix"][0]["match_result"] == "unverifiable"
    assert result["internal_admission_eligible"] is False


def test_candidate_from_record_kind_specific_fields():
    rec = {"material_id": "MAT-P-1", "area": 15000.0, "completed_at": "2024-06-01",
           "valid_from": "2024-01-01", "valid_until": None}
    cand = matching.candidate_from_record("similar_performance", rec, content_hash="h" * 64)
    assert cand["evidence_ref"] == "MAT-P-1"
    assert cand["amount"] == "15000.0"
    assert cand["date"] == "2024-06-01"
    assert "level" not in cand  # 业绩候选不带等级字段
    q = matching.candidate_from_record("qualification_record", {"material_id": "MAT-Q-1", "level": "一级"})
    assert q["level"] == "一级"
    assert "amount" not in q