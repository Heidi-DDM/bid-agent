# F008/F021/F023 v1.6（2026-09-23）：规则参数结构化 + 匹配引擎升级 的回归测试。
# 背景（PJ-b4e65720e2 实测）：抽取器只给 type/anchor_key 不给可比对参数、快照层要求
# 核验早于判定时点（30 条有效资质只放行 1 条）、满足项无证据回链被一律降级——
# 硬性要求全部 manual_review/unverifiable。本文件锁定三层修复的行为。
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.parsing.extractor import structure_rule_params
from scripts.matching import engine

AS_OF = "2025-12-19"


# ---------- 抽取层：真实条款原文 → 引擎参数 ----------

def test_params_qualification_from_real_clauses():
    # 河北大学招标文件 H-001 原文（前缀谓语噪声需剥离）
    p = structure_rule_params("qualification_grade", "具备行政主管部门核发的工程设计综合甲级")
    assert p == {"acceptable": [{"category": "工程设计综合", "level": "甲级"}]}
    p = structure_rule_params("qualification_grade", "具备市政公用工程施工总承包一级或以上资质")
    assert p == {"acceptable": [{"category": "市政公用工程施工总承包", "level": "一级"}]}


def test_params_project_manager_and_leads():
    p = structure_rule_params("pm_registered_builder",
                              "拟派项目经理具有注册在投标单位的市政公用工程专业壹级及以上注册建造师执业资格")
    assert p == {"specialty": ["市政公用工程"], "cert_level": "一级"}
    p = structure_rule_params("construction_lead",
                              "拟派施工负责人（建造师）具有市政公用工程专业壹级及以上注册建造师执业资格")
    assert p["cert_level"] == "一级" and p["specialty"] == ["市政公用工程"]
    assert structure_rule_params("pm_b_cert", "同时具有对应有效的安全生产考核合格证书") == {"require_b_cert": True}
    assert structure_rule_params("design_lead", "拟派设计负责人具有国家注册土木工程师") == {"cert": "注册土木工程师"}


def test_params_performance_bond_price_validity_officer():
    p = structure_rule_params("similar_performance_hard",
                              "自2020年9月1日以来完成过一项3000万元及以上市政工程的施工业绩")
    assert p["since"] == "2020-09-01" and p["min_amount"] == 30000000.0
    assert p["project_type"] == "市政工程" and p["subject"] == "bidder"
    p = structure_rule_params("bid_bond", "投标保证金☑要求提交投标保证金1.投标保证金的金额：人民币叁拾万元整")
    assert p["amount"] == 300000.0
    assert structure_rule_params("ceiling_price", "最高投标限价3919.5378万元") == {"max_amount": 39195378.0}
    assert structure_rule_params("bid_validity", "投标有效期120日历天") == {"days": 120}
    p = structure_rule_params("safety_officer", "专职安全生产管理人员具有对应有效的安全生产考核合格证书，配备人数不少于1个")
    assert p == {"require_c_cert": True, "count": 1}
    assert structure_rule_params("financial_audit", "提供2022、2023、2024年度财务审计报告") == {"years": ["2022", "2023", "2024"]}


def test_params_missing_when_unparseable():
    # 抽不出就不写键（引擎回落 manual_review/unverifiable，不推断）
    assert structure_rule_params("qualification_grade", "有效的资质要求详见附件") == {}
    assert structure_rule_params("bid_validity", "投标有效期见前附表") == {}
    assert structure_rule_params("qualification_grade", "__待补__") == {}


# ---------- 引擎层：等级归一 / 类别别名 / 新类型 / 回链 ----------

def _rec(**kw):
    base = {"material_id": "MAT-Q-1", "status": "active", "verified_at": "2025-06-01",
            "evidence_refs": ["ledger:xls:row2", "material:MAT-EVID-1"]}
    base.update(kw)
    return base


def test_level_normalization_design_grades():
    assert engine._level("甲级") == 3 and engine._level("乙级") == 2
    assert engine._level("综合甲级") == 4 and engine._level("特级") == 4
    assert engine._level("壹级") == 3


def test_qualification_design_grade_honest_outcome():
    # 要求工程设计综合甲级；只有行业甲级 → 类别不匹配（不含「综合」）→ not_satisfied（如实）
    ev = {"qualification_record": [_rec(category="工程设计建筑行业（建筑工程）", level="甲级")]}
    rule = {"type": "qualification", "acceptable": [{"category": "工程设计综合", "level": "甲级"}]}
    r, reason, refs = engine._match_hard(rule, ev, AS_OF)[:3]
    assert r == "not_satisfied" and "不满足" in reason


def test_qualification_category_containment_and_refs():
    # 类别互含 + 等级达标 → satisfied 且携带记录 evidence_refs（ADR-004 §2.5 回链）
    ev = {"qualification_record": [_rec(category="市政公用工程施工总承包", level="一级")]}
    rule = {"type": "qualification", "acceptable": [{"category": "市政公用工程施工总承包", "level": "一级"}]}
    r, reason, refs = engine._match_hard(rule, ev, AS_OF)[:3]
    assert r == "satisfied"
    assert refs == ["ledger:xls:row2", "material:MAT-EVID-1"]


def test_business_license_unverifiable_without_record():
    r, reason = engine._match_hard({"type": "business_license"}, {}, AS_OF)[:2]
    assert r == "unverifiable" and "营业执照" in reason


def test_bid_validity_and_ceiling_price_honest():
    # 2026-09-29 用户裁定：投标有效期/限价在判定阶段为信息项（编制时响应/投标时录价），不缺证阻断
    r, reason = engine._match_hard({"type": "bid_validity", "days": 120}, {}, AS_OF)[:2]
    assert r == "not_applicable" and "响应" in reason
    r, reason = engine._match_hard({"type": "ceiling_price", "max_amount": 39195378.0}, {}, AS_OF)[:2]
    assert r == "not_applicable" and "限价" in reason


def test_construction_lead_satisfied_with_available_manager():
    ev = {"manager_profile": [{
        "material_id": "MAT-M-1", "manager_id": "M-1", "display_name": "张三",
        "specialty": ["建筑/市政"], "cert_level": "一级", "b_cert": "B123", "active_projects": [],
        "availability": "available", "status": "active", "verified_at": "2025-06-01",
        "evidence_refs": ["ledger:mgr:row5"],
    }]}
    reqs = [{"requirement_id": "H-CL", "req_type": "hard_requirement", "category": "人员",
             "clause_ref": "§3.3", "assertion": "施工负责人市政壹级建造师", "evidence_required": ["manager_profile"],
             "rule": {"type": "construction_lead", "specialty": ["市政公用工程"], "cert_level": "一级"}}]
    out = engine.evaluate(reqs, ev, as_of=AS_OF, mode="gate")
    m = out["matrix"][0]
    assert m["match_result"] == "satisfied" and "施工负责人" in m["match_reason"]
    assert m.get("evidence_refs") == ["ledger:mgr:row5"]
    assert m.get("person_id") == "M-1" and m.get("candidate_plan_ref")


def test_design_lead_unverifiable_without_cert_record():
    reqs = [{"requirement_id": "H-DL", "req_type": "hard_requirement", "category": "人员",
             "clause_ref": "§3.3", "assertion": "设计负责人国家注册土木工程师", "evidence_required": ["manager_profile"],
             "rule": {"type": "design_lead", "cert": "注册土木工程师"}}]
    out = engine.evaluate(reqs, {"manager_profile": [], "technical_team_member": []}, as_of=AS_OF, mode="gate")
    m = out["matrix"][0]
    assert m["match_result"] == "unverifiable" and m.get("reason_code") == "evidence_missing"


def test_similar_performance_type_alias_and_refs():
    # 招标要「市政工程」业绩，台账类型「市政基础工程」→ 主干词匹配；满足并回链
    ev = {"similar_performance": [{
        "material_id": "MAT-P-1", "project_name": "金钟湖公园改造提升工程项目EPC总承包",
        "project_type": "市政基础工程", "contract_amount": 340970000, "completed_at": "2022-02-20",
        "subject": "bidder", "status": "active", "verified_at": "2025-10-15",
        "evidence_refs": ["ledger:perf:row9"],
    }]}
    rule = {"type": "similar_performance", "since": "2020-09-01", "min_amount": 30000000,
            "project_type": "市政工程", "subject": "bidder"}
    r, reason, refs = engine._match_similar_performance(rule, ev, AS_OF)[:3]
    assert r == "satisfied" and "金钟湖" in reason and refs == ["ledger:perf:row9"]


def test_project_manager_specialty_alias():
    # 要求「市政公用工程」，经理 specialty「市政」→ token 归一后交集命中（经候选班子路径）
    ev = {"manager_profile": [{
        "material_id": "MAT-M-1", "manager_id": "M-1", "specialty": ["市政"], "cert_level": "一级",
        "b_cert": "B1", "active_projects": [], "availability": "available", "status": "active",
        "verified_at": "2025-06-01", "evidence_refs": ["ledger:mgr:row1"],
    }]}
    reqs = [{"requirement_id": "H-PM", "req_type": "hard_requirement", "category": "人员",
             "clause_ref": "§3.3", "assertion": "项目经理市政壹级建造师且持B证", "evidence_required": ["manager_profile"],
             "rule": {"type": "project_manager", "specialty": ["市政公用工程"], "cert_level": "一级", "require_b_cert": True}}]
    out = engine.evaluate(reqs, ev, as_of=AS_OF, mode="gate")
    m = out["matrix"][0]
    assert m["match_result"] == "satisfied" and "张" not in m["match_reason"]  # 未脱敏姓名不进 reason
    assert m.get("evidence_refs") == ["ledger:mgr:row1"]
    assert m.get("person_id") == "M-1"


def test_evidence_verified_after_as_of_still_valid():
    # 核验晚于判定时点仍有效（核验=事后确认）；有效期窗口仍严格
    rec = _rec(valid_until="2028-11-04", verified_at="2026-09-22")
    assert engine.evidence_is_valid(rec, AS_OF) is True
    assert engine.evidence_is_valid(_rec(valid_until="2025-06-01", verified_at="2026-09-22"), AS_OF) is False


def test_satisfied_entries_carry_evidence_refs_in_evaluate():
    reqs = [{
        "requirement_id": "H-001", "req_type": "hard_requirement", "category": "资质",
        "clause_ref": "§3.2", "assertion": "市政公用工程施工总承包一级",
        "evidence_required": ["qualification_record"],
        "rule": {"type": "qualification", "acceptable": [{"category": "市政公用工程施工总承包", "level": "一级"}]},
    }]
    ev = {"qualification_record": [_rec(category="市政公用工程施工总承包", level="一级")]}
    out = engine.evaluate(reqs, ev, as_of=AS_OF, mode="gate")
    entry = out["matrix"][0]
    assert entry["match_result"] == "satisfied"
    assert entry.get("evidence_refs") == ["ledger:xls:row2", "material:MAT-EVID-1"]
