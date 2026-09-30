# 方案 §3.5 第 3-5 步 / F023 §2.3/§6：匹配接入纯逻辑（无第三方依赖）
# RAG 候选 → 结构化核验 → F008/F023 确定性规则引擎。
# 红线（F025 §6 / 方案 §3.5 验收）：
# - 向量分数与 LLM 置信度不进入评分/准入公式（只影响候选发现排序）；
# - 召回不到证据 → unverifiable/blocked_missing_data；明确不满足 → not_satisfied；
# - 缺规则集 / 无法确定 as_of / 规则引擎不可用 → MatchNotRunnableError（任务 retryable，不伪造成功）；
# - 候选核验 blocked（无法判定）的记录必须从判定输入剔除，不得推断满足。
#
# 本模块不 import SQLAlchemy；记录统一按 dict 或带属性的对象读取（_get 兼容两者），
# 供 worker 匹配执行器与单元测试复用。
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Optional

from runtime.rag import verification
from runtime.rag.verification import VerificationItem, VerificationResult


class MatchNotRunnableError(Exception):
    """匹配不可执行（缺规则集/无 as_of/引擎不可用）→ 任务 retryable/manual_review。"""


class GateBlockedError(MatchNotRunnableError):
    """ADR-004 服务端门禁拦截（项目过期 / 身份冲突）：确定性阻断，任务终态失败不重试。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _get(record: Any, key: str, default: Any = None) -> Any:
    """dict 或 ORM 对象统一取值（纯逻辑不绑定 ORM 类型）。"""
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def snapshot_hash(payload: Any) -> str:
    """证据快照 SHA-256（方案 §3.5：同一证据版本 → 同一 hash，可重放比对）。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _snapshot_eligible(record: dict, as_of: str) -> bool:
    """快照层过滤（F023 §2.3）：active、已核验、有效期覆盖 as_of；缺失不推断。

    2026-09-23 语义修正：核验行为时间（verified_at）不再要求早于判定时点——
    核验是对既有事实的事后确认，企业资料库 2026-09 才整体导入核验，而历史项目
    判定时点（投标截止）早于此，旧行为把 30 条有效资质只放行 1 条进快照
    （PJ-b4e65720e2 实测"现有 1 条有效资质"）。有效性仍严格按 valid_from/
    valid_until 对 as_of 判定；未核验（verified_at 为空）依旧不入快照。
    """
    if _get(record, "status") != "active":
        return False
    if not _get(record, "verified_at"):
        return False
    valid_from = _get(record, "valid_from")
    valid_until = _get(record, "valid_until")
    if valid_from and str(valid_from)[:10] > as_of[:10]:
        return False
    if valid_until and str(valid_until)[:10] < as_of[:10]:
        return False
    return True


def build_enterprise_evidence(
    *,
    qualifications: list = None,
    performances: list = None,
    managers: list = None,
    personnel: list = None,
    as_of: str,
) -> dict[str, list[dict]]:
    """企业资料 → engine 证据 dict（截至 as_of 的 active 快照，方案 §3.5 第 2 步）。

    只映射表内确定存在的字段；缺失字段（如业绩核验时间）不推断，
    engine 保守判定 unverifiable（缺失阻断，F008 §2.3）。
    """
    evidence: dict[str, list[dict]] = {}
    for q in qualifications or []:
        record = {
            "material_id": _get(q, "material_id"),
            "category": _get(q, "category"),
            "level": _get(q, "level"),
            "status": _get(q, "status"),
            "verified_at": _iso(_get(q, "verified_at")),
            "valid_from": _iso(_get(q, "valid_from")),
            "valid_until": _iso(_get(q, "valid_until")),
            "evidence_refs": _get(q, "evidence_refs") or [],
        }
        if _snapshot_eligible(record, as_of):
            # F022 §3：安全生产许可证为独立 evidence kind（engine evidence_type=safety_license）
            if "安全生产" in str(_get(q, "category") or ""):
                evidence.setdefault("safety_license", []).append(record)
            else:
                evidence.setdefault("qualification_record", []).append(record)
    for p in performances or []:
        scale = _get(p, "scale_metrics") or {}
        record = {
            "material_id": _get(p, "material_id"),
            "project_name": _get(p, "project_name"),
            "project_type": _get(p, "project_type"),
            "area": scale.get("area"),
            "contract_amount": _get(p, "contract_amount"),
            "completed_at": _iso(_get(p, "completed_at")),
            "subject": "bidder",
            "status": _get(p, "status"),
            # F022：核验时间字段由 0005 迁移补齐；未核验（verified_at 为空）不入快照，
            # engine 判 unverifiable（缺失阻断，不推断满足）。
            "verified_at": _iso(_get(p, "verified_at")),
            "valid_from": None,
            "valid_until": None,
            # 2026-09-23 v1.6：补齐 evidence_refs——满足项证据回链（ADR-004 §2.5）依赖
            # 该字段；此前业绩记录不带，H-023/S-016 满足后被误降级 manual_review
            "evidence_refs": _get(p, "evidence_refs") or [],
        }
        if _snapshot_eligible(record, as_of):
            evidence.setdefault("similar_performance", []).append(record)
    for m in managers or []:
        active = _get(m, "active_projects")
        # JSONB 列若因导入缺陷存成字符串 '[]'（normalize 污染历史数据）→ 容错为空列表
        if isinstance(active, str):
            try:
                import json as _json

                active = _json.loads(active) if active.strip() else []
            except ValueError:
                active = []
        record = {
            "material_id": _get(m, "material_id"),
            "manager_id": _get(m, "manager_id"),
            "display_name": _get(m, "display_name"),
            "specialty": [_get(m, "specialty")] if _get(m, "specialty") else [],
            "cert_level": _get(m, "cert_level"),
            "cert_valid_until": _iso(_get(m, "cert_valid_until")),
            "b_cert": _get(m, "b_cert_no"),  # R022-③：安全B证编号（0008 补齐）；缺失=None→engine require_b_cert 判 unverifiable
            "active_projects": active or [],
            "availability": _get(m, "availability"),
            "status": _get(m, "status"),
            "verified_at": _iso(_get(m, "verified_at")),
            "valid_from": None,
            "valid_until": _iso(_get(m, "cert_valid_until")),
            "evidence_refs": _get(m, "evidence_refs") or [],
        }
        if _snapshot_eligible(record, as_of):
            evidence.setdefault("manager_profile", []).append(record)
    for p in personnel or []:
        cert_level = _get(p, "cert_level")
        # 安全员只按 C 证认定（与 build_ledger_context 同口径；见 2026-09-29 修正说明）
        is_safety_officer = str(cert_level or "").upper().startswith("C")
        if is_safety_officer:
            # engine safety_officer 判定：cert_type=C（F022 §3：C 证专职安全员）
            record = {
                "material_id": _get(p, "material_id"),
                "personnel_id": _get(p, "personnel_id"),
                "cert_type": str(cert_level or "").upper()[:1] or "C",
                "status": _get(p, "status"),
                "verified_at": _iso(_get(p, "verified_at")),
                "valid_from": _iso(_get(p, "valid_from")),
                "valid_until": _iso(_get(p, "valid_until")),
            }
            if _snapshot_eligible(record, as_of):
                evidence.setdefault("safety_officer_cert", []).append(record)
            continue
        record = {
            "material_id": _get(p, "material_id"),
            "personnel_id": _get(p, "personnel_id"),
            "specialty": _get(p, "specialty"),
            "cert_level": cert_level,
            "status": _get(p, "status"),
            "verified_at": _iso(_get(p, "verified_at")),
            "valid_from": _iso(_get(p, "valid_from")),
            "valid_until": _iso(_get(p, "valid_until")),
        }
        if _snapshot_eligible(record, as_of):
            evidence.setdefault("technical_team_member", []).append(record)
    return evidence


def build_ledger_context(
    *,
    qualifications: list = None,
    performances: list = None,
    managers: list = None,
    personnel: list = None,
    as_of: str,
) -> tuple[dict[str, int], dict[str, list[dict]]]:
    """台账口径上下文（docs/12 §3.2 / 8.1-1 状态拆分的数据源）。

    返回 (ledger_counts, ineligible)：
    - ledger_counts：每类证据在台账中的原始记录数（含未核验/过期）——
      0 条 = enterprise_record_missing（真缺资料），>0 条但不入快照 = 待核验/过期；
    - ineligible：不入快照的记录及其原因（expired / unverified / not_yet_valid），
      供引擎把「证书过期」判为事实性不满足，而不是与「无记录」混为同一待补状态。
    只统计四类表内确定存在的记录，不做任何推断。
    """
    counts: dict[str, int] = {}
    ineligible: dict[str, list[dict]] = {}

    def _track(kind: str, record: dict) -> None:
        counts[kind] = counts.get(kind, 0) + 1
        if not record.get("verified_at"):
            ineligible.setdefault(kind, []).append({"reason": "unverified",
                                                    "material_id": record.get("material_id")})
            return
        valid_from = record.get("valid_from")
        valid_until = record.get("valid_until")
        if valid_until and str(valid_until)[:10] < as_of[:10]:
            ineligible.setdefault(kind, []).append({"reason": "expired", "valid_until": str(valid_until)[:10],
                                                    "material_id": record.get("material_id")})
            return
        if valid_from and str(valid_from)[:10] > as_of[:10]:
            ineligible.setdefault(kind, []).append({"reason": "not_yet_valid", "valid_from": str(valid_from)[:10],
                                                    "material_id": record.get("material_id")})

    for q in qualifications or []:
        category = str(_get(q, "category") or "")
        kind = "safety_license" if "安全生产" in category else "qualification_record"
        _track(kind, {
            "material_id": _get(q, "material_id"),
            "verified_at": _iso(_get(q, "verified_at")),
            "valid_from": _iso(_get(q, "valid_from")),
            "valid_until": _iso(_get(q, "valid_until")),
        })
    for p in performances or []:
        _track("similar_performance", {
            "material_id": _get(p, "material_id"),
            "verified_at": _iso(_get(p, "verified_at")),
            "valid_from": None,
            "valid_until": None,
        })
    for m in managers or []:
        _track("manager_profile", {
            "material_id": _get(m, "material_id"),
            "verified_at": _iso(_get(m, "verified_at")),
            "valid_from": None,
            "valid_until": _iso(_get(m, "cert_valid_until")),
        })
    for p in personnel or []:
        cert_level = _get(p, "cert_level")
        # 2026-09-29 修正：专职安全员只按 C 证认定（cert_level 以 C 开头）；
        # specialty 含「安全」的职称人员（如安全工程师）是技术团队，不是 C 证安全员——
        # 旧口径把 4 名安全工程职称人员误计为安全员台账，导致「0 核验=不满足」的误判链。
        is_safety_officer = str(cert_level or "").upper().startswith("C")
        kind = "safety_officer_cert" if is_safety_officer else "technical_team_member"
        _track(kind, {
            "material_id": _get(p, "material_id"),
            "verified_at": _iso(_get(p, "verified_at")),
            "valid_from": _iso(_get(p, "valid_from")),
            "valid_until": _iso(_get(p, "valid_until")),
        })
    return counts, ineligible


def requirement_dict(req: Any) -> dict:
    """Requirement 表行 → engine 契约 dict。

    “项目管理机构人员资料包”是投标文件附件提交清单，不是一个额外的技术负责人
    资格条件；对历史规则快照也做同样的保守归类，避免旧解析结果继续制造伪缺口。
    """
    assertion = _get(req, "assertion") or ""
    rule = _get(req, "rule") or {}
    attachment_markers = "应附" in assertion and ("扫描件" in assertion or "原件" in assertion)
    person_package_markers = sum(token in assertion for token in ("身份证", "职称证", "养老保险", "注册资格证书")) >= 2
    package_context = "项目管理机构" in assertion or any(token in assertion for token in ("技术负责人", "合同商务负责人", "岗位人员"))
    submission_only = attachment_markers and person_package_markers and package_context
    decision_scope = _get(req, "decision_scope")
    if submission_only:
        decision_scope = "information_only"
        rule = dict(rule)
        rule["submission_package_only"] = True
        rule["type"] = "submission_package"
    return {
        "requirement_id": _get(req, "requirement_id"),
        "req_type": _get(req, "req_type"),
        "category": _get(req, "category"),
        "lot_id": _get(req, "lot_id"),
        "clause_ref": _get(req, "clause_ref"),
        "assertion": _get(req, "assertion"),
        "rule": rule,
        "decision_scope": decision_scope,
        "evidence_required": _get(req, "evidence_required") or [],
        "missing_action": _get(req, "missing_action"),
        "failure_effect": _get(req, "failure_effect"),
        "as_of": _get(req, "as_of"),
        "action_status": _get(req, "action_status"),
        # ADR-008（docs/12 §2.4）：动作阶段随规则透传——approval_ready 参与审批门禁，
        # 其余阶段到点检查；缺失时引擎保守按 approval_ready 处理（fail-closed）
        "required_by_stage": _get(req, "required_by_stage"),
        "max_score": _get(req, "max_score"),
        "score_nature": _get(req, "score_nature"),
        "score_formula": _get(req, "score_formula"),
    }


def candidate_required_from_rule(rule: dict, as_of: str) -> dict:
    """从 F008 规则表达式提取可核验约束（仅取明确存在的字段，不推断）。

    返回 verify_candidates 的 required 参数；无约束字段时为空 dict。
    """
    required: dict[str, Any] = {}
    if rule.get("level"):
        required["level_min"] = rule["level"]
    if rule.get("min_area") is not None:
        required["amount_min"] = Decimal(str(rule["min_area"]))
    if rule.get("amount") is not None:
        required["amount_min"] = Decimal(str(rule["amount"]))
    if rule.get("since"):
        required["date_after"] = str(rule["since"])
    if rule.get("date_after"):
        required["date_after"] = str(rule["date_after"])
    if rule.get("date_before"):
        required["date_before"] = str(rule["date_before"])
    if as_of:
        required["valid_as_of"] = as_of
    return required


def candidate_record(
    *,
    material_id: str,
    content_hash: Optional[str] = None,
    amount: Any = None,
    level: Any = None,
    date: Any = None,
    valid_from: Any = None,
    valid_until: Any = None,
) -> dict:
    """结构化记录 → 核验候选 dict（verify_candidates 输入，金额转字符串）。"""
    record: dict[str, Any] = {"evidence_ref": material_id, "material_id": material_id}
    if content_hash:
        record["content_hash"] = content_hash
    if amount is not None:
        record["amount"] = str(amount) if not isinstance(amount, str) else amount
    if level:
        record["level"] = level
    if date:
        record["date"] = _iso(date)
    if valid_from:
        record["valid_from"] = _iso(valid_from)
    if valid_until:
        record["valid_until"] = _iso(valid_until)
    return record


def candidate_from_record(
    kind: str, record: dict, *, content_hash: Optional[str] = None
) -> dict:
    """按证据 kind 从结构化记录构造核验候选（只取该 kind 明确存在的字段）。

    避免把无关字段（如资质记录的核验时间）带入日期/金额核验造成误判。
    """
    material_id = record.get("material_id") or ""
    if kind == "similar_performance":
        return candidate_record(
            material_id=material_id,
            content_hash=content_hash,
            amount=record.get("area") or record.get("contract_amount"),
            date=record.get("completed_at"),
            valid_from=record.get("valid_from"),
            valid_until=record.get("valid_until"),
        )
    if kind in ("qualification_record", "manager_profile", "technical_team_member"):
        return candidate_record(
            material_id=material_id,
            content_hash=content_hash,
            level=record.get("level") or record.get("cert_level"),
            valid_from=record.get("valid_from"),
            valid_until=record.get("valid_until"),
        )
    return candidate_record(material_id=material_id, content_hash=content_hash)


def gate_candidates(candidates: list[dict], required: dict) -> VerificationResult:
    """对 RAG 候选证据做约束核验（方案 §3.5 第 3 步，无候选 → blocked）。"""
    return verification.verify_candidates(candidates, required=required)


def verification_summary(result: VerificationResult) -> dict:
    """VerificationResult → 可落库 dict（MatchRun.structured_verification）。"""
    return {
        "passed": [_item_dict(i) for i in result.passed],
        "failed": [_item_dict(i) for i in result.failed],
        "blocked": [_item_dict(i) for i in result.blocked],
        "all_passed": result.all_passed,
    }


def _item_dict(item: VerificationItem) -> dict:
    out: dict[str, Any] = {"evidence_ref": item.evidence_ref, "field": item.field}
    if item.expected is not None:
        out["expected"] = str(item.expected)
    if item.actual is not None:
        out["actual"] = str(item.actual)
    out["passed"] = item.passed
    if item.note:
        out["note"] = item.note
    return out


def blocked_material_ids(result: VerificationResult) -> list[str]:
    """核验 blocked（无法判定）候选的 material_id → 从判定输入剔除。"""
    return sorted({i.evidence_ref for i in result.blocked if i.evidence_ref})


def run_match(
    *,
    requirements: list[dict],
    evidence: dict[str, list[dict]],
    as_of: str,
    mode: str = "gate",
    lot_id: Optional[str] = None,
    evaluate_fn: Optional[Callable] = None,
    ledger_counts: Optional[dict[str, int]] = None,
    ineligible: Optional[dict[str, list[dict]]] = None,
) -> dict:
    """执行规则判定（方案 §3.5 第 4 步 / F023 §2 第 4 步）：只有核验通过的证据交给规则引擎。

    F023 §2 第 4 步分两次执行：先 ``diagnostic`` 全量得到每条要求的判定与原因，再 ``gate``
    计算业务状态（硬性不满足即短路、一票否决）。返回合并结果：矩阵取全量（每条要求都有
    结论与原因，用户可逐条核对满足/不满足），短路后门禁未执行到的条目逐条标 ``gate_executed=False``
    并在 ``coverage.short_circuited`` 点名（F008 覆盖门禁：未执行项必须标识，禁止展示为通过）。
    ``mode="diagnostic"`` 直接返回全量结果。

    ``ledger_counts`` / ``ineligible``（docs/12 §3.2 状态拆分）：台账原始记录数与不入快照
    记录的原因，供引擎区分「真缺资料」与「证据充分但不满足/已过期」。

    evaluate_fn 可注入（测试）；默认 scripts.matching.engine.evaluate（延迟导入，
    引擎不可用 → MatchNotRunnableError，不伪造成功）。
    """
    if not requirements:
        raise MatchNotRunnableError("规则集为空，匹配不可执行（不伪造成功）")
    fn = evaluate_fn or _load_engine_evaluate()
    kwargs: dict = {}
    if ledger_counts is not None:
        kwargs["ledger_counts"] = ledger_counts
    if ineligible is not None:
        kwargs["ineligible"] = ineligible
    try:
        diagnostic = fn(requirements, evidence, as_of=as_of, mode="diagnostic", lot_id=lot_id, **kwargs)
    except TypeError:
        # 兼容注入的旧签名 evaluate_fn（无台账口径参数）
        diagnostic = fn(requirements, evidence, as_of=as_of, mode="diagnostic", lot_id=lot_id)
    if mode == "diagnostic":
        return diagnostic
    try:
        gate = fn(requirements, evidence, as_of=as_of, mode="gate", lot_id=lot_id, **kwargs)
    except TypeError:
        gate = fn(requirements, evidence, as_of=as_of, mode="gate", lot_id=lot_id)
    return merge_gate_with_diagnostic(gate=gate, diagnostic=diagnostic)


def merge_gate_with_diagnostic(*, gate: dict, diagnostic: dict) -> dict:
    """门禁结果 + 诊断全量结果 → 单次 gate 运行的完整输出（F023 §2 第 4 步）。

    - matrix/blocked/pending/review 取诊断全量：处置队列列出全部缺项而非短路前的第一项；
    - 业务结论（qualification_result 等）以诊断全量如实汇总——硬性失败在两种模式下都在
      blocked 内，一票否决结论不变；门禁在截断矩阵上算出的 scoring/readiness 是空集上的
      「满分/就绪」，属虚假结论，不采用；
    - coverage.executed/complete 描述本次运行实际执行（全量），gate_executed/gate_complete/
      short_circuited 描述门禁短路情况。
    """
    gate_ids = {e.get("requirement_id") for e in gate.get("matrix") or []}
    matrix: list[dict] = []
    by_id: dict[Any, dict] = {}
    for entry in diagnostic.get("matrix") or []:
        item = dict(entry)
        item["gate_executed"] = item.get("requirement_id") in gate_ids
        matrix.append(item)
        by_id[item.get("requirement_id")] = item
    short_circuited = [e["requirement_id"] for e in matrix if not e["gate_executed"]]

    def _annotated(entries: list[dict] | None) -> list[dict]:
        # 队列项与矩阵同源同标记（处置队列据此提示「门禁短路后·诊断结果」）
        return [by_id.get(e.get("requirement_id"), dict(e, gate_executed=True)) for e in entries or []]

    cov_full = dict(diagnostic.get("coverage") or {})
    cov_gate = gate.get("coverage") or {}
    coverage = {
        "declared": cov_full.get("declared"),
        "executed": cov_full.get("executed"),
        "complete": cov_full.get("complete"),
        "gate_executed": cov_gate.get("executed"),
        "gate_complete": cov_gate.get("complete"),
        "short_circuited": short_circuited,
    }
    merged = dict(diagnostic)
    merged.update(
        mode="gate", matrix=matrix, coverage=coverage,
        blocked=_annotated(diagnostic.get("blocked")),
        pending=_annotated(diagnostic.get("pending")),
        review=_annotated(diagnostic.get("review")),
    )
    # 门禁若已短路（硬性失败），准入必然不可通过；两种模式在此一致，取交集防御引擎实现差异
    merged["internal_admission_eligible"] = bool(
        diagnostic.get("internal_admission_eligible")) and bool(gate.get("internal_admission_eligible"))
    return merged


EVIDENCE_DOWNGRADE_NOTE = (
    "满足结论缺少可回链的企业证据引用（evidence_refs 为空），"
    "按 ADR-004 §2.5 / 优化方案 §7.2 降级为人工复核，不计入正式准入"
)


def _document_fact_exempt(req: dict) -> bool:
    """仅依赖招标文件事实、无需企业证据的规则：联合体极性（接受/不接受均为条款事实，
    2026-09-29 裁定：不接受联合体=按独立投标人投标即可，无需任何声明材料）。"""
    rule = (req or {}).get("rule") or {}
    if rule.get("type") == "consortium":
        return rule.get("allowed", rule.get("accepts_consortium")) is not None
    return False


def downgrade_satisfied_without_evidence(
    result: dict, evidence_refs_by_req: dict[str, dict], requirements: list[dict]
) -> dict:
    """ADR-004 §2.5 证据链最低要求：hard/scored 的 satisfied 无企业证据回链 → manual_review。

    就地修改 engine 结果（matrix 条目、review 队列、准入布尔、评分/资格汇总），并在
    ``coverage.evidence_downgraded`` 点名被降级条目；动作类要求由人员登记、不需企业证据，
    不在此降级。RAG 索引未就绪导致全部无回链时，所有满足项一律降级——不以"匹配跑完"冒充匹配正确。
    """
    req_by_id = {r.get("requirement_id"): r for r in requirements if r.get("requirement_id")}
    downgraded: list[str] = []
    matrix = result.get("matrix") or []
    for entry in matrix:
        if entry.get("match_result") != "satisfied":
            continue
        if entry.get("req_type") not in ("hard_requirement", "scored_requirement"):
            continue
        rid = entry.get("requirement_id")
        # 回链两源（2026-09-23 v1.6）：① RAG 检索召回的 L3 chunk 引用；② 引擎判定时
        # 满足记录自带的结构化 evidence_refs（blocking 式直接回链——L3 向量索引未就绪
        # 时满足项不再一律降级，判定依据仍是已核验的结构化快照）。
        refs = ((evidence_refs_by_req.get(rid) or {}).get("evidence_refs")
                or entry.get("evidence_refs") or [])
        if refs or _document_fact_exempt(req_by_id.get(rid) or {}):
            continue
        entry["match_result"] = "manual_review"
        entry["match_reason"] = f"{entry.get('match_reason') or ''}；{EVIDENCE_DOWNGRADE_NOTE}".lstrip("；")
        entry["score"] = None
        entry["evidence_downgraded"] = True
        result.setdefault("review", []).append(entry)
        downgraded.append(rid)
    if downgraded:
        result["internal_admission_eligible"] = False
        hard_ids = {e.get("requirement_id") for e in matrix if e.get("req_type") == "hard_requirement"}
        scored_ids = {e.get("requirement_id") for e in matrix if e.get("req_type") == "scored_requirement"}
        if any(r in scored_ids for r in downgraded):
            result["scoring_result"] = "not_full"
        if any(r in hard_ids for r in downgraded) and result.get("qualification_result") == "passed":
            result["qualification_result"] = "pending"
        result.setdefault("coverage", {})["evidence_downgraded"] = list(downgraded)
    return result


def _load_engine_evaluate() -> Callable:
    try:
        from scripts.matching import engine  # 纯标准库确定性引擎（F008）
    except ImportError as exc:  # pragma: no cover - 依赖探测
        raise MatchNotRunnableError("规则引擎不可用（scripts.matching.engine 无法导入）") from exc
    return engine.evaluate