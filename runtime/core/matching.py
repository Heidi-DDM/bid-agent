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
    """快照层过滤（F023 §2.3）：active、已核验、有效期覆盖 as_of；缺失不推断。"""
    if _get(record, "status") != "active":
        return False
    verified = _get(record, "verified_at")
    if not verified or str(verified)[:10] > as_of[:10]:
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
        }
        if _snapshot_eligible(record, as_of):
            evidence.setdefault("similar_performance", []).append(record)
    for m in managers or []:
        record = {
            "material_id": _get(m, "material_id"),
            "manager_id": _get(m, "manager_id"),
            "display_name": _get(m, "display_name"),
            "specialty": [_get(m, "specialty")] if _get(m, "specialty") else [],
            "cert_level": _get(m, "cert_level"),
            "cert_valid_until": _iso(_get(m, "cert_valid_until")),
            "active_projects": _get(m, "active_projects") or [],
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
        record = {
            "material_id": _get(p, "material_id"),
            "personnel_id": _get(p, "personnel_id"),
            "specialty": _get(p, "specialty"),
            "cert_level": _get(p, "cert_level"),
            "status": _get(p, "status"),
            "verified_at": _iso(_get(p, "verified_at")),
            "valid_from": _iso(_get(p, "valid_from")),
            "valid_until": _iso(_get(p, "valid_until")),
        }
        if _snapshot_eligible(record, as_of):
            evidence.setdefault("technical_team_member", []).append(record)
    return evidence


def requirement_dict(req: Any) -> dict:
    """Requirement 表行 → engine 契约 dict（F008 §4.1 字段透传，不做业务推断）。"""
    return {
        "requirement_id": _get(req, "requirement_id"),
        "req_type": _get(req, "req_type"),
        "category": _get(req, "category"),
        "lot_id": _get(req, "lot_id"),
        "clause_ref": _get(req, "clause_ref"),
        "assertion": _get(req, "assertion"),
        "rule": _get(req, "rule"),
        "evidence_required": _get(req, "evidence_required") or [],
        "missing_action": _get(req, "missing_action"),
        "failure_effect": _get(req, "failure_effect"),
        "as_of": _get(req, "as_of"),
        "action_status": _get(req, "action_status"),
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
) -> dict:
    """执行规则判定（方案 §3.5 第 4 步）：只有核验通过的证据交给规则引擎。

    evaluate_fn 可注入（测试）；默认 scripts.matching.engine.evaluate（延迟导入，
    引擎不可用 → MatchNotRunnableError，不伪造成功）。
    """
    if not requirements:
        raise MatchNotRunnableError("规则集为空，匹配不可执行（不伪造成功）")
    fn = evaluate_fn or _load_engine_evaluate()
    return fn(requirements, evidence, as_of=as_of, mode=mode, lot_id=lot_id)


def _load_engine_evaluate() -> Callable:
    try:
        from scripts.matching import engine  # 纯标准库确定性引擎（F008）
    except ImportError as exc:  # pragma: no cover - 依赖探测
        raise MatchNotRunnableError("规则引擎不可用（scripts.matching.engine 无法导入）") from exc
    return engine.evaluate