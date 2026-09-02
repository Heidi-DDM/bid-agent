# 方案 §3.5 第 3-4 步 / F025 §6：结构化核验（纯逻辑，无第三方依赖）
# RAG 候选 → 单位/金额/日期/等级/有效期/版本/哈希核验 → 只有通过的证据
# 才能交给 F008/F023 确定性规则引擎。向量分数/LLM 置信度不进入核验（F025 §6）。
#
# 本模块供 R023 匹配服务调用；核验失败必须显式 blocked，不得推断满足。
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from runtime.rag.normalizer import level_ge, normalize_amount, normalize_date, normalize_level


@dataclass
class VerificationItem:
    """单项核验结果（证据化，回链 evidence/chunk）。"""

    evidence_ref: str
    field: str  # amount / date / level / validity / version / hash
    expected: Any = None
    actual: Any = None
    passed: bool = False
    note: str = ""


@dataclass
class VerificationResult:
    """一组候选的结构化核验结果。

    - passed：可交给规则引擎的项；
    - failed：明确不满足的项（映射 not_satisfied/硬性阻断）；
    - blocked：无法判定（缺字段/哈希不符/过期）→ 映射 unverifiable/blocked_missing_data。
    """

    passed: list[VerificationItem] = field(default_factory=list)
    failed: list[VerificationItem] = field(default_factory=list)
    blocked: list[VerificationItem] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return not self.failed and not self.blocked


def verify_amount_ge(evidence_ref: str, raw: str | None, min_amount: Decimal) -> VerificationItem:
    """金额核验：normalize_amount 后比较；无法解析 → blocked（不得推断）。"""
    actual = normalize_amount(raw)
    if actual is None:
        return VerificationItem(evidence_ref, "amount", expected=str(min_amount), actual=raw,
                                passed=False, note="金额无法解析（待补/待核实）")
    return VerificationItem(evidence_ref, "amount", expected=str(min_amount), actual=str(actual),
                            passed=actual >= min_amount)


def verify_level_ge(evidence_ref: str, raw: str | None, min_level: str) -> VerificationItem:
    """等级核验：中文等级归一化后 ≥ 语义；未知 → blocked。"""
    actual = normalize_level(raw)
    result = level_ge(actual, min_level)
    if result is None:
        return VerificationItem(evidence_ref, "level", expected=min_level, actual=raw,
                                passed=False, note="等级无法判定（待补/待核实）")
    return VerificationItem(evidence_ref, "level", expected=min_level, actual=actual, passed=result)


def verify_date_window(evidence_ref: str, raw: str | None, *, after: str | None = None,
                       before: str | None = None) -> VerificationItem:
    """日期核验：ISO 归一化后检查窗口；无法解析 → blocked。"""
    actual = normalize_date(raw)
    if actual is None:
        return VerificationItem(evidence_ref, "date", expected=f"{after}~{before}", actual=raw,
                                passed=False, note="日期无法解析（待补/待核实）")
    ok = True
    if after:
        after_d = datetime.fromisoformat(after).date()
        ok = ok and actual >= after_d
    if before:
        before_d = datetime.fromisoformat(before).date()
        ok = ok and actual <= before_d
    return VerificationItem(evidence_ref, "date", expected=f"{after}~{before}", actual=actual.isoformat(),
                            passed=ok)


def _parse_day(value: Any) -> date | None:
    """valid_from/valid_until 边界解析为日期（避免混合时区比较，有效期精确到天）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def verify_validity(evidence_ref: str, *, valid_from=None, valid_until=None,
                    as_of: str) -> VerificationItem:
    """有效期核验：as_of 落在 [valid_from, valid_until]；缺失边界按开放处理，
    但 valid_until 早于 as_of 必须 failed（过期），valid_from 晚于 as_of 必须 failed（未生效）；
    边界无法解析 → blocked（不得推断）。"""
    as_of_day = datetime.fromisoformat(as_of.replace("Z", "+00:00")).date()
    v_from = _parse_day(valid_from)
    v_until = _parse_day(valid_until)
    if valid_from is not None and v_from is None:
        return VerificationItem(evidence_ref, "validity", expected=f"<= {as_of}",
                                actual=valid_from, passed=False, note="有效起期无法解析（待补/待核实）")
    if valid_until is not None and v_until is None:
        return VerificationItem(evidence_ref, "validity", expected=f">= {as_of}",
                                actual=valid_until, passed=False, note="有效止期无法解析（待补/待核实）")
    if v_until is not None and v_until < as_of_day:
        return VerificationItem(evidence_ref, "validity", expected=f">= {as_of}",
                                actual=v_until.isoformat(), passed=False, note="证据已过期")
    if v_from is not None and v_from > as_of_day:
        return VerificationItem(evidence_ref, "validity", expected=f"<= {as_of}",
                                actual=v_from.isoformat(), passed=False, note="证据未生效")
    return VerificationItem(evidence_ref, "validity", expected=f"as_of={as_of}",
                            actual="in-range", passed=True)


def verify_hash(evidence_ref: str, actual_hash: str | None, expected_hash: str) -> VerificationItem:
    """内容哈希核验：不一致 → failed（原文不可变，F025 §8.1）。"""
    if actual_hash is None:
        return VerificationItem(evidence_ref, "hash", expected=expected_hash[:16],
                                actual=None, passed=False, note="缺失哈希")
    return VerificationItem(evidence_ref, "hash", expected=expected_hash[:16],
                            actual=actual_hash[:16], passed=actual_hash == expected_hash)


def verify_candidates(candidates: list[dict], *, required: dict) -> VerificationResult:
    """对 RAG 候选证据做结构化核验（方案 §3.5 第 3 步）。

    candidates：检索候选（chunk/evidence 字典，含 evidence_ref/content_hash/字段）。
    required：{amount_min, level_min, date_after, date_before, valid_as_of, content_hash}。
    任一 candidate 满足全部可判定约束即 passed；无法判定进 blocked，明确不满足进 failed。
    """
    result = VerificationResult()
    if not candidates:
        result.blocked.append(VerificationItem("", "evidence", note="无候选证据（召回为空）"))
        return result
    for cand in candidates:
        ref = cand.get("evidence_ref") or cand.get("chunk_id") or cand.get("material_id") or ""
        items: list[VerificationItem] = []
        if "amount_min" in required:
            items.append(verify_amount_ge(ref, cand.get("amount"), required["amount_min"]))
        if "level_min" in required:
            items.append(verify_level_ge(ref, cand.get("level"), required["level_min"]))
        if "date_after" in required or "date_before" in required:
            items.append(verify_date_window(ref, cand.get("date"),
                                            after=required.get("date_after"),
                                            before=required.get("date_before")))
        if "valid_as_of" in required:
            items.append(verify_validity(ref, valid_from=cand.get("valid_from"),
                                         valid_until=cand.get("valid_until"),
                                         as_of=required["valid_as_of"]))
        if "content_hash" in required:
            items.append(verify_hash(ref, cand.get("content_hash"), required["content_hash"]))
        if not items:
            result.blocked.append(VerificationItem(ref, "none", note="无可判定约束"))
            continue
        if any(not i.passed and i.note and "无法" in i.note for i in items):
            result.blocked.append(VerificationItem(ref, "mixed", note="存在无法判定项"))
        elif all(i.passed for i in items):
            result.passed.extend(items)
        else:
            result.failed.extend([i for i in items if not i.passed])
    return result