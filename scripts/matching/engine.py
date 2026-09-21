"""Small, deterministic matching engine used by the lab and regression tests.

The engine deliberately treats missing or temporally invalid evidence as
``unverifiable``.  It does not infer legal qualification or generate a bid.
"""
from __future__ import annotations

from datetime import date
from typing import Any


RESULTS = {"satisfied", "not_satisfied", "unverifiable", "manual_review"}
# 动作项状态 → 中文（判定原因面向投标专员，不出现英文枚举）
ACTION_STATUS_LABEL = {"not_started": "尚未开始", "ready": "就绪", "completed": "完成",
                       "overdue": "已逾期", "not_applicable": "不适用"}


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


UNSTRUCTURED_RULE_NOTE = "规则仅定位到条款原文、未结构化出可比对参数"


def _match_hard(rule: dict[str, Any], evidence: dict[str, list[dict[str, Any]]], as_of: str) -> tuple[str, str]:
    kind = rule.get("type")
    if kind == "qualification":
        evidence_type = rule.get("evidence_type") or (
            "safety_license" if rule.get("anchor_key") == "safety_license" else "qualification_record")
        records = _evidence(evidence, evidence_type, as_of)
        if evidence_type == "safety_license":
            # 安全生产许可证不分等级：存在时点有效且已核验的许可证即满足，不与资质等级比对
            if records:
                rec = records[0]
                return "satisfied", f"安全生产许可证有效（{rec.get('material_id') or rec.get('category')}，有效期至 {rec.get('valid_until') or '未填'}）"
            return "unverifiable", "缺少在 as_of 时点有效且已核验的安全生产许可证"
        if not records:
            return "unverifiable", "缺少在 as_of 时点有效且已核验的资质证据"
        acceptable = rule.get("acceptable") or [{"category": rule.get("qualification_type"), "level": rule.get("level")}]
        comparable = [w for w in acceptable if w.get("category") or w.get("level")]
        if not comparable:
            return "manual_review", f"{UNSTRUCTURED_RULE_NOTE}（资质类别/等级），现有 {len(records)} 条有效资质需人工核对"
        for wanted in comparable:
            for record in records:
                if record.get("category") == wanted.get("category") and _level(record.get("level")) >= _level(wanted.get("level")):
                    return "satisfied", f"{record.get('category')} {record.get('level')} 满足 {wanted.get('level')}"
        have = "、".join(f"{r.get('category')} {r.get('level') or ''}".strip() for r in records[:3])
        want = "、".join(f"{w.get('category') or '?'} {w.get('level') or ''}".strip() for w in comparable)
        return "not_satisfied", f"存在有效资质（{have}），但类别或等级不满足要求（{want}）"
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
        # 解析器输出极性字段 accepts_consortium（F021），旧规则用 allowed；二者同义
        allowed = rule.get("allowed", rule.get("accepts_consortium"))
        if allowed is True:
            return "satisfied", "招标文件接受联合体投标，独立投标或联合体均可，无额外阻断"
        if allowed is None:
            return "manual_review", "联合体条款极性未判定（接受/不接受），需人工核对原文"
        if any(d.get("declares") == "独立投标，不组成联合体" for d in declared):
            return "satisfied", "不接受联合体，已声明独立投标"
        return "unverifiable", "不接受联合体，缺少独立投标声明"
    if kind == "project_manager":
        if not (rule.get("specialty") or rule.get("cert_level") or rule.get("require_b_cert")
                or "require_no_active_project" in rule):
            return "manual_review", f"{UNSTRUCTURED_RULE_NOTE}（专业/等级/B证/在建约束），需人工核对拟派项目经理"
        managers = _evidence(evidence, "manager_profile", as_of)
        if not managers:
            return "unverifiable", "缺少时点有效且已核验的项目经理证据"
        for manager in managers:
            specialty = set(manager.get("specialty", []))
            if rule.get("specialty") and not specialty.intersection(rule.get("specialty", [])):
                continue
            if rule.get("cert_level") and _level(manager.get("cert_level")) < _level(rule.get("cert_level")):
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
        if rule.get("amount") is None:
            return "manual_review", f"{UNSTRUCTURED_RULE_NOTE}（保证金金额/形式），已有凭证需人工核对"
        for receipt in receipts:
            if receipt.get("amount") == rule.get("amount") and receipt.get("form") in rule.get("forms", []):
                return "satisfied", "保证金金额、形式和到账凭证满足要求"
        return "not_satisfied", "保证金金额或形式不符合要求"
    if kind == "response":
        if _evidence(evidence, rule.get("evidence_type", "response_document"), as_of):
            return "satisfied", "响应性文件证据已核验"
        return "unverifiable", "缺少已核验的响应性文件证据"
    if kind == "safety_officer":
        certs = _evidence(evidence, "safety_officer_cert", as_of)
        valid = [c for c in certs if not rule.get("require_c_cert") or c.get("cert_type") == "C"]
        if rule.get("count") is None:
            # 人数要求未结构化：不能默认「1 人」判满足（原文可能要求 3 人/5 人）
            if not valid:
                return "unverifiable", "缺少时点有效且已核验的专职安全员 C 证证据"
            return "manual_review", f"{UNSTRUCTURED_RULE_NOTE}（配备人数），现有 {len(valid)} 名有效 C 证专职安全员，需人工核对原文人数要求"
        if len(valid) >= rule.get("count"):
            return "satisfied", f"专职安全生产管理人员 {len(valid)} 人（≥{rule.get('count')}）且C证有效"
        return "unverifiable", f"安全员C证有效数量 {len(valid)} 不足 {rule.get('count')} 或证据缺失"
    if kind == "technical_team":
        wanted = set(rule.get("specialties", []))
        if not wanted:
            return "manual_review", f"{UNSTRUCTURED_RULE_NOTE}（技术团队专业要求），需人工核对"
        members = _evidence(evidence, "technical_team_member", as_of)
        covered = {m.get("specialty") for m in members if m.get("specialty")}
        if wanted.issubset(covered):
            return "satisfied", f"技术团队覆盖专业 {sorted(covered)}（要求 {sorted(wanted)}）"
        return "unverifiable", f"技术团队专业覆盖 {sorted(covered)}，缺 {sorted(wanted - covered)}"
    if kind == "quote_cap":
        quotes = [q for q in _evidence(evidence, rule.get("evidence_type", "bid_price_input"), as_of) if q.get("type") == "quoted_price"]
        if not quotes:
            return "unverifiable", "缺少人员录入的报价（系统不生成报价）"
        amount = quotes[0].get("amount")
        if amount is None:
            return "unverifiable", "报价金额字段缺失"
        if rule.get("max_amount") is None:
            return "manual_review", f"{UNSTRUCTURED_RULE_NOTE}（最高投标限价金额），需人工核对报价 {amount}"
        if amount > rule.get("max_amount"):
            return "not_satisfied", f"报价 {amount} 超过最高投标限价 {rule.get('max_amount')}"
        return "satisfied", f"报价 {amount} 未超最高投标限价 {rule.get('max_amount')}"
    if kind == "social_security":
        proofs = _evidence(evidence, "social_security_proof", as_of)
        if not proofs:
            return "unverifiable", "缺少社保缴纳证明"
        for p in proofs:
            if p.get("subject") != rule.get("subject", "project_manager"):
                continue
            if p.get("continuous_months", 0) >= rule.get("continuous_months", 1):
                return "satisfied", f"社保连续缴纳 {p.get('continuous_months')} 个月满足要求"
        return "unverifiable", "社保证明未满足连续月数或主体要求"
    if kind == "similar_performance":
        # 资格审查口径的业绩硬性要求（如「自 X 年以来完成过一项 N 万元及以上…业绩」）与评分项共用判定
        return _match_similar_performance(rule, evidence, as_of)
    return "manual_review", f"规则类型 {kind!r} 尚未实现自动判定，需人工按条款原文核对"


def _match_similar_performance(rule: dict[str, Any], evidence: dict[str, list[dict[str, Any]]], as_of: str) -> tuple[str, str]:
    """类似业绩客观项（技术标明标）：2022-09-01 至开标日单体建筑面积≥min_area 的同类业绩。

    判定口径（农大招标文件 第三章四(5) 备注 c）：
    - 以竣工验收报告中明确的竣工时间或合同中的计划竣工日期为准（completed_at / planned_end）；
    - 投标人业绩与项目经理业绩不可通用（subject 区分 bidder / project_manager）；
    - 证据存在但无一满足 → unverifiable（不推断满足，不冒充评标得分）；
    - 规则未结构化（无起算期/面积/金额/类型任一约束）→ manual_review：任何一条业绩都能"满足"空约束，属虚假通过。
    """
    if not any(rule.get(k) is not None for k in ("since", "min_area", "min_amount", "project_type", "date_after")):
        return "manual_review", f"{UNSTRUCTURED_RULE_NOTE}（业绩起算期/规模/类型），需人工核对企业业绩"
    records = _evidence(evidence, rule.get("evidence_type", "similar_performance"), as_of)
    if not records:
        return "unverifiable", "缺少时点有效且已核验的类似业绩证据"
    subject = rule.get("subject", "bidder")
    since = _day(rule.get("since") or rule.get("date_after"))
    point = _day(as_of)
    min_area = rule.get("min_area", 0)
    min_amount = rule.get("min_amount")
    wanted_type = rule.get("project_type")
    for rec in records:
        if rec.get("subject") != subject:
            continue
        done = _day(rec.get("completed_at") or rec.get("planned_end"))
        if done is None or (since and done < since) or (point and done > point):
            continue
        if rec.get("area") is not None and rec.get("area") < min_area:
            continue
        if min_amount is not None:
            amount = rec.get("contract_amount")
            if amount is None or float(amount) < float(min_amount):
                continue
        if wanted_type and rec.get("project_type") != wanted_type:
            continue
        return "satisfied", f"{subject} 类似业绩满足（{rec.get('project_name', '')} 面积 {rec.get('area')}㎡，竣工 {done}）"
    return "unverifiable", f"已有类似业绩记录 {len(records)} 条但无一满足条件（主体/面积/金额/竣工窗口/类型）"


def _match_scored(item: dict[str, Any], evidence: dict[str, list[dict[str, Any]]], as_of: str) -> tuple[str, str, int | None, bool]:
    if item.get("score_nature") == "subjective":
        review = item.get("internal_review") or {}
        if review.get("status") == "passed":
            return "satisfied", "内部质量评审已通过（不代表评标委员会得分）", item.get("max_score"), True
        # 未通过评审的主观项得分不可计算：score 必须为 None，不得写 0 冒充已计分（ADR-004 §2.5）
        return "manual_review", "待内部质量评审，得分暂不可计算，不能计入内部满分", None, False
    rule = item.get("rule") or {}
    if rule.get("type") == "similar_performance":
        result, reason = _match_similar_performance(rule, evidence, as_of)
        if result == "satisfied":
            return "satisfied", reason, item.get("max_score"), True
        return result, reason, None, False
    if item.get("requires_quote"):
        quotes = [q for q in evidence.get("bid_price_input", []) if q.get("type") == "quoted_price" and q.get("entered_by")]
        if not quotes:
            return "unverifiable", "报价未由人员录入，系统不生成或推荐报价", None, False
    present: list[str] = []
    for kind in item.get("evidence_required", []):
        if not _evidence(evidence, kind, as_of):
            return "unverifiable", f"缺少时点有效且已核验的计分证据（{kind}），不计分", None, False
        present.append(kind)
    score = item.get("max_score") if (item.get("score_formula") or {}).get("kind") == "fixed_max" else None
    if score is None:
        have = f"，已有证据 {present}" if present else ""
        return "manual_review", f"评分项无可复算的评分公式（score_formula 未结构化）{have}；得分需人工按评标办法核对，不计入内部满分", None, False
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
            status = item.get("action_status") or "not_started"
            label = ACTION_STATUS_LABEL.get(status, status)
            result, reason, score = (
                ("satisfied", f"投标动作已{label}（由人员登记）", None) if status in ("ready", "completed")
                else ("unverifiable", f"投标动作{label}（报名/递交/开标等由人员完成后登记，系统不代办）", None))
        entry = {"requirement_id": item["requirement_id"], "req_type": item["req_type"], "clause_ref": item["clause_ref"],
                 "match_result": result, "match_reason": reason, "score": score, "max_score": item.get("max_score")}
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
