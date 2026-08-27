"""Small, deterministic matching engine used by the lab and regression tests.

The engine deliberately treats missing or temporally invalid evidence as
``unverifiable``.  It does not infer legal qualification or generate a bid.
"""
from __future__ import annotations

from datetime import date
from typing import Any


RESULTS = {"satisfied", "not_satisfied", "unverifiable", "manual_review"}


def _day(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def evidence_is_valid(item: dict[str, Any], as_of: str) -> bool:
    """Require status, verification and validity at the evaluation time."""
    point = _day(as_of)
    verified = _day(item.get("verified_at"))
    valid_from = _day(item.get("valid_from") or item.get("issued_at"))
    valid_until = _day(item.get("valid_until") or item.get("cert_valid_until"))
    if not point or item.get("status") not in ("active", "valid") or not verified:
        return False
    if verified > point or (valid_from and valid_from > point):
        return False
    return not valid_until or valid_until >= point


def _evidence(evidence: dict[str, list[dict[str, Any]]], kind: str, as_of: str) -> list[dict[str, Any]]:
    return [item for item in evidence.get(kind, []) if evidence_is_valid(item, as_of)]


def _level(value: Any) -> int:
    normalized = str(value or "").replace("壹", "一").replace("贰", "二").replace("叁", "三")
    return {"三级": 1, "二级": 2, "一级": 3, "特级": 4}.get(normalized, -1)


def validate_requirements(requirements: list[dict[str, Any]], expected_count: int | None = None) -> dict[str, Any]:
    """Fail fast on incomplete or duplicate rule inputs."""
    ids = [r.get("requirement_id") for r in requirements]
    errors = []
    if any(not value for value in ids):
        errors.append("requirement_id 不能为空")
    if len(ids) != len(set(ids)):
        errors.append("requirement_id 必须唯一")
    if expected_count is not None and len(requirements) != expected_count:
        errors.append(f"要求数量应为 {expected_count}，实际 {len(requirements)}")
    for item in requirements:
        required_fields = ["req_type", "category", "clause_ref", "assertion", "evidence_required"]
        if item.get("req_type") != "action_requirement":
            required_fields.append("rule")
        for field in required_fields:
            if field not in item:
                errors.append(f"{item.get('requirement_id', '<unknown>')} 缺少 {field}")
    counts = {kind: sum(r.get("req_type") == kind for r in requirements)
              for kind in ("hard_requirement", "scored_requirement", "action_requirement")}
    return {"valid": not errors, "errors": errors, "count": len(requirements), "counts": counts}


def _match_hard(rule: dict[str, Any], evidence: dict[str, list[dict[str, Any]]], as_of: str) -> tuple[str, str]:
    kind = rule.get("type")
    if kind == "qualification":
        records = _evidence(evidence, rule.get("evidence_type", "qualification_record"), as_of)
        if not records:
            return "unverifiable", "缺少在 as_of 时点有效且已核验的资质证据"
        acceptable = rule.get("acceptable", [{"category": rule.get("qualification_type"), "level": rule.get("level")}])
        for wanted in acceptable:
            for record in records:
                if record.get("category") == wanted.get("category") and _level(record.get("level")) >= _level(wanted.get("level")):
                    return "satisfied", f"{record.get('category')} {record.get('level')} 满足 {wanted.get('level')}"
        return "not_satisfied", "存在有效资质，但类别或等级不满足"
    if kind == "financial":
        years = set(rule.get("years", []))
        audits = [e for e in _evidence(evidence, "financial_report", as_of) if e.get("year") in years]
        banks = [e for e in _evidence(evidence, "bank_credit", as_of) if e.get("year") in years or not years]
        if audits or banks:
            return "satisfied", "审计报告/银行资信证明满足年度要求"
        return "unverifiable", f"缺少 {sorted(years)} 年度财务审计报告或银行资信证明"
    if kind == "credit":
        reports = _evidence(evidence, "credit_check_report", as_of)
        required = set(rule.get("checks", []))
        if not reports:
            return "unverifiable", "缺少时点有效的信用三查证据"
        if not required.issubset(set(reports[0].get("checks", []))):
            return "unverifiable", "信用三查证据不完整"
        if reports[0].get("negative"):
            return "not_satisfied", "信用核查存在失信记录"
        return "satisfied", "信用三查证据完整且无失信记录"
    if kind == "consortium":
        declared = evidence.get("consortium_declaration", [])
        if rule.get("allowed") is False and any(d.get("declares") == "独立投标，不组成联合体" for d in declared):
            return "satisfied", "已声明独立投标"
        return "unverifiable", "缺少联合体/独立投标声明"
    if kind == "project_manager":
        managers = _evidence(evidence, "manager_profile", as_of)
        if not managers:
            return "unverifiable", "缺少时点有效且已核验的项目经理证据"
        for manager in managers:
            specialty = set(manager.get("specialty", []))
            if not specialty.intersection(rule.get("specialty", [])):
                continue
            if _level(manager.get("cert_level")) < _level(rule.get("cert_level")):
                continue
            if rule.get("require_b_cert") and not manager.get("b_cert"):
                continue
            if rule.get("require_no_active_project", True) and manager.get("active_projects"):
                continue
            if manager.get("status") == "active" and manager.get("availability") == "available":
                return "satisfied", f"项目经理 {manager.get('manager_id')} 专业、等级、B证、在建状态均满足"
        return "unverifiable", "已有经理记录但没有一名同时满足专业、等级、B证和无在建条件"
    if kind == "bid_bond":
        receipts = _evidence(evidence, "bid_bond", as_of)
        if not receipts:
            return "unverifiable", "缺少保证金到账凭证"
        for receipt in receipts:
            if receipt.get("amount") == rule.get("amount") and receipt.get("form") in rule.get("forms", []):
                return "satisfied", "保证金金额、形式和到账凭证满足要求"
        return "not_satisfied", "保证金金额或形式不符合要求"
    if kind == "response":
        if _evidence(evidence, rule.get("evidence_type", "response_document"), as_of):
            return "satisfied", "响应性文件证据已核验"
        return "unverifiable", "缺少已核验的响应性文件证据"
    return "manual_review", f"规则类型 {kind!r} 尚未实现"


def _match_scored(item: dict[str, Any], evidence: dict[str, list[dict[str, Any]]], as_of: str) -> tuple[str, str, int | None, bool]:
    if item.get("score_nature") == "subjective":
        review = item.get("internal_review") or {}
        if review.get("status") == "passed":
            return "satisfied", "内部质量评审已通过（不代表评标委员会得分）", item.get("max_score"), True
        return "manual_review", "待内部质量评审，不能计入内部满分", 0, False
    if item.get("requires_quote"):
        quotes = [q for q in evidence.get("bid_price_input", []) if q.get("type") == "quoted_price" and q.get("entered_by")]
        if not quotes:
            return "unverifiable", "报价未由人员录入，系统不生成或推荐报价", None, False
    for kind in item.get("evidence_required", []):
        if not _evidence(evidence, kind, as_of):
            return "unverifiable", f"缺少时点有效的计分证据: {kind}", None, False
    score = item.get("max_score") if item.get("score_formula", {}).get("kind") == "fixed_max" else None
    if score is None:
        return "manual_review", "评分公式或输入不完整，需人工复核", None, False
    return "satisfied", f"客观项可复算 {score}/{item.get('max_score')}", score, score == item.get("max_score")


def evaluate(requirements: list[dict[str, Any]], evidence: dict[str, list[dict[str, Any]]], *, as_of: str, mode: str = "gate", lot_id: str | int | None = None) -> dict[str, Any]:
    if mode not in ("gate", "diagnostic"):
        raise ValueError("mode 必须为 gate 或 diagnostic")
    # 标段隔离（F008 §4.1 lot_id）：规则与证据均按标段过滤；lot_id 为空 = 项目级通用
    if lot_id is not None:
        requirements = [r for r in requirements if r.get("lot_id") in (None, lot_id)]
        evidence = {k: [e for e in v if e.get("lot_id") in (None, lot_id)] for k, v in evidence.items()}
    # 校验针对过滤后的执行规则集（多标段规则库按 lot 过滤后 ID 唯一）
    validation = validate_requirements(requirements)
    if not validation["valid"]:
        raise ValueError("规则输入无效: " + "; ".join(validation["errors"]))
    matrix, blocked, pending, review = [], [], [], []
    for item in requirements:
        if item["req_type"] == "hard_requirement":
            result, reason = _match_hard(item["rule"], evidence, as_of)
            score = None
        elif item["req_type"] == "scored_requirement":
            result, reason, score, full = _match_scored(item, evidence, as_of)
        else:
            status = item.get("action_status", "not_started")
            result, reason, score = ("satisfied", f"动作状态={status}", None) if status in ("ready", "completed") else ("unverifiable", f"动作状态={status}，尚未就绪", None)
        entry = {"requirement_id": item["requirement_id"], "req_type": item["req_type"], "clause_ref": item["clause_ref"], "match_result": result, "match_reason": reason, "score": score}
        matrix.append(entry)
        if result == "not_satisfied": blocked.append(entry)
        elif result == "unverifiable": pending.append(entry)
        elif result == "manual_review": review.append(entry)
        if mode == "gate" and item["req_type"] == "hard_requirement" and result == "not_satisfied":
            break
    hard = [m for m in matrix if m["req_type"] == "hard_requirement"]
    scored = [m for m in matrix if m["req_type"] == "scored_requirement"]
    actions = [m for m in matrix if m["req_type"] == "action_requirement"]
    return {
        "as_of": as_of, "mode": mode, "lot_id": lot_id, "matrix": matrix,
        "coverage": {"executed": len(matrix), "declared": len(requirements), "complete": len(matrix) == len(requirements)},
        "blocked": blocked, "pending": pending, "review": review,
        "qualification_result": "failed" if any(m in blocked for m in hard) else ("pending" if any(m in pending for m in hard) else "passed"),
        "scoring_result": "not_full" if any(m in pending + review for m in scored) else "full",
        "operational_readiness": "ready" if all(m["match_result"] == "satisfied" for m in actions if m["req_type"] == "action_requirement") else "not_ready",
        "internal_admission_eligible": not blocked and not pending and not review and len(matrix) == len(requirements),
    }
