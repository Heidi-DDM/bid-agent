"""R010 投标审批/驳回/人工豁免/审计 —— 确定性最小实现（F009）。

规则要点（F009 §6）：
1. 满分 ≠ 自动投标：只有 internal_admission_eligible 才进入 pending_bid_approval；
2. 驳回必须填写意见（comment 必填）；
3. 豁免不改变"满分"定义，只允许带着豁免项进入人工审批视野；
4. 豁免必须附原因、证据、有效期；过期自动失效并回到阻断状态；
5. 全部动作留审计（谁、何时、依据、结论）；系统不代替公司作法律/商业承诺。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any


def _day(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@dataclass
class AuditEntry:
    """审计记录：谁、何时、依据什么、结论。"""
    actor: str
    at: str
    action: str
    basis: str
    outcome: str


@dataclass
class Waiver:
    """人工豁免（F009 §4.2）。"""
    waiver_id: str
    authorizer: str
    reason: str
    evidence_refs: list[str]
    valid_until: str
    approved_at: str
    covered_items: list[str] = field(default_factory=list)

    def is_active(self, as_of: str) -> bool:
        until = _day(self.valid_until)
        point = _day(as_of)
        return until is not None and point is not None and point <= until


@dataclass
class ApprovalRecord:
    """审批记录（F009 §4.1）+ 审计流。"""
    approval_id: str
    project_id: str
    admission_result_ref: str
    approver: str
    decision: str = "pending"            # pending / approved / rejected / waived
    decided_at: str | None = None
    comment: str | None = None
    waivers: list[Waiver] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)


def create_approval(admission_result: dict[str, Any], *, approval_id: str, project_id: str,
                    approver: str, admission_result_ref: str | None = None) -> ApprovalRecord:
    """满分才进入审批队列；未满分不得创建审批（F009 §7 未到满分不进审批队列）。"""
    if not admission_result.get("internal_admission_eligible"):
        raise ValueError("内部准入未满足：只有满分（无阻断/待补/复核且覆盖完整）才可进入投标审批")
    record = ApprovalRecord(
        approval_id=approval_id,
        project_id=project_id,
        admission_result_ref=admission_result_ref or f"admission:{project_id}",
        approver=approver,
    )
    record.audit.append(AuditEntry(actor=approver, at="created", action="create_approval",
                                   basis=admission_result_ref or f"admission:{project_id}",
                                   outcome="pending_bid_approval"))
    return record


def _require_pending(record: ApprovalRecord) -> None:
    if record.decision != "pending":
        raise ValueError(f"审批 {record.approval_id} 已终态（{record.decision}），不可重复决策")


def approve(record: ApprovalRecord, *, approver: str, at: str, comment: str | None = None) -> ApprovalRecord:
    """审批通过 → approved_for_bidding。仅待审记录可决策。"""
    _require_pending(record)
    record.decision = "approved"
    record.decided_at = at
    record.comment = comment
    record.audit.append(AuditEntry(actor=approver, at=at, action="approve",
                                   basis=record.admission_result_ref, outcome="approved_for_bidding"))
    return record


def reject(record: ApprovalRecord, *, approver: str, at: str, comment: str) -> ApprovalRecord:
    """驳回 → rejected_by_approver；comment 必填（F009 §6.4）。"""
    _require_pending(record)
    if not comment or not comment.strip():
        raise ValueError("驳回必须填写意见（comment 必填）")
    record.decision = "rejected"
    record.decided_at = at
    record.comment = comment
    record.audit.append(AuditEntry(actor=approver, at=at, action="reject",
                                   basis=record.admission_result_ref, outcome="rejected_by_approver"))
    return record


def add_waiver(record: ApprovalRecord, *, waiver_id: str, authorizer: str, reason: str,
               evidence_refs: list[str], valid_until: str, approved_at: str,
               covered_items: list[str] | None = None) -> ApprovalRecord:
    """发起豁免：原因/证据/有效期必填；豁免不改变满分定义（F009 §6.3）。"""
    if not reason or not reason.strip():
        raise ValueError("豁免原因必填")
    if not evidence_refs:
        raise ValueError("豁免必须附证据（在途材料/受理回执等）")
    if _day(valid_until) is None:
        raise ValueError("豁免有效期必填且须为合法日期")
    waiver = Waiver(waiver_id=waiver_id, authorizer=authorizer, reason=reason,
                    evidence_refs=evidence_refs, valid_until=valid_until,
                    approved_at=approved_at, covered_items=covered_items or [])
    record.waivers.append(waiver)
    record.audit.append(AuditEntry(actor=authorizer, at=approved_at, action="add_waiver",
                                   basis="; ".join(evidence_refs), outcome=f"waiver:{waiver_id}"))
    return record


def decide_waived(record: ApprovalRecord, *, approver: str, at: str, comment: str) -> ApprovalRecord:
    """带豁免批准 → waived（仍由审批人决策，系统不自动放行）。"""
    _require_pending(record)
    active = [w for w in record.waivers if w.is_active(at)]
    if not active:
        raise ValueError("无有效豁免（缺失或已过期），不得带豁免批准")
    record.decision = "waived"
    record.decided_at = at
    record.comment = comment
    record.audit.append(AuditEntry(actor=approver, at=at, action="decide_waived",
                                   basis=f"waivers={[w.waiver_id for w in active]}",
                                   outcome="approved_with_waiver"))
    return record


def expire_waivers(record: ApprovalRecord, *, as_of: str) -> ApprovalRecord:
    """豁免过期自动失效；若审批仍待审且存在过期豁免 → 回到阻断状态（F009 §6.3/§7）。"""
    changed = False
    for waiver in record.waivers:
        if not waiver.is_active(as_of) and waiver.valid_until < as_of:
            changed = True
    if changed and record.decision == "pending":
        record.decision = "blocked_waiver_expired"
        record.audit.append(AuditEntry(actor="system", at=as_of, action="expire_waivers",
                                       basis="valid_until 已过", outcome="blocked_waiver_expired"))
    return record


def audit_log(record: ApprovalRecord) -> list[dict[str, Any]]:
    """导出完整审计流（谁、何时、依据、结论），可序列化。"""
    return [asdict(entry) for entry in record.audit]


def to_dict(record: ApprovalRecord) -> dict[str, Any]:
    data = asdict(record)
    data["state"] = record.decision
    return data