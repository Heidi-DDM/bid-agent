"""Small, deterministic matching engine used by the lab and regression tests.

The engine deliberately treats missing or temporally invalid evidence as
``unverifiable``.  It does not infer legal qualification or generate a bid.

ADR-008 / docs/12 v2（2026-09-28）在既有四值结论之上增加结构化事实层：
每条 matrix 条目携带 ``reason_code``（单一字典 runtime.core.domain_dict）、
``required_value`` / ``observed_value``、``evidence_state``、人员绑定
（``person_id`` / ``candidate_plan_ref``）、评分分类 ``score_classification``。
历史枚举 satisfied/not_satisfied/unverifiable/manual_review 保持兼容；
unverifiable 的细分（blocked_missing_data 等）由投影层按 reason_code 映射。
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    # 独立脚本方式运行（golden lab / CI）时仓库根不在 sys.path，补引导；
    # 被 runtime 以 scripts.matching.engine 导入时本分支不生效。
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


RESULTS = {"satisfied", "not_satisfied", "unverifiable", "manual_review",
           "not_calculable", "not_applicable", "not_evaluated"}
# 动作项状态 → 中文（判定原因面向投标专员，不出现英文枚举）
ACTION_STATUS_LABEL = {"not_started": "尚未开始", "ready": "就绪", "completed": "完成",
                       "overdue": "已逾期", "not_applicable": "不适用"}

# ---- 单一字典（docs/12 §11-2：枚举/原因码/岗位/阶段/评分分类有唯一版本源） ----
from runtime.core.domain_dict import (  # noqa: E402
    RC_AUTHORIZED_INPUT_MISSING,
    RC_CANDIDATE_PLAN_UNSELECTED,
    RC_CERTIFICATE_EXPIRED,
    RC_ENTERPRISE_RECORD_MISSING,
    RC_EVIDENCE_MISSING,
    RC_EVIDENCE_PENDING_VERIFICATION,
    RC_FORMULA_MISSING,
    RC_GRADE_BELOW_REQUIREMENT,
    RC_NOT_APPLICABLE,
    RC_ONSITE_CONFLICT,
    RC_PROFESSIONAL_JUDGEMENT_REQUIRED,
    RC_PROFESSION_MISMATCH,
    RC_RULE_UNSTRUCTURED,
    RC_SCORING_EVIDENCE_MISSING,
    RC_VALIDITY_DATE_MISSING,
    RC_VERIFIED_QUANTITY_INSUFFICIENT,
    ROLE_CODE_LABEL,
    RULE_TYPE_ROLE_CODE,
    SC_METHODOLOGY_ONLY,
    SC_OBJECTIVE_CALCULABLE,
    SC_PRICE_FORMULA,
    SC_SUBJECTIVE_REVIEW,
    SC_UNSTRUCTURED,
    SINGLE_PERSON_RULE_TYPES,
)

MATCHER_VERSION = "engine-adr008-v2.1"  # 2026-09-29：五项业务裁定（安全员三段式/失信自查/联合体/保证金执行化/有效期限价信息项）

UNSTRUCTURED_RULE_NOTE = "规则仅定位到条款原文、未结构化出可比对参数"


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
    """Require status, verification and validity at the evaluation time.

    2026-09-23：核验行为时间不再要求早于判定时点（与 matching._snapshot_eligible
    同步修正）——核验是对既有事实的事后确认；有效性仍按 valid_from/valid_until
    对 as_of 严格判定，未核验一律无效。
    """
    point = _day(as_of)
    if not point or item.get("status") not in ("active", "valid") or not item.get("verified_at"):
        return False
    valid_from = _day(item.get("valid_from") or item.get("issued_at"))
    valid_until = _day(item.get("valid_until") or item.get("cert_valid_until"))
    if valid_from and valid_from > point:
        return False
    return not valid_until or valid_until >= point


def _evidence(evidence: dict[str, list[dict[str, Any]]], kind: str, as_of: str) -> list[dict[str, Any]]:
    return [item for item in evidence.get(kind, []) if evidence_is_valid(item, as_of)]


# 等级归一（施工序列：三级<二级<一级<特级；设计序列：丙级<乙级<甲级<综合甲级）。
# 2026-09-23 v1.6：补设计序列与壹贰叁大写归一——此前「甲级」不在映射里（-1），
# 设计类资质比较必然失败（借鉴 ER 工具的 canonicalization：比较前先统一形态）。
_LEVEL_MAP = {
    "三级": 1, "二级": 2, "一级": 3, "特级": 4,
    "丙级": 1, "乙级": 2, "甲级": 3, "综合甲级": 4,
}


def _level(value: Any) -> int:
    normalized = str(value or "").replace("壹", "一").replace("贰", "二").replace("叁", "三")
    return _LEVEL_MAP.get(normalized, -1)


def _category_matches(wanted: Any, record: Any) -> bool:
    """资质类别匹配：精确相等或互为包含（台账类别常含行业前缀，如「工程设计建筑行业（建筑工程）」）。"""
    w, r = str(wanted or ""), str(record or "")
    if not w or not r:
        return False
    return w == r or w in r or r in w


def _specialty_tokens(value: Any) -> set[str]:
    """专业归一 token 集：「市政公用工程/公路」→{市政,公路}（借鉴 Splink blocking key 思路：
    比较双方先各自规范化为可比对的键，再求交集）。"""
    text = str(value or "")
    out: set[str] = set()
    for part in re.split(r"[/、,，\s]+", text):
        part = part.strip()
        if not part:
            continue
        part = part.replace("公用工程", "").replace("工程", "")
        if part:
            out.add(part)
    return out


def _type_matches(wanted: Any, record: Any) -> bool:
    """业绩工程类型匹配：token 交集（「市政工程」vs「市政基础工程」→{市政}∩{市政基础}；
    取主干词（去「基础/工程」后缀）后包含判定，避免字面不等即判不满足。"""
    w, r = str(wanted or ""), str(record or "")
    if not w or not r:
        return False
    if w == r or w in r or r in w:
        return True
    w_core = w.replace("基础工程", "").replace("工程", "")
    r_core = r.replace("基础工程", "").replace("工程", "")
    return bool(w_core and r_core and (w_core in r_core or r_core in w_core))


def _refs_of(record: dict[str, Any] | None) -> list[str]:
    """满足项的企业证据回链（ADR-004 §2.5）：结构化记录自带的 evidence_refs。"""
    if not record:
        return []
    return [str(r) for r in (record.get("evidence_refs") or []) if r]


def _person_key(record: dict[str, Any]) -> str:
    return str(record.get("manager_id") or record.get("personnel_id")
               or record.get("material_id") or record.get("display_name") or "")


# ---------------------------------------------------------------------------
# 事实层（docs/12 §3.2）：matcher 统一返回 (result, reason, refs, facts)
# facts 携带 reason_code / required_value / observed_value / evidence_state /
# person 绑定等结构化字段；旧三元组调用方（若有）不受影响——本模块内部消费。
# ---------------------------------------------------------------------------

def _f(reason_code: str | None, **kw: Any) -> dict[str, Any]:
    facts: dict[str, Any] = {"reason_code": reason_code}
    facts.update(kw)
    return facts


def _no_evidence_facts(kind: str, ledger_counts: dict[str, int] | None,
                       ineligible: dict[str, list[dict]] | None, *,
                       missing_note: str = "缺少时点有效且已核验的证据") -> tuple[str, str, list, dict]:
    """无可核验证据时的状态拆分（docs/12 §3.2 / 8.1-1）：

    - 台账中该类型记录为 0 → enterprise_record_missing（真缺资料，待补）；
    - 记录存在但未入快照 → 按 ineligible 原因（过期 → 事实性 certificate_expired；
      未核验 → evidence_pending_verification；生效日晚于时点 → evidence_missing）；
    - 无 ineligible 明细（旧调用）但有台账记录 → evidence_pending_verification。
    """
    counts = ledger_counts or {}
    bad = (ineligible or {}).get(kind) or []
    ledger_n = counts.get(kind)
    if bad:
        reasons = {b.get("reason") for b in bad}
        if "expired" in reasons:
            sample = next(b for b in bad if b.get("reason") == "expired")
            until = sample.get("valid_until") or "未填"
            return (
                "not_satisfied",
                f"存在该类记录但有效期至 {until}，在判定时点已过期（证书过期是事实，不是缺资料）",
                [],
                _f(RC_CERTIFICATE_EXPIRED, evidence_state="sufficient_but_insufficient",
                   observed_value={"expired_count": len([b for b in bad if b.get("reason") == "expired"])}),
            )
        if "unverified" in reasons:
            return (
                "unverifiable", f"{missing_note}（台账有记录但尚未核验，核验后重算，不推断满足）", [],
                _f(RC_EVIDENCE_PENDING_VERIFICATION, evidence_state="pending_verification",
                   observed_value={"ledger_count": len(bad)}),
            )
        return (
            "unverifiable", f"{missing_note}（记录生效日/有效期不覆盖判定时点）", [],
            _f(RC_EVIDENCE_MISSING, evidence_state="missing", observed_value={"ledger_count": len(bad)}),
        )
    if ledger_n == 0 or (ledger_counts is not None and not ledger_n):
        # 台账口径明确该类记录为 0（键缺失也计 0）→ 真缺资料（docs/12 8.1-1）
        return (
            "unverifiable", f"{missing_note}（企业台账中尚无此类记录，待补录并核验）", [],
            _f(RC_ENTERPRISE_RECORD_MISSING, evidence_state="missing", observed_value={"ledger_count": 0}),
        )
    if ledger_n:
        return (
            "unverifiable", f"{missing_note}（台账有 {ledger_n} 条记录，均不满足时点/核验要求，待核验后重算）", [],
            _f(RC_EVIDENCE_PENDING_VERIFICATION, evidence_state="pending_verification",
               observed_value={"ledger_count": ledger_n}),
        )
    return (
        "unverifiable", missing_note, [],
        _f(RC_EVIDENCE_MISSING, evidence_state="missing"),
    )


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


def _match_hard(rule: dict[str, Any], evidence: dict[str, list[dict[str, Any]]], as_of: str,
                ledger_counts: dict[str, int] | None = None,
                ineligible: dict[str, list[dict]] | None = None,
                ) -> tuple[str, str, list[str], dict[str, Any]]:
    """硬性要求判定 → (结果, 原因, 企业证据回链, 事实层)。

    回链来自满足判定的结构化记录自身 evidence_refs（ADR-004 §2.5；RAG 检索回链由
    worker 侧合并）。人员类规则（project_manager 等）在 evaluate 的候选班子阶段
    统一判定，不经本函数。
    """
    kind = rule.get("type")
    if kind == "submission_package":
        return ("not_applicable", "该条款是投标文件附件资料包要求，不是独立人员资格条件；在递交阶段按附件形成资料包",
                [], _f(RC_NOT_APPLICABLE, evidence_state="not_applicable",
                       task_kind="preparation", stage="submission_ready"))
    if kind == "qualification":
        evidence_type = rule.get("evidence_type") or (
            "safety_license" if rule.get("anchor_key") == "safety_license" else "qualification_record")
        records = _evidence(evidence, evidence_type, as_of)
        if evidence_type == "safety_license":
            # 安全生产许可证不分等级：存在时点有效且已核验的许可证即满足，不与资质等级比对
            if records:
                rec = records[0]
                return ("satisfied",
                        f"安全生产许可证有效（{rec.get('material_id') or rec.get('category')}，有效期至 {rec.get('valid_until') or '未填'}）",
                        _refs_of(rec), _f(None, evidence_state="sufficient"))
            result, reason, refs, facts = _no_evidence_facts(
                evidence_type, ledger_counts, ineligible, missing_note="缺少在 as_of 时点有效且已核验的安全生产许可证")
            return result, reason, refs, facts
        if not records:
            return _no_evidence_facts(evidence_type, ledger_counts, ineligible,
                                      missing_note="缺少在 as_of 时点有效且已核验的资质证据")
        acceptable = rule.get("acceptable") or [{"category": rule.get("qualification_type"), "level": rule.get("level")}]
        comparable = [w for w in acceptable if w.get("category") or w.get("level")]
        if not comparable:
            return "manual_review", f"{UNSTRUCTURED_RULE_NOTE}（资质类别/等级），现有 {len(records)} 条有效资质需人工核对", [], \
                _f(RC_RULE_UNSTRUCTURED, evidence_state="sufficient",
                   observed_value={"valid_records": len(records)})
        for wanted in comparable:
            for record in records:
                cat_ok = _category_matches(wanted.get("category"), record.get("category")) or not wanted.get("category")
                if cat_ok and _level(record.get("level")) >= _level(wanted.get("level")):
                    return ("satisfied",
                            f"{record.get('category')} {record.get('level')} 满足 {wanted.get('category') or ''}{wanted.get('level') or ''} 要求",
                            _refs_of(record),
                            _f(None, evidence_state="sufficient",
                               observed_value={"category": record.get("category"), "level": record.get("level")},
                               required_value={"acceptable": comparable}))
        have = "、".join(f"{r.get('category')} {r.get('level') or ''}".strip() for r in records[:3])
        want = "、".join(f"{w.get('category') or '?'} {w.get('level') or ''}".strip() for w in comparable)
        # 状态拆分（docs/12 §3.2）：证据充分但不满足 → 按缺口类型给独立原因码
        level_short = all(
            (_category_matches(w.get("category"), r.get("category")) or not w.get("category"))
            and _level(r.get("level")) < _level(w.get("level"))
            for w in comparable for r in records
        ) and any(w.get("level") for w in comparable)
        reason_code = RC_GRADE_BELOW_REQUIREMENT if level_short else RC_PROFESSION_MISMATCH
        return ("not_satisfied", f"存在有效资质（{have}），但类别或等级不满足要求（{want}）", [],
                _f(reason_code, evidence_state="sufficient_but_insufficient",
                   observed_value={"held": [{"category": r.get("category"), "level": r.get("level")} for r in records[:5]]},
                   required_value={"acceptable": comparable}))
    if kind == "business_license":
        # v1.6：营业执照独立判定——资料库存在已核验的营业执照记录即满足；
        # 无记录 → unverifiable（资料缺证，不是公司没有执照，不推断满足）
        records = _evidence(evidence, "business_license", as_of)
        if records:
            return "satisfied", f"营业执照已核验且时点有效（{records[0].get('material_id') or '资料库记录'}）", \
                _refs_of(records[0]), _f(None, evidence_state="sufficient")
        result, reason, refs, facts = _no_evidence_facts(
            "business_license", ledger_counts, ineligible, missing_note="企业资料库中无已核验的营业执照记录（待补录营业执照及核验，不推断满足）")
        return result, reason, refs, facts
    if kind == "bid_validity":
        days = rule.get("days")
        if days is None:
            return "manual_review", f"{UNSTRUCTURED_RULE_NOTE}（投标有效期天数），需人工按原文核对", [], \
                _f(RC_RULE_UNSTRUCTURED)
        # 2026-09-29 用户裁定：投标有效期是投标文件的响应性内容（编制时响应），
        # 资格审查资料清单无对应材料项——判定阶段为信息项，不作为缺证阻断。
        if _evidence(evidence, "response_document", as_of):
            return "satisfied", f"响应性文件已核验（投标有效期 {days} 日历天已响应）", [], \
                _f(None, evidence_state="sufficient", required_value={"days": days})
        return ("not_applicable",
                f"投标有效期 {days} 日历天：投标文件编制时按此响应（资格阶段无对应提交材料），判定阶段仅作信息记录",
                [],
                _f(RC_NOT_APPLICABLE, evidence_state="not_applicable", required_value={"days": days},
                   task_kind="preparation", stage="submission_ready"))
    if kind == "ceiling_price":
        # v1.6：最高投标限价=报价上限约束。系统不生成报价；报价由人员录入后比对，
        # 未录入前 unverifiable（与 quote_cap 同口径）
        quotes = [q for q in _evidence(evidence, "bid_price_input", as_of) if q.get("type") == "quoted_price"]
        max_amount = rule.get("max_amount")
        if max_amount is None:
            return "manual_review", f"{UNSTRUCTURED_RULE_NOTE}（最高投标限价金额），需人工核对", [], \
                _f(RC_RULE_UNSTRUCTURED)
        if not quotes:
            return ("not_applicable",
                    f"最高投标限价 {_fmt_money(max_amount)} 元（报价上限信息）：报价由授权人员在投标时录入后比对，"
                    "系统不生成或推荐报价；判定阶段仅作信息记录",
                    [],
                    _f(RC_NOT_APPLICABLE, evidence_state="not_applicable", required_value={"max_amount": max_amount},
                       task_kind="preparation", stage="submission_ready"))
        amount = quotes[0].get("amount")
        if amount is not None and float(amount) > float(max_amount):
            return "not_satisfied", f"报价 {amount} 超过最高投标限价 {max_amount}", [], \
                _f(RC_GRADE_BELOW_REQUIREMENT, evidence_state="sufficient_but_insufficient",
                   observed_value={"amount": amount}, required_value={"max_amount": max_amount})
        return "satisfied", f"报价 {amount} 未超最高投标限价 {max_amount}", _refs_of(quotes[0]), \
            _f(None, evidence_state="sufficient", observed_value={"amount": amount},
               required_value={"max_amount": max_amount})
    if kind == "financial":
        years = set(rule.get("years", []))
        audits = [e for e in _evidence(evidence, "financial_report", as_of) if e.get("year") in years]
        banks = [e for e in _evidence(evidence, "bank_credit", as_of) if e.get("year") in years or not years]
        if audits or banks:
            rec = (audits or banks)[0]
            return "satisfied", "审计报告/银行资信证明满足年度要求", _refs_of(rec), \
                _f(None, evidence_state="sufficient", required_value={"years": sorted(years)})
        want_years = f"{sorted(years)} 年度" if years else "（年度要求未结构化）"
        result, reason, refs, facts = _no_evidence_facts(
            "financial_report", ledger_counts, ineligible,
            missing_note=f"缺少 {want_years}财务审计报告或银行资信证明")
        if result == "not_satisfied":
            # 财务报告只有过期语义时不构成「明确不满足」——保守回退待补（不编造失败）
            result, reason = "unverifiable", f"缺少 {want_years}财务审计报告或银行资信证明（如有过期记录请核验后重算）"
            facts = dict(facts, reason_code=RC_EVIDENCE_MISSING, evidence_state="missing")
        return result, reason, refs, facts
    if kind == "credit":
        # 2026-09-29 用户裁定：「未被列入失信被执行人名单」是状态要求（原文以官方平台信息为准），
        # 招标文件资格审查资料清单未要求提交信用证明材料——投标决策阶段作为「自查确认事项」，
        # 不作为缺证阻断；有已核验信用记录时如实呈现（含失信事实）。
        reports = _evidence(evidence, "credit_check_report", as_of)
        required = set(rule.get("checks", []))
        self_check_note = ("状态要求：以“信用中国/全国法院失信平台”信息为准；资格审查资料清单未要求提交证明材料——"
                           "投标决策阶段由企业自查确认（投标人及拟派项目经理在要求期间未被列入失信名单），"
                           "不作为缺证阻断")
        if reports:
            if reports[0].get("negative"):
                return "not_satisfied", "已核验信用记录存在失信记录（事实，需处置）", _refs_of(reports[0]), \
                    _f(RC_PROFESSION_MISMATCH, evidence_state="sufficient_but_insufficient",
                       observed_value={"negative": reports[0].get("negative")})
            return "satisfied", f"已有核验通过的信用记录且无失信记录。{self_check_note}", _refs_of(reports[0]), \
                _f(None, evidence_state="sufficient")
        return ("not_applicable", self_check_note, [],
                _f(RC_NOT_APPLICABLE, evidence_state="not_applicable",
                   next_action="assign_reviewer"))
    if kind == "consortium":
        declared = evidence.get("consortium_declaration", [])
        # 解析器输出极性字段 accepts_consortium（F021），旧规则用 allowed；二者同义
        allowed = rule.get("allowed", rule.get("accepts_consortium"))
        if allowed is True:
            return "satisfied", "招标文件接受联合体投标，独立投标或联合体均可，无额外阻断", [], \
                _f(None, evidence_state="not_applicable")
        if allowed is None:
            return "manual_review", "联合体条款极性未判定（接受/不接受），需人工核对原文", [], \
                _f(RC_RULE_UNSTRUCTURED)
        # 2026-09-29 用户裁定：「不接受联合体」是对投标行为的约束——按独立投标人投标即可，
        # 资料清单未要求提交「独立投标声明」（该要求为旧金标准遗留，本文件无此材料项）。
        if any(d.get("declares") == "独立投标，不组成联合体" for d in declared):
            return "satisfied", "不接受联合体，已声明独立投标", [], \
                _f(None, evidence_state="sufficient")
        return "satisfied", "招标文件不接受联合体投标——按独立投标人身份投标即可满足，无需提交任何声明材料", [], \
            _f(None, evidence_state="not_applicable")
    if kind == "bid_bond":
        # 2026-09-29 用户裁定：保证金在「决定投标之后、递交之前」才缴纳/出具（资料清单第 7 项属
        # 投标文件组成）——判定要不要投标的阶段不存在到账凭证，不作为缺证阻断；转入投标执行计划。
        receipts = _evidence(evidence, "bid_bond", as_of)
        amount = rule.get("amount")
        forms = rule.get("forms") or []
        note = ("投标执行事项：投标保证金" + (f" {_fmt_money(amount)} 元" if amount else "（金额见条款）")
                + (f"，方式 {'/'.join(forms)}" if forms else "")
                + "——决定投标后、递交投标文件前办理（缴纳凭证放入投标文件），判定阶段无需持有")
        if receipts and amount is not None:
            for receipt in receipts:
                if receipt.get("amount") == amount and (not forms or receipt.get("form") in forms):
                    return "satisfied", f"保证金已办理且金额/方式符合要求。{note}", _refs_of(receipt), \
                        _f(None, evidence_state="sufficient", task_kind="bond", stage="submission_ready",
                           observed_value={"amount": receipt.get("amount"), "form": receipt.get("form")},
                           required_value={"amount": amount, "forms": forms})
            return "not_satisfied", "已登记的保证金金额或方式不符合条款要求", [], \
                _f(RC_GRADE_BELOW_REQUIREMENT, evidence_state="sufficient_but_insufficient",
                   observed_value={"amounts": [r.get("amount") for r in receipts[:3]]},
                   required_value={"amount": amount, "forms": forms})
        if receipts is None or not receipts:
            return ("not_applicable", note, [],
                    _f(RC_NOT_APPLICABLE, evidence_state="not_applicable", task_kind="bond",
                       stage="submission_ready", required_value={"amount": amount, "forms": forms},
                       next_action="complete_operation_task"))
        return "manual_review", f"{UNSTRUCTURED_RULE_NOTE}（保证金金额/形式），已有凭证需人工核对", [], \
            _f(RC_RULE_UNSTRUCTURED, evidence_state="sufficient", task_kind="bond", stage="submission_ready")
    if kind == "response":
        if _evidence(evidence, rule.get("evidence_type", "response_document"), as_of):
            return "satisfied", "响应性文件证据已核验", [], _f(None, evidence_state="sufficient")
        return "unverifiable", "缺少已核验的响应性文件证据", [], \
            _f(RC_EVIDENCE_MISSING, evidence_state="missing")
    if kind == "safety_officer":
        certs = _evidence(evidence, "safety_officer_cert", as_of)
        valid = [c for c in certs if not rule.get("require_c_cert") or c.get("cert_type") == "C"]
        required_count = rule.get("count")
        if required_count is None:
            # 人数要求未结构化：不能默认「1 人」判满足（原文可能要求 3 人/5 人）
            if not valid:
                return _no_evidence_facts("safety_officer_cert", ledger_counts, ineligible,
                                          missing_note="缺少时点有效且已核验的专职安全员 C 证证据")
            return "manual_review", f"{UNSTRUCTURED_RULE_NOTE}（配备人数），现有 {len(valid)} 名有效 C 证专职安全员，需人工核对原文人数要求", [], \
                _f(RC_RULE_UNSTRUCTURED, evidence_state="sufficient",
                   observed_value={"verified_count": len(valid)})
        required_value = {"operator": ">=", "value": required_count, "unit": "person"}
        pending = len((ineligible or {}).get("safety_officer_cert") or [])
        ledger_n = (ledger_counts or {}).get("safety_officer_cert") or 0
        pool = max(len(valid) + pending, len(valid) + ledger_n)  # 台账持证人员总口径
        observed = {"value": len(valid), "pending_verification": pending,
                    "source": "verified_enterprise_snapshot"}
        if len(valid) >= required_count:
            refs = sorted({r for c in valid for r in _refs_of(c)})
            return ("satisfied",
                    f"专职安全生产管理人员 {len(valid)} 人（≥{required_count}）且C证有效",
                    refs,
                    _f(None, evidence_state="sufficient", observed_value=observed, required_value=required_value))
        # 数量判定三段式（2026-09-29 用户裁定）：判「不满足」必须先排除「资料不全」——
        # ① 台账无此类记录 → 待补资料（无法区分缺数据与真没有，不推断不满足）；
        # ② 台账有记录、已核验不足但未核验部分补上可能够 → 待核验（核验后重算即知）；
        # ③ 台账持证总人数（已核验+待核验）确实不足 → 明确不满足（事实性数量缺口）。
        if pool == 0:
            return ("unverifiable",
                    f"企业台账中尚无「专职安全生产管理人员 C 证」人员记录——无法区分「资料未导入」与「确无此类人员」，"
                    f"按待补资料处理：请提供安全员 C 证人员台账/证书，导入核验后重算（招标文件 §3.10 要求 {required_count} 人）",
                    [],
                    _f(RC_ENTERPRISE_RECORD_MISSING, evidence_state="missing",
                       observed_value={"ledger_count": 0}, required_value=required_value,
                       next_action="create_or_merge_evidence_task"))
        if len(valid) < required_count <= pool:
            return ("unverifiable",
                    f"专职安全员 C 证已核验有效 {len(valid)} 人，另有 {pending} 人待核验——核验完成前无法确认是否满足 "
                    f"{required_count} 人要求（不推断满足也不推断不满足），核验后重算",
                    [],
                    _f(RC_EVIDENCE_PENDING_VERIFICATION, evidence_state="pending_verification",
                       observed_value=observed, required_value=required_value,
                       next_action="verify_pending_evidence"))
        return ("not_satisfied",
                f"台账专职安全员 C 证人员共 {pool} 人（已核验有效 {len(valid)} 人），确实不足要求的 {required_count} 人"
                "——属人员数量事实缺口，需增配持证安全员，不能用补一份证明替代",
                sorted({r for c in valid for r in _refs_of(c)}),
                _f(RC_VERIFIED_QUANTITY_INSUFFICIENT, evidence_state="sufficient_but_insufficient",
                   observed_value=observed, required_value=required_value,
                   next_action="replace_or_add_qualified_person"))
    if kind == "technical_team" or kind == "tech_team":
        # tech_team = 抽取器历史类型名（v1 规则集与旧 golden 均用此名），与
        # technical_team 同义——不设别名则该类条款从未进入自动判定（2026-09-23）
        wanted = set(rule.get("specialties", []))
        if not wanted:
            return "manual_review", f"{UNSTRUCTURED_RULE_NOTE}（技术团队专业要求），需人工核对", [], \
                _f(RC_RULE_UNSTRUCTURED)
        members = _evidence(evidence, "technical_team_member", as_of)
        covered = {m.get("specialty") for m in members if m.get("specialty")}
        if wanted.issubset(covered):
            hit = next(m for m in members if m.get("specialty") in wanted)
            return "satisfied", f"技术团队覆盖专业 {sorted(covered)}（要求 {sorted(wanted)}）", _refs_of(hit), \
                _f(None, evidence_state="sufficient",
                   observed_value={"covered": sorted(covered)}, required_value={"specialties": sorted(wanted)})
        if not members:
            return _no_evidence_facts("technical_team_member", ledger_counts, ineligible,
                                      missing_note="缺少已核验的技术团队人员记录")
        return "unverifiable", f"技术团队专业覆盖 {sorted(covered)}，缺 {sorted(wanted - covered)}", [], \
            _f(RC_EVIDENCE_MISSING, evidence_state="sufficient_but_insufficient",
               observed_value={"covered": sorted(covered)}, required_value={"specialties": sorted(wanted)})
    if kind == "quote_cap":
        quotes = [q for q in _evidence(evidence, rule.get("evidence_type", "bid_price_input"), as_of) if q.get("type") == "quoted_price"]
        if not quotes:
            return "unverifiable", "缺少人员录入的报价（系统不生成报价）", [], \
                _f(RC_AUTHORIZED_INPUT_MISSING)
        amount = quotes[0].get("amount")
        if amount is None:
            return "unverifiable", "报价金额字段缺失", [], \
                _f(RC_VALIDITY_DATE_MISSING, evidence_state="missing")
        if rule.get("max_amount") is None:
            return "manual_review", f"{UNSTRUCTURED_RULE_NOTE}（最高投标限价金额），需人工核对报价 {amount}", [], \
                _f(RC_RULE_UNSTRUCTURED, observed_value={"amount": amount})
        if amount > rule.get("max_amount"):
            return "not_satisfied", f"报价 {amount} 超过最高投标限价 {rule.get('max_amount')}", [], \
                _f(RC_GRADE_BELOW_REQUIREMENT, evidence_state="sufficient_but_insufficient",
                   observed_value={"amount": amount}, required_value={"max_amount": rule.get("max_amount")})
        return "satisfied", f"报价 {amount} 未超最高投标限价 {rule.get('max_amount')}", _refs_of(quotes[0]), \
            _f(None, evidence_state="sufficient", observed_value={"amount": amount},
               required_value={"max_amount": rule.get("max_amount")})
    if kind == "social_security":
        proofs = _evidence(evidence, "social_security_proof", as_of)
        if not proofs:
            return _no_evidence_facts("social_security_proof", ledger_counts, ineligible,
                                      missing_note="缺少社保缴纳证明")
        for p in proofs:
            if p.get("subject") != rule.get("subject", "project_manager"):
                continue
            if p.get("continuous_months", 0) >= rule.get("continuous_months", 1):
                return "satisfied", f"社保连续缴纳 {p.get('continuous_months')} 个月满足要求", _refs_of(p), \
                    _f(None, evidence_state="sufficient",
                       observed_value={"continuous_months": p.get("continuous_months")},
                       required_value={"continuous_months": rule.get("continuous_months", 1),
                                       "subject": rule.get("subject", "project_manager")})
        return "unverifiable", "社保证明未满足连续月数或主体要求", [], \
            _f(RC_EVIDENCE_MISSING, evidence_state="sufficient_but_insufficient")
    if kind == "similar_performance":
        # 资格审查口径的业绩硬性要求（如「自 X 年以来完成过一项 N 万元及以上…业绩」）与评分项共用判定
        return _match_similar_performance(rule, evidence, as_of, ledger_counts=ledger_counts,
                                          ineligible=ineligible)
    return "manual_review", f"规则类型 {kind!r} 尚未实现自动判定，需人工按条款原文核对", [], \
        _f(RC_RULE_UNSTRUCTURED)


def _match_similar_performance(rule: dict[str, Any], evidence: dict[str, list[dict[str, Any]]], as_of: str,
                               ledger_counts: dict[str, int] | None = None,
                               ineligible: dict[str, list[dict]] | None = None,
                               ) -> tuple[str, str, list[str], dict[str, Any]]:
    """类似业绩客观项（技术标明标）：2022-09-01 至开标日单体建筑面积≥min_area 的同类业绩。

    判定口径（农大招标文件 第三章四(5) 备注 c）：
    - 以竣工验收报告中明确的竣工时间或合同中的计划竣工日期为准（completed_at / planned_end）；
    - 投标人业绩与项目经理业绩不可通用（subject 区分 bidder / project_manager）；
    - 证据存在但无一满足 → unverifiable（不推断满足，不冒充评标得分）；
    - 规则未结构化（无起算期/面积/金额/类型任一约束）→ manual_review：任何一条业绩都能"满足"空约束，属虚假通过。
    """
    evidence_type = rule.get("evidence_type", "similar_performance")
    if not any(rule.get(k) is not None for k in ("since", "min_area", "min_amount", "project_type", "date_after")):
        return "manual_review", f"{UNSTRUCTURED_RULE_NOTE}（业绩起算期/规模/类型），需人工核对企业业绩", [], \
            _f(RC_RULE_UNSTRUCTURED)
    records = _evidence(evidence, evidence_type, as_of)
    required_value = {k: rule.get(k) for k in ("since", "min_area", "min_amount", "project_type")
                      if rule.get(k) is not None}
    if not records:
        return _no_evidence_facts(evidence_type, ledger_counts, ineligible,
                                  missing_note="缺少时点有效且已核验的类似业绩证据")
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
        if wanted_type and not _type_matches(wanted_type, rec.get("project_type")):
            continue
        return ("satisfied",
                f"{subject} 类似业绩满足（{rec.get('project_name', '')}，合同额 {rec.get('contract_amount')} 元，竣工 {done}，类型 {rec.get('project_type')}）",
                _refs_of(rec),
                _f(None, evidence_state="sufficient",
                   observed_value={"project_name": rec.get("project_name"),
                                   "contract_amount": rec.get("contract_amount"), "completed_at": str(done)},
                   required_value=required_value, subject=subject))
    return "unverifiable", f"已有类似业绩记录 {len(records)} 条但无一满足条件（主体/面积/金额/竣工窗口/类型）", [], \
        _f(RC_EVIDENCE_MISSING, evidence_state="sufficient_but_insufficient",
           observed_value={"records": len(records)}, required_value=required_value, subject=subject)


# ---------------------------------------------------------------------------
# 人员候选班子（docs/12 §3.3 / ADR-008 §2.4）：同一 person_id 覆盖岗位全部条件，
# 禁止跨人拼接。确定性优先级：证据完整度 > 证书等级 > person_id；不使用模型偏好
# 或向量相似度。
# ---------------------------------------------------------------------------

def _person_evidence_completeness(person: dict[str, Any]) -> tuple:
    return (
        1 if person.get("b_cert") else 0,
        len(person.get("evidence_refs") or []),
        1 if person.get("cert_valid_until") else 0,
        _level(person.get("cert_level")),
    )


def _person_pool(rule_type: str, evidence: dict[str, list[dict[str, Any]]], as_of: str) -> list[dict[str, Any]]:
    pool = list(_evidence(evidence, "manager_profile", as_of))
    if rule_type in ("design_lead", "tech_lead"):
        pool += [p for p in _evidence(evidence, "technical_team_member", as_of)
                 if _person_key(p) not in {_person_key(m) for m in pool}]
    # 确定性排序：证据完整度降序、证书等级降序、person_id 升序（可重放，无模型偏好）
    pool.sort(key=lambda p: (_person_evidence_completeness(p), _person_key(p)), reverse=True)
    return pool


def _check_person_rule(rule: dict[str, Any], person: dict[str, Any]) -> tuple[bool, str | None, str]:
    """单条人员规则 × 单个人选 → (是否满足, 失败原因码, 失败说明)。"""
    rule_type = rule.get("type")
    if rule_type in ("project_manager", "construction_lead"):
        wanted_specialty = _specialty_tokens("/".join(rule.get("specialty") or []))
        specialty = _specialty_tokens("/".join(person.get("specialty") or []))
        if wanted_specialty and not specialty & wanted_specialty:
            return False, RC_PROFESSION_MISMATCH, "专业不符"
        if rule.get("cert_level") and _level(person.get("cert_level")) < _level(rule.get("cert_level")):
            return False, RC_GRADE_BELOW_REQUIREMENT, "证书等级低于要求"
        if rule.get("require_b_cert") and not person.get("b_cert"):
            # 台账无 B 证字段属数据缺口，不是「没有证」的事实（2026-09-22 现状：B 证待补 1181）
            return False, RC_EVIDENCE_MISSING, "B 证编号未录入台账"
        if rule.get("require_no_active_project", True) and person.get("active_projects"):
            return False, RC_ONSITE_CONFLICT, f"在施项目 {len(person['active_projects'])} 项"
        if person.get("status") == "active" and person.get("availability") == "available":
            return True, None, ""
        if person.get("status") != "active":
            return False, RC_EVIDENCE_PENDING_VERIFICATION, "人员记录非 active"
        return False, RC_ONSITE_CONFLICT, f"可调派状态为 {person.get('availability') or '未填'}"
    # design_lead / tech_lead：证书/职称关键词在经理与技术团队记录中匹配
    cert = rule.get("cert") or rule.get("title")
    hay = f"{person.get('specialty') or ''} {person.get('cert_level') or ''} {person.get('cert_type') or ''}"
    cert_core = (cert or "").replace("国家", "").replace("注册", "")
    if cert_core and cert_core in hay:
        return True, None, ""
    return False, RC_EVIDENCE_MISSING, f"未见 {cert or '要求证书'} 记录"


_FACTUAL_PERSON_FAILS = (RC_PROFESSION_MISMATCH, RC_GRADE_BELOW_REQUIREMENT, RC_ONSITE_CONFLICT)
# 聚合失败时的确定性原因码顺序（可审计，不随机）
_PERSON_FAIL_ORDER = (RC_PROFESSION_MISMATCH, RC_GRADE_BELOW_REQUIREMENT, RC_ONSITE_CONFLICT,
                      RC_EVIDENCE_MISSING, RC_EVIDENCE_PENDING_VERIFICATION)


def _fmt_money(v: Any) -> str:
    """金额可读格式：整数值不加 .0，千分位（500000.0 → 500,000；328447260.0 → 328,447,260）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    i = int(f)
    if f == i:
        return f"{i:,}"
    return f"{f:,.2f}"


def _person_condition_text(rule: dict[str, Any], item: dict[str, Any]) -> str:
    """岗位聚合条件的人类可读表述（来源=招标文件条款，2026-09-29 用户裁定：
    「全部条件」必须逐条列明出处，不得笼统表述）。"""
    if rule.get("type") in ("project_manager", "construction_lead"):
        parts = []
        if rule.get("specialty"):
            parts.append("注册专业 " + "/".join(rule["specialty"]))
        if rule.get("cert_level"):
            parts.append(f"{rule['cert_level']}建造师（注册在投标单位）")
        if rule.get("require_b_cert"):
            parts.append("持有效安全生产考核合格证书（B 证）")
        # 在施限制由 §3.8 型条款（无专业/等级/B证参数的纯在施规则）独立承载，不在其他条重复
        if not (rule.get("specialty") or rule.get("cert_level") or rule.get("require_b_cert")) \
                and rule.get("require_no_active_project", True):
            parts.append("投标截止日不在其他在建合同工程担任项目经理")
        return "；".join(parts) or "按条款要求"
    if rule.get("cert") or rule.get("title"):
        return f"持 {rule.get('cert') or rule.get('title')}"
    return "按条款要求"


def _evaluate_person_domain(
    role_rules: list[dict[str, Any]],
    rule_type: str,
    evidence: dict[str, list[dict[str, Any]]],
    as_of: str,
) -> dict[str, Any]:
    """一个岗位（可能多条规则）→ 候选班子方案与逐条结论（docs/12 §3.3）。

    - 每个人选对岗位内全部规则逐一判定，全部通过 = 完整方案；
    - 存在完整方案 → 该岗位所有规则 satisfied，回链同一 person_id 的证据；
    - 无完整方案 → 不得跨人拼接：个别人满足个别条款 → manual_review/
      candidate_plan_unselected；全部人选在同一规则上事实性失败 → not_satisfied；
      失败源于数据缺口 → unverifiable（待补证）。
    """
    role_code = RULE_TYPE_ROLE_CODE.get(rule_type, rule_type)
    pool = _person_pool(rule_type, evidence, as_of)
    plans: list[dict[str, Any]] = []
    for person in pool:
        results: dict[str, dict[str, Any]] = {}
        for req in role_rules:
            ok, fail_code, fail_text = _check_person_rule(req.get("rule") or {}, person)
            results[req["requirement_id"]] = {
                "result": "satisfied" if ok else "failed",
                "reason_code": fail_code,
                "fail_text": fail_text,
            }
        complete = all(r["result"] == "satisfied" for r in results.values())
        gaps = sorted({r["reason_code"] for r in results.values() if r["reason_code"]})
        plans.append({
            "plan_ref": f"CP-{role_code}-{_person_key(person) or len(plans)}",
            "role_code": role_code,
            "status": "complete" if complete else "incomplete",
            "primary": False,
            "members": [{
                "role_code": role_code,
                "person_id": _person_key(person),
                "person_kind": "manager" if person.get("manager_id") else "personnel",
                "is_primary": True,
                "availability_as_of": as_of,
                "evidence_refs": _refs_of(person),
            }],
            "results": results,
            "gap_reason_codes": gaps,
            "selection_basis": "deterministic:evidence_completeness",
            "_person": person,
        })
    complete_plans = [p for p in plans if p["status"] == "complete"]
    if complete_plans:
        # 完整方案内确定性择优（证据完整度/证书等级），全部完整方案均展示供人工选定
        complete_plans[0]["primary"] = True
    outcomes: dict[str, dict[str, Any]] = {}
    primary = complete_plans[0] if complete_plans else None
    # 岗位「全部条件」清单（每条含条款出处）——向用户明确聚合了哪些条款、缺什么（2026-09-29 裁定）
    person_conditions = [
        {"requirement_id": req["requirement_id"], "clause_ref": req.get("clause_ref"),
         "condition": _person_condition_text(req.get("rule") or {}, req)}
        for req in role_rules
    ]
    for req in role_rules:
        rid = req["requirement_id"]
        if primary is not None:
            person = primary["_person"]
            label = ROLE_CODE_LABEL.get(role_code, role_code)
            outcomes[rid] = {
                "result": "satisfied",
                "reason": (f"{label}由同一人满足全部条件：{person.get('display_name') or _person_key(person)}"
                           f"（专业 {person.get('specialty') or '—'}、等级 {person.get('cert_level') or '—'}、"
                           f"B证 {'有' if person.get('b_cert') else '—'}、在施 {len(person.get('active_projects') or [])} 项）"),
                "refs": _refs_of(person),
                "facts": _f(None, evidence_state="sufficient",
                            person_id=_person_key(person),
                            person_kind=primary["members"][0]["person_kind"],
                            candidate_plan_ref=primary["plan_ref"],
                            conditions=person_conditions),
            }
            continue
        # 无完整方案：逐规则聚合（禁止拼接）
        per_rule = [(p, p["results"][rid]) for p in plans]
        passes = [p for p, r in per_rule if r["result"] == "satisfied"]
        if not pool:
            outcomes[rid] = {
                "result": "unverifiable",
                "reason": f"缺少时点有效且已核验的{ROLE_CODE_LABEL.get(role_code, role_code)}人选记录（待补录并核验）",
                "refs": [],
                "facts": _f(RC_EVIDENCE_MISSING, evidence_state="missing", conditions=person_conditions),
            }
            continue
        if passes:
            # 个别人满足本条但无人满足岗位全部条件 → 候选班子待选，不拼接（docs/12 O4）
            gap_texts = "；".join(
                f"{p['_person'].get('display_name') or _person_key(p['_person'])}："
                + "、".join(f"{p['results'][x]['reason_code']}({p['results'][x]['fail_text']})"
                           for x in p["results"] if p["results"][x]["result"] == "failed")
                for p in plans[:3]
            )
            cond_texts = "；".join(f"「{c['condition']}」（{c['clause_ref'] or c['requirement_id']}）" for c in person_conditions)
            outcomes[rid] = {
                "result": "manual_review",
                "reason": (f"本条基础核验有 {len(passes)} 人通过；但当前没有同一人同时满足该岗位的全部条件，"
                           f"因此不能把不同人员拼成一个项目经理方案。岗位综合条件（均来自招标文件）：{cond_texts}。"
                           "这是岗位方案未成立的综合阻断，不等同于本条事实不满足；请选定/补足同一人方案。"
                           "候选差异：" + gap_texts),
                "refs": [],
                "facts": _f(RC_CANDIDATE_PLAN_UNSELECTED, evidence_state="sufficient_but_insufficient",
                            observed_value={"candidates": len(pool), "passing_this_rule": len(passes),
                                            "rule_fact_satisfied": True, "complete_plan": False},
                            conditions=person_conditions),
            }
            continue
        codes = {r["reason_code"] for _, r in per_rule if r["reason_code"]}
        factual = [c for c in _PERSON_FAIL_ORDER if c in codes and c in _FACTUAL_PERSON_FAILS]
        if factual:
            code = factual[0]
            sample = next(r for _, r in per_rule if r["reason_code"] == code)
            outcomes[rid] = {
                "result": "not_satisfied",
                "reason": (f"全部 {len(pool)} 名候选均未通过本条：{sample['fail_text']}"
                           f"（候选失败码 {sorted(codes)}）；属资源事实缺口，需更换/补足人选，不能用补一份证明替代"),
                "refs": [],
                "facts": _f(code, evidence_state="sufficient_but_insufficient",
                            observed_value={"candidates": len(pool), "fail_codes": sorted(codes)},
                            next_action="replace_or_add_qualified_person",
                            conditions=person_conditions),
            }
            continue
        code = next((c for c in _PERSON_FAIL_ORDER if c in codes), RC_EVIDENCE_MISSING)
        gap_texts = {
            RC_EVIDENCE_MISSING: "B 证编号台账未录入（需补录核验）",
            RC_EVIDENCE_PENDING_VERIFICATION: "人员记录未核验",
        }
        gaps = sorted({gap_texts.get(c, c) for c in codes})
        outcomes[rid] = {
            "result": "unverifiable",
            "reason": (f"全部 {len(pool)} 名候选均因数据缺口未通过（{ '、'.join(gaps) }）——"
                       "属企业资料待补/待核验，不是人员事实不满足；补录核验后重算即可确认，不推断不满足"),
            "refs": [],
            "facts": _f(code, evidence_state="pending_verification",
                        observed_value={"candidates": len(pool), "fail_codes": sorted(codes)},
                        conditions=person_conditions),
        }
    for p in plans:
        p.pop("_person", None)
    return {"plans": plans, "outcomes": outcomes, "role_code": role_code}


# ---------------------------------------------------------------------------
# 评分分类（docs/12 §2.3 / §3.1 score_classification）
# ---------------------------------------------------------------------------

_METHODOLOGY_ANCHORS = {"evaluation_method", "score_methodology", "bid_evaluation_method"}


def classify_scored(item: dict[str, Any]) -> str:
    """评分项 → 五类分类（确定性；规则显式声明优先，不猜测业务语义）。"""
    declared = item.get("score_classification")
    if declared in (SC_OBJECTIVE_CALCULABLE, SC_SUBJECTIVE_REVIEW, SC_PRICE_FORMULA,
                    SC_METHODOLOGY_ONLY, SC_UNSTRUCTURED):
        return declared
    if item.get("score_nature") == "subjective":
        return SC_SUBJECTIVE_REVIEW
    rule = item.get("rule") or {}
    formula = item.get("score_formula") or {}
    if rule.get("type") == "methodology" or rule.get("anchor_key") in _METHODOLOGY_ANCHORS \
            or formula.get("kind") == "methodology":
        return SC_METHODOLOGY_ONLY
    if item.get("requires_quote") or rule.get("type") in ("quote_cap", "ceiling_price", "price_score") \
            or "bid_price_input" in (item.get("evidence_required") or []):
        return SC_PRICE_FORMULA
    if formula.get("kind") == "fixed_max" or rule.get("type") == "similar_performance":
        return SC_OBJECTIVE_CALCULABLE
    return SC_UNSTRUCTURED


def _match_scored(item: dict[str, Any], evidence: dict[str, list[dict[str, Any]]], as_of: str,
                  ledger_counts: dict[str, int] | None = None,
                  ineligible: dict[str, list[dict]] | None = None,
                  ) -> tuple[str, str, int | None, bool, list[str], dict[str, Any]]:
    """→ (结果, 原因, 得分, 是否满分, 证据回链, 事实层)。

    评分分类口径（docs/12 §2.3）：
    - methodology_only：评标方法说明，仅归档，不计入可复算总分（不产生 0/0）；
    - price_formula：无授权输入 → not_calculable（系统绝不推荐报价）；
    - subjective：未过内部评审 → manual_review，score=None；
    - unstructured / 公式缺失 → not_calculable/formula_missing；
    - 计分证据缺失 → not_calculable/scoring_evidence_missing。
    """
    classification = classify_scored(item)
    facts_base: dict[str, Any] = {"score_classification": classification}

    if classification == SC_METHODOLOGY_ONLY:
        return ("not_applicable",
                "评标方法说明（如综合评估法/随机平均价/百分制），仅归档解释，不计入可复算总分，不产生「0/0 未满分」",
                None, False, [], dict(_f(RC_NOT_APPLICABLE, evidence_state="not_applicable"), **facts_base))
    if item.get("score_nature") == "subjective" or classification == SC_SUBJECTIVE_REVIEW:
        review = item.get("internal_review") or {}
        if review.get("status") == "passed":
            return "satisfied", "内部质量评审已通过（不代表评标委员会得分）", item.get("max_score"), True, [], \
                dict(_f(None, evidence_state="sufficient"), **facts_base)
        # 未通过评审的主观项得分不可计算：score 必须为 None，不得写 0 冒充已计分（ADR-004 §2.5）
        return "manual_review", "待内部质量评审，得分暂不可计算，不能计入内部满分", None, False, [], \
            dict(_f(RC_PROFESSIONAL_JUDGEMENT_REQUIRED), **facts_base)
    rule = item.get("rule") or {}
    if classification == SC_PRICE_FORMULA:
        quotes = [q for q in evidence.get("bid_price_input", []) if q.get("type") == "quoted_price" and q.get("entered_by")]
        if not quotes:
            return "not_calculable", "报价未由人员录入，系统不生成或推荐报价（待授权输入）", None, False, [], \
                dict(_f(RC_AUTHORIZED_INPUT_MISSING), **facts_base)
        if (item.get("score_formula") or {}).get("kind") not in ("fixed_max", "price_deviation"):
            return "not_calculable", "报价评分公式未结构化，无法复算（报价已录入，公式待结构化）", None, False, [], \
                dict(_f(RC_FORMULA_MISSING, observed_value={"quoted_price": quotes[0].get("amount")}), **facts_base)
        score = item.get("max_score")
        return "satisfied", f"报价项按公式复算 {score}/{item.get('max_score')}（输入为人工录入报价）", score, score == item.get("max_score"), \
            _refs_of(quotes[0]), dict(_f(None, evidence_state="sufficient"), **facts_base)
    if rule.get("type") == "similar_performance":
        result, reason, refs, facts = _match_similar_performance(rule, evidence, as_of,
                                                                 ledger_counts=ledger_counts, ineligible=ineligible)
        if result == "satisfied":
            return "satisfied", reason, item.get("max_score"), True, refs, dict(facts, **facts_base)
        mapped = {"unverifiable": "not_calculable"}.get(result, result)
        code = facts.get("reason_code")
        if mapped == "not_calculable" and code in (None, RC_EVIDENCE_MISSING, RC_ENTERPRISE_RECORD_MISSING,
                                                   RC_EVIDENCE_PENDING_VERIFICATION):
            code = RC_SCORING_EVIDENCE_MISSING
        return mapped, reason, None, False, [], dict(_f(code, **{k: v for k, v in facts.items() if k != "reason_code"}), **facts_base)
    if item.get("requires_quote"):
        quotes = [q for q in evidence.get("bid_price_input", []) if q.get("type") == "quoted_price" and q.get("entered_by")]
        if not quotes:
            return "not_calculable", "报价未由人员录入，系统不生成或推荐报价", None, False, [], \
                dict(_f(RC_AUTHORIZED_INPUT_MISSING), **facts_base)
    present: list[str] = []
    for kind in item.get("evidence_required", []):
        records = _evidence(evidence, kind, as_of)
        if not records:
            return "not_calculable", f"缺少时点有效且已核验的计分证据（{kind}），不可计分", None, False, [], \
                dict(_f(RC_SCORING_EVIDENCE_MISSING, evidence_state="missing"), **facts_base)
        present.append(kind)
    score = item.get("max_score") if (item.get("score_formula") or {}).get("kind") == "fixed_max" else None
    if score is None:
        have = f"，已有证据 {present}" if present else ""
        return "not_calculable", f"评分项无可复算的评分公式（score_formula 未结构化）{have}；得分需人工按评标办法核对，不计入内部满分", None, False, [], \
            dict(_f(RC_FORMULA_MISSING), **facts_base)
    refs = _refs_of(_evidence(evidence, item.get("evidence_required", [None])[0], as_of)[0]) if present else []
    return "satisfied", f"客观项可复算 {score}/{item.get('max_score')}", score, score == item.get("max_score"), refs, \
        dict(_f(None, evidence_state="sufficient", observed_value={"score": score},
                required_value={"max_score": item.get("max_score")}), **facts_base)


def _default_task_kind(item: dict[str, Any]) -> str:
    text = f"{item.get('category') or ''} {item.get('assertion') or ''} {item.get('clause_ref') or ''}"
    if "报名" in text or "文件获取" in text or "获取招标文件" in text:
        return "registration"
    if "CA" in text.upper() or "数字证书" in text:
        return "ca_cert"
    if "保证金" in text or "保函" in text:
        return "bond"
    if "踏勘" in text or "现场" in text:
        return "site_visit"
    if "开标" in text:
        return "opening"
    if "递交" in text or "投标文件" in text:
        return "submission"
    if "编制" in text:
        return "preparation"
    return "other"


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def evaluate(requirements: list[dict[str, Any]], evidence: dict[str, list[dict[str, Any]]], *,
             as_of: str, mode: str = "gate", lot_id: str | int | None = None,
             ledger_counts: dict[str, int] | None = None,
             ineligible: dict[str, list[dict]] | None = None) -> dict[str, Any]:
    """确定性规则判定（ADR-008 v2：四域分层 + 事实层 + 候选班子）。

    - 资格/资源（hard）：结论 + 原因码 + 事实值 + 证据状态；
    - 评分（scored）：五类分类，methodology_only 归档不计分，不可计算显式 not_calculable；
    - 执行（action）：结论=动作状态（not_started 等），只进执行计划，不再伪装缺证；
    - 人员（project_manager 等）：候选班子同一 person_id 约束，禁止跨人拼接。
    ``ledger_counts`` / ``ineligible`` 为可选的台账口径输入（docs/12 8.1-1 状态拆分）；
    不提供时回落既有保守判定（不推断）。
    """
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

    # 人员域候选班子（docs/12 §3.3）：先按岗位聚合规则，再按同一人判定
    person_rules_by_type: dict[str, list[dict[str, Any]]] = {}
    for item in requirements:
        rule_type = (item.get("rule") or {}).get("type")
        if item.get("req_type") in ("hard_requirement", "scored_requirement") \
                and rule_type in SINGLE_PERSON_RULE_TYPES:
            person_rules_by_type.setdefault(rule_type, []).append(item)
    person_domain: dict[str, dict[str, Any]] = {}
    for rule_type, rules in person_rules_by_type.items():
        person_domain[rule_type] = _evaluate_person_domain(rules, rule_type, evidence, as_of)

    matrix, blocked, pending, review = [], [], [], []
    for item in requirements:
        refs: list[str] = []
        facts: dict[str, Any] = {}
        rid = item["requirement_id"]
        if item["req_type"] == "hard_requirement":
            rule_type = (item.get("rule") or {}).get("type")
            if rule_type in person_rules_by_type:
                outcome = person_domain[rule_type]["outcomes"][rid]
                result, reason, refs = outcome["result"], outcome["reason"], outcome["refs"]
                facts = outcome["facts"]
            else:
                result, reason, refs, facts = _match_hard(item["rule"], evidence, as_of,
                                                          ledger_counts=ledger_counts, ineligible=ineligible)
            score = None
        elif item["req_type"] == "scored_requirement":
            rule_type = (item.get("rule") or {}).get("type")
            if rule_type in person_rules_by_type:
                outcome = person_domain[rule_type]["outcomes"][rid]
                result, reason, score = outcome["result"], outcome["reason"], item.get("max_score")
                full = result == "satisfied"
                refs = outcome["refs"]
                facts = dict(outcome["facts"], score_classification=classify_scored(item))
            else:
                result, reason, score, full, refs, facts = _match_scored(
                    item, evidence, as_of, ledger_counts=ledger_counts, ineligible=ineligible)
        else:
            # 动作项（docs/12 §2.4 / ADR-008 §2.2-2）：结论=动作状态；只进执行计划，
            # 不再进入缺证/复核队列；除显式 approval_ready 外不参与审批门禁。
            status = item.get("action_status") or "not_started"
            label = ACTION_STATUS_LABEL.get(status, status)
            result = status
            reason = (f"投标动作已{label}（由责任人登记，系统不代办）" if status in ("ready", "completed")
                      else f"投标动作{label}（{item.get('assertion') or '按条款到阶段完成'}；进入投标执行计划跟踪，不属于企业缺证）")
            score = None
            facts = {
                "reason_code": None,
                "task_kind": (item.get("rule") or {}).get("task_kind") or _default_task_kind(item),
                "stage": item.get("required_by_stage") or "approval_ready",
                "evidence_state": "not_applicable",
            }
        entry = {"requirement_id": rid, "req_type": item["req_type"], "clause_ref": item["clause_ref"],
                 "match_result": result, "match_reason": reason, "score": score, "max_score": item.get("max_score")}
        if facts:
            entry["reason_code"] = facts.get("reason_code")
            for key in ("required_value", "observed_value", "evidence_state", "person_id",
                        "person_kind", "candidate_plan_ref", "next_action", "score_classification",
                        "task_kind", "stage", "conditions"):
                if facts.get(key) is not None:
                    entry[key] = facts[key]
        if result == "satisfied" and refs:
            # 企业证据回链（ADR-004 §2.5）：满足项携带判定依据记录的 evidence_refs
            entry["evidence_refs"] = refs
        matrix.append(entry)
        if item["req_type"] == "action_requirement":
            continue  # 动作项不进入资格处置队列（docs/12 O2）
        is_admission_scope = (item.get("decision_scope") or "admission") == "admission"
        if is_admission_scope and result == "not_satisfied":
            blocked.append(entry)
        elif is_admission_scope and result == "unverifiable":
            pending.append(entry)
        elif is_admission_scope and result in ("manual_review", "not_calculable"):
            review.append(entry)
        if mode == "gate" and item["req_type"] == "hard_requirement" and is_admission_scope and result == "not_satisfied":
            break

    hard = [m for m in matrix if m["req_type"] == "hard_requirement"]
    # information_only 条款（例如附件项目管理机构资料包）仍保留在矩阵，
    # 但不进入资格门禁/缺口计数；这是“材料要求”与“企业资格条件”的边界。
    req_by_id = {r.get("requirement_id"): r for r in requirements}
    admission_hard = [m for m in hard if (req_by_id.get(m["requirement_id"], {}).get("decision_scope") or "admission") == "admission"]
    scored = [m for m in matrix if m["req_type"] == "scored_requirement"]
    actions = [m for m in matrix if m["req_type"] == "action_requirement"]
    # methodology_only 仅归档，不参与可复算口径（docs/12 §2.3）
    scored_effective = [m for m in scored if m.get("score_classification") != SC_METHODOLOGY_ONLY]

    def _stage_actions(stage: str) -> list[dict]:
        return [a for a in actions if (a.get("stage") or "approval_ready") == stage]

    approval_actions = _stage_actions("approval_ready")
    approval_actions_ready = all(a["match_result"] in ("ready", "completed") for a in approval_actions)
    stage_readiness = {
        stage: ("ready" if all(a["match_result"] in ("ready", "completed") for a in _stage_actions(stage))
                else "not_ready")
        for stage in ("preparation", "approval_ready", "submission_ready", "submitted", "opened")
    }
    qualification_result = (
        "failed" if any(m["match_result"] == "not_satisfied" for m in admission_hard)
        else ("pending" if any(m["match_result"] in ("unverifiable", "manual_review") for m in admission_hard) else "passed")
    )
    scoring_calculable = any(m.get("score_classification") == SC_OBJECTIVE_CALCULABLE for m in scored_effective)
    scoring_result = ("not_full" if any(m["match_result"] in ("not_satisfied", "unverifiable", "manual_review",
                                                              "not_calculable") for m in scored_effective)
                      else ("full" if scored_effective else "full"))
    hard_ok = not any(m["match_result"] != "satisfied" for m in admission_hard)
    scored_ok = not any(m["match_result"] != "satisfied" for m in scored_effective)
    internal_admission_eligible = (
        hard_ok and scored_ok and approval_actions_ready
        and len(matrix) == len(requirements)
    )

    domain_summaries = {
        "qualification": {
            "satisfied": sum(1 for m in admission_hard if m["match_result"] == "satisfied"),
            "not_satisfied": sum(1 for m in admission_hard if m["match_result"] == "not_satisfied"),
            "blocked_missing_data": sum(1 for m in admission_hard if m["match_result"] == "unverifiable"),
            "manual_review": sum(1 for m in admission_hard if m["match_result"] == "manual_review"),
            "total": len(admission_hard),
        },
        "scoring": {
            "objective_calculable": sum(1 for m in scored if m.get("score_classification") == SC_OBJECTIVE_CALCULABLE),
            "subjective_review": sum(1 for m in scored if m.get("score_classification") == SC_SUBJECTIVE_REVIEW),
            "price_formula": sum(1 for m in scored if m.get("score_classification") == SC_PRICE_FORMULA),
            "methodology_only": sum(1 for m in scored if m.get("score_classification") == SC_METHODOLOGY_ONLY),
            "unstructured": sum(1 for m in scored if m.get("score_classification") == SC_UNSTRUCTURED),
            "calculable": scoring_calculable,
            "total": len(scored),
        },
        "operation": {
            "not_started": sum(1 for a in actions if a["match_result"] == "not_started"),
            "ready": sum(1 for a in actions if a["match_result"] == "ready"),
            "completed": sum(1 for a in actions if a["match_result"] == "completed"),
            "overdue": sum(1 for a in actions if a["match_result"] == "overdue"),
            "approval_gate_ready": approval_actions_ready,
            "stage_readiness": stage_readiness,
            "total": len(actions),
        },
    }

    candidate_plans: list[dict[str, Any]] = []
    for rule_type, domain in person_domain.items():
        candidate_plans.extend(domain["plans"])

    return {
        "as_of": as_of, "mode": mode, "lot_id": lot_id, "matrix": matrix,
        "matcher_version": MATCHER_VERSION,
        "coverage": {"executed": len(matrix), "declared": len(requirements),
                     "complete": len(matrix) == len(requirements)},
        "blocked": blocked, "pending": pending, "review": review,
        "domain_summaries": domain_summaries,
        "candidate_plans": candidate_plans,
        "qualification_result": qualification_result,
        "scoring_result": scoring_result,
        "scoring_calculable": scoring_calculable,
        "operational_readiness": {
            "status": "ready" if all(v == "ready" for v in stage_readiness.values()) else "not_ready",
            "approval_ready": approval_actions_ready,
            "stage_readiness": stage_readiness,
            "action_done": sum(1 for a in actions if a["match_result"] in ("ready", "completed")),
            "action_total": len(actions),
        },
        "internal_admission_eligible": internal_admission_eligible,
    }
