"""ADR-004 Iteration 1 workflow service.

This module deliberately separates early preparation coordination from formal
admission and turns matching issues into role-owned remediation work. It never
returns a bid recommendation, price, or automatic decision.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runtime.core import rbac
from runtime.core.errors import ApiError
from runtime.db.models import (
    AdmissionResult,
    Material,
    PreparationRecord,
    Project,
    ProjectIdentity,
    QuickPrescreenRun,
    RemediationTask,
)

PRESCREEN_DISCLAIMER = "快速预核仅汇总当前已登记事实、待补和待核实事项；不是投标建议、不是正式匹配，不能替代正式投标审批。"

PREPARATION_PENDING = "preparation_pending"
PREPARATION_APPROVED = "preparation_approved"
PREPARATION_DECLINED = "preparation_declined"

TASK_OPEN = "open"
TASK_IN_PROGRESS = "in_progress"
TASK_EVIDENCE_SUBMITTED = "evidence_submitted"
TASK_RESOLVED_PENDING_RECALCULATION = "resolved_pending_recalculation"
TASK_CLOSED = "closed"
TASK_REJECTED = "rejected"
TASK_CANCELLED = "cancelled"
TASK_OVERDUE = "overdue"
TASK_TERMINAL = frozenset({TASK_CLOSED, TASK_REJECTED, TASK_CANCELLED})
TASK_ACTIVE = frozenset({TASK_OPEN, TASK_IN_PROGRESS, TASK_EVIDENCE_SUBMITTED, TASK_RESOLVED_PENDING_RECALCULATION, TASK_OVERDUE})

TASK_TYPES = frozenset({
    "document_check", "evidence_supplement", "rule_review", "resource_confirmation",
    "identity_confirmation", "termination_correction", "stale_review",
})

DEFAULT_ROLE_BY_TYPE = {
    "document_check": rbac.BID_SPECIALIST,
    "evidence_supplement": rbac.DATA_ADMIN,
    "rule_review": rbac.BID_SPECIALIST,
    "resource_confirmation": rbac.BID_SPECIALIST,
    "identity_confirmation": rbac.BID_SPECIALIST,
    "termination_correction": rbac.BID_SPECIALIST,
    "stale_review": rbac.BID_SPECIALIST,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(session: Session, *, actor: str, action: str, outcome: str | None = None,
           basis: str | None = None, object_ref: str | None = None) -> None:
    from runtime.db.api_service import audit
    audit(session, actor=actor, action=action, outcome=outcome, basis=basis, object_ref=object_ref)


def _project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise ApiError("not_found", f"项目不存在: {project_id}")
    return project


def _task_payload(task: RemediationTask, *, viewer_role: str | None = None) -> dict[str, Any]:
    """Serialize a remediation task without leaking private evidence references.

    Evidence references may later resolve to enterprise-private material versions.
    Only the two business roles may receive the exact references (F026 §5：
    投标专员含资料核验职责；经营负责人全量可见)。其他/未知角色只见聚合证据状态；
    历史 data_admin/legal 别名在入口已归并为投标专员。
    """
    can_view_evidence_refs = viewer_role in {rbac.BID_SPECIALIST, rbac.BUSINESS_HEAD}
    payload = {
        "task_id": task.task_id,
        "project_id": task.project_id,
        "task_type": task.task_type,
        "state": task.state,
        "title": task.title,
        "description": task.description,
        "source_kind": task.source_kind,
        "source_ref": task.source_ref,
        "source_match_run_id": task.source_match_run_id,
        "requirement_id": task.requirement_id,
        "assignee_role": task.assignee_role,
        "assignee": task.assignee,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "evidence_required": task.evidence_required or [],
        "evidence_count": len(task.evidence_refs or []),
        "evidence_submitted": bool(task.evidence_refs),
        "resolution": task.resolution,
        "closing_condition": task.closing_condition,
        "created_by": task.created_by,
        "closed_by": task.closed_by,
        "closed_at": task.closed_at.isoformat() if task.closed_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }
    if can_view_evidence_refs:
        payload["evidence_refs"] = task.evidence_refs or []
    return payload


def _preparation_payload(row: PreparationRecord | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "preparation_id": row.preparation_id,
        "project_id": row.project_id,
        "state": row.state,
        "reason": row.reason,
        "created_by": row.created_by,
        "decided_by": row.decided_by,
        "decision_comment": row.decision_comment,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "meaning": "仅代表投入投标准备工作；不代表正式同意投标、报价、签章或递交。",
    }


def latest_preparation(session: Session, project_id: str) -> PreparationRecord | None:
    return session.scalar(
        select(PreparationRecord).where(PreparationRecord.project_id == project_id)
        .order_by(PreparationRecord.created_at.desc()).limit(1)
    )


def latest_preparation_payload(session: Session, project_id: str) -> dict[str, Any] | None:
    _project(session, project_id)
    return _preparation_payload(latest_preparation(session, project_id))


def _require_business_head(role: str) -> None:
    if role != rbac.BUSINESS_HEAD:
        raise ApiError("forbidden", "仅经营负责人可以执行投标准备立项决定")


def _ensure_not_overdue(session: Session, project: Project, *, actor: str, action: str) -> None:
    from runtime.db import lifecycle_service
    # Preparation is deliberately not an identity/admission decision. It only uses
    # the real deadline safety gate; identity facts are retained for the approver.
    blocked = lifecycle_service.gate_reason(session, project, action=action, actor=actor)
    if blocked is not None and blocked[0] == "overdue":
        code, message = blocked
        _audit(session, actor=actor, action=f"preparation.{action}_denied", outcome=code,
               basis=f"project_id={project.project_id}", object_ref=project.project_id)
        session.commit()
        raise ApiError("invalid_state_transition", message, detail={"gate": action, "reason": code})


def build_prescreen_snapshot(session: Session, project_id: str) -> dict[str, Any]:
    """Build factual precheck output. No matching engine and no project mutation."""
    project = _project(session, project_id)
    identity = session.get(ProjectIdentity, project_id)
    latest = session.scalar(
        select(AdmissionResult).where(AdmissionResult.project_id == project_id)
        .order_by(AdmissionResult.created_at.desc()).limit(1)
    )
    document_count = session.scalar(
        select(func.count()).select_from(Material).where(Material.project_id == project_id)
    ) or 0

    facts: list[dict[str, Any]] = [
        {"code": "project_registered", "text": "项目已登记", "value": project.project_name},
        {"code": "identity_status", "text": "项目身份校验状态", "value": identity.identity_status if identity else "待核验"},
        {"code": "tender_document", "text": "当前招标文件引用", "value": project.tender_document_ref or None},
        {"code": "project_material_count", "text": "关联材料版本数量", "value": document_count},
        {"code": "bid_deadline", "text": "投标截止事实", "value": (
            project.bid_deadline_at.isoformat() if project.bid_deadline_at else
            (project.bid_deadline.isoformat() if project.bid_deadline else None)
        )},
    ]
    missing: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    if identity is None:
        missing.append({"code": "identity_missing", "text": "尚未完成公告与招标文件的项目身份校验"})
    elif identity.identity_status != "identity_confirmed":
        review.append({"code": identity.identity_status, "text": "项目身份尚未完成确认或存在冲突；正式匹配会被服务端阻断。"})
    if not project.tender_document_ref:
        missing.append({"code": "tender_document_missing", "text": "尚未登记当前招标文件引用"})
    if project.bid_deadline is None:
        missing.append({"code": "bid_deadline_missing", "text": "投标截止时间待补，不从规则判定时点推断"})
    if latest is None:
        review.append({"code": "formal_match_absent", "text": "尚无正式匹配结果；快速预核不能替代正式匹配。"})
    else:
        for item in latest.pending_items or []:
            missing.append({"code": "existing_missing", "requirement_id": item.get("req") or item.get("requirement_id"),
                            "text": item.get("text") or item.get("reason") or "既有匹配提示缺少资料"})
        for item in (latest.review_items or []) + (latest.blocked_items or []):
            review.append({"code": item.get("match_result") or "manual_review",
                           "requirement_id": item.get("req") or item.get("requirement_id"),
                           "text": item.get("text") or item.get("reason") or "既有匹配需要人工复核"})
    return {"facts": facts, "missing": missing, "review": review, "disclaimer": PRESCREEN_DISCLAIMER}


def create_prescreen(session: Session, *, project_id: str, actor: str) -> dict[str, Any]:
    snapshot = build_prescreen_snapshot(session, project_id)
    row = QuickPrescreenRun(
        prescreen_id=f"PS-{uuid.uuid4().hex[:12]}", project_id=project_id,
        created_by=actor, snapshot=snapshot, disclaimer=PRESCREEN_DISCLAIMER,
    )
    session.add(row)
    _audit(session, actor=actor, action="quick_prescreen.created", outcome="completed",
           basis=PRESCREEN_DISCLAIMER, object_ref=project_id)
    session.commit()
    return {"prescreen_id": row.prescreen_id, "project_id": project_id, "created_by": actor,
            "created_at": row.created_at.isoformat(), **snapshot}


def latest_prescreen(session: Session, project_id: str) -> dict[str, Any] | None:
    _project(session, project_id)
    row = session.scalar(
        select(QuickPrescreenRun).where(QuickPrescreenRun.project_id == project_id)
        .order_by(QuickPrescreenRun.created_at.desc()).limit(1)
    )
    if row is None:
        return None
    return {"prescreen_id": row.prescreen_id, "project_id": row.project_id, "created_by": row.created_by,
            "created_at": row.created_at.isoformat(), **(row.snapshot or {}), "disclaimer": row.disclaimer}


def create_preparation(session: Session, *, project_id: str, actor: str, role: str, reason: str) -> dict[str, Any]:
    _require_business_head(role)
    if not reason or not reason.strip():
        raise ApiError("invalid_request", "投标准备立项理由必填")
    project = _project(session, project_id)
    _ensure_not_overdue(session, project, actor=actor, action="create")
    current = latest_preparation(session, project_id)
    if current is not None and current.state == PREPARATION_PENDING:
        raise ApiError("invalid_state_transition", "当前已有待决定的投标准备立项，请先批准或拒绝")
    row = PreparationRecord(preparation_id=f"PR-{uuid.uuid4().hex[:12]}", project_id=project_id,
                            state=PREPARATION_PENDING, reason=reason.strip(), created_by=actor)
    session.add(row)
    _audit(session, actor=actor, action="preparation.created", outcome=PREPARATION_PENDING,
           basis=reason.strip(), object_ref=project_id)
    session.commit()
    return _preparation_payload(row) or {}


def decide_preparation(session: Session, *, project_id: str, preparation_id: str, actor: str,
                       role: str, decision: str, comment: str) -> dict[str, Any]:
    _require_business_head(role)
    if decision not in {PREPARATION_APPROVED, PREPARATION_DECLINED}:
        raise ApiError("invalid_request", "不支持的准备立项决定")
    if not comment or not comment.strip():
        raise ApiError("invalid_request", "决定意见必填")
    project = _project(session, project_id)
    _ensure_not_overdue(session, project, actor=actor, action="approve" if decision == PREPARATION_APPROVED else "decline")
    row = session.get(PreparationRecord, preparation_id)
    if row is None or row.project_id != project_id:
        raise ApiError("not_found", "投标准备立项记录不存在")
    if row.state != PREPARATION_PENDING:
        raise ApiError("invalid_state_transition", "该准备立项已完成决定，不能重复操作")
    row.state = decision
    row.decided_by = actor
    row.decision_comment = comment.strip()
    row.decided_at = _now()
    _audit(session, actor=actor, action="preparation.approved" if decision == PREPARATION_APPROVED else "preparation.declined",
           outcome=decision, basis=comment.strip(), object_ref=project_id)
    session.commit()
    return _preparation_payload(row) or {}


def _fingerprint(project_id: str, source_kind: str, source_ref: str, task_type: str) -> str:
    raw = f"{project_id}|{source_kind}|{source_ref}|{task_type}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _task_spec(item: dict[str, Any], bucket: str) -> tuple[str, str, str, str, list]:
    result = item.get("match_result")
    req_id = item.get("req") or item.get("requirement_id")
    text = item.get("text") or item.get("reason") or "匹配结果需要处置"
    if bucket == "blocked" or result in {"not_satisfied", "blocked_hard_requirement", "not_qualified"}:
        return ("termination_correction", "明确不满足：核对事实、纠正资料关联或终止该项目处置",
                rbac.BID_SPECIALIST, "resolved_by_new_match", [])
    if bucket == "missing":
        return ("evidence_supplement", "缺少企业资料或证据：补充并核验后重算",
                rbac.DATA_ADMIN, "verified_evidence_and_new_match", item.get("recommended_material_types") or [])
    owner = item.get("owner_role")
    task_type = "resource_confirmation" if owner in {"resource_manager", "business_head"} else "rule_review"
    if "身份" in text:
        task_type = "identity_confirmation"
    if "文件" in text or "条款" in text:
        task_type = "document_check" if task_type == "rule_review" else task_type
    role = rbac.BUSINESS_HEAD if task_type == "resource_confirmation" else DEFAULT_ROLE_BY_TYPE[task_type]
    return (task_type, "待人工复核：提交处理结论后重新核验/重算", role, "resolved_by_new_match", [])


def _create_or_reopen(session: Session, *, project_id: str, source_kind: str, source_ref: str,
                      source_match_run_id: str | None, item: dict[str, Any], bucket: str,
                      actor: str = "system:match") -> tuple[RemediationTask, str]:
    """Create/reopen one source-owned task and report the exact mutation."""
    task_type, desc, role, closing, required = _task_spec(item, bucket)
    fingerprint = _fingerprint(project_id, source_kind, source_ref, task_type)
    existing = session.scalar(select(RemediationTask).where(RemediationTask.source_fingerprint == fingerprint).limit(1))
    title = item.get("text") or item.get("reason") or desc
    if existing is None:
        existing = RemediationTask(
            task_id=f"RT-{uuid.uuid4().hex[:12]}", project_id=project_id, task_type=task_type,
            state=TASK_OPEN, title=str(title)[:256], description=desc, source_kind=source_kind,
            source_ref=source_ref, source_fingerprint=fingerprint, source_match_run_id=source_match_run_id,
            requirement_id=item.get("req") or item.get("requirement_id"), assignee_role=role,
            evidence_required=required, closing_condition=closing, created_by=actor,
        )
        session.add(existing)
        _audit(session, actor=actor, action="remediation_task.created", outcome=task_type,
               basis=f"source={source_kind}:{source_ref}", object_ref=project_id)
        return existing, "created"
    if existing.state in TASK_TERMINAL:
        # Same problem can recur after a prior corrected run. Preserve one auditable
        # record but explicitly reopen it; do not silently overwrite its evidence.
        existing.state = TASK_OPEN
        existing.assignee = None
        existing.closed_by = None
        existing.closed_at = None
        existing.resolution = None
        existing.source_match_run_id = source_match_run_id
        _audit(session, actor=actor, action="remediation_task.reopened", outcome=task_type,
               basis=f"source={source_kind}:{source_ref}", object_ref=project_id)
        return existing, "reopened"
    existing.source_match_run_id = source_match_run_id
    return existing, "existing"


def sync_tasks_from_admission(session: Session, *, project_id: str, match_run_id: str | None,
                              admission: AdmissionResult | None = None, commit: bool = True) -> dict[str, int]:
    """Materialize matching issues and close only issues absent in a new result.

    ``commit=False`` lets a caller decide the transaction boundary. The worker uses
    it after the immutable match/admission result has already been committed, so a
    task-orchestration fault can be recorded without rolling back factual matching.
    """
    if admission is None:
        admission = session.scalar(select(AdmissionResult).where(AdmissionResult.project_id == project_id)
                                  .order_by(AdmissionResult.created_at.desc()).limit(1))
    if admission is None:
        return {"touched": 0, "created": 0, "reopened": 0, "closed": 0}
    current: set[str] = set()
    touched = created = reopened = 0
    for bucket, rows in (("blocked", admission.blocked_items or []), ("missing", admission.pending_items or []),
                         ("review", admission.review_items or [])):
        for item in rows:
            source_ref = str(item.get("req") or item.get("requirement_id") or hashlib.sha256(
                str(item.get("text") or item).encode("utf-8")).hexdigest()[:24])
            task, mutation = _create_or_reopen(session, project_id=project_id, source_kind="admission_result",
                                               source_ref=source_ref, source_match_run_id=match_run_id,
                                               item=item, bucket=bucket)
            current.add(task.source_fingerprint)
            touched += 1
            if mutation == "created":
                created += 1
            elif mutation == "reopened":
                reopened += 1
    closed = 0
    # A task only auto-closes when a subsequent matching result has removed its exact
    # source fingerprint. It does not close merely because somebody uploaded a file.
    rows = session.scalars(select(RemediationTask).where(RemediationTask.project_id == project_id,
                                                          RemediationTask.state.in_(TASK_ACTIVE))).all()
    for task in rows:
        if task.source_kind == "admission_result" and task.source_fingerprint not in current:
            task.state = TASK_CLOSED
            task.closed_by = "system:match"
            task.closed_at = _now()
            _audit(session, actor="system:match", action="remediation_task.closed_by_recalculation",
                   outcome="source_absent_in_latest_match", basis=task.source_ref, object_ref=project_id)
            closed += 1
    if commit:
        session.commit()
    else:
        session.flush()
    return {"touched": touched, "created": created, "reopened": reopened, "closed": closed}


def list_tasks(session: Session, *, project_id: str, role: str, actor: str, state: str | None = None,
               assignee: str | None = None, mine: bool = False) -> list[dict[str, Any]]:
    _project(session, project_id)
    stmt = select(RemediationTask).where(RemediationTask.project_id == project_id)
    if state:
        stmt = stmt.where(RemediationTask.state == state)
    if assignee:
        stmt = stmt.where(RemediationTask.assignee == assignee)
    if mine:
        stmt = stmt.where((RemediationTask.assignee == actor) | (RemediationTask.assignee_role == role))
    return [_task_payload(t, viewer_role=role)
            for t in session.scalars(stmt.order_by(RemediationTask.created_at.desc())).all()]


def _get_task(session: Session, project_id: str, task_id: str) -> RemediationTask:
    row = session.get(RemediationTask, task_id)
    if row is None or row.project_id != project_id:
        raise ApiError("not_found", "处置任务不存在")
    return row


def _assert_can_work(role: str, task: RemediationTask, *, actor: str) -> None:
    # An assignee string is not an authorization grant: only the responsible role
    # (or the business head coordinating work) may perform a task transition.
    if role in {rbac.BUSINESS_HEAD, task.assignee_role}:
        return
    raise ApiError("forbidden", "当前角色不是该任务的责任角色，不能办理")


def create_task(session: Session, *, project_id: str, role: str, actor: str, task_type: str,
                title: str, description: str | None, assignee_role: str | None,
                requirement_id: str | None, due_at: datetime | None, evidence_required: list[str] | None) -> dict[str, Any]:
    _project(session, project_id)
    if task_type not in TASK_TYPES:
        raise ApiError("invalid_request", "不支持的任务类型")
    if not title or not title.strip():
        raise ApiError("invalid_request", "任务标题必填")
    intended_role = assignee_role or DEFAULT_ROLE_BY_TYPE[task_type]
    if intended_role not in rbac.ROLES:
        raise ApiError("invalid_request", "责任角色不合法")
    if role not in {rbac.BUSINESS_HEAD, intended_role}:
        raise ApiError("forbidden", "仅经营负责人或目标责任角色可以登记该任务")
    ref = requirement_id or f"manual:{uuid.uuid4().hex[:12]}"
    row = RemediationTask(task_id=f"RT-{uuid.uuid4().hex[:12]}", project_id=project_id,
                          task_type=task_type, state=TASK_OPEN, title=title.strip()[:256], description=description,
                          source_kind="manual", source_ref=ref,
                          source_fingerprint=_fingerprint(project_id, "manual", ref, task_type),
                          requirement_id=requirement_id, assignee_role=intended_role, due_at=due_at,
                          evidence_required=evidence_required or [],
                          closing_condition="resolved_by_new_match" if requirement_id else "manual_resolution",
                          created_by=actor)
    session.add(row)
    _audit(session, actor=actor, action="remediation_task.created_manual", outcome=task_type,
           basis=f"source_ref={ref}", object_ref=project_id)
    session.commit()
    return _task_payload(row, viewer_role=role)


def claim_task(session: Session, *, project_id: str, task_id: str, role: str, actor: str) -> dict[str, Any]:
    row = _get_task(session, project_id, task_id)
    _assert_can_work(role, row, actor=actor)
    if row.state not in {TASK_OPEN, TASK_IN_PROGRESS}:
        raise ApiError("invalid_state_transition", "当前任务状态不能领取")
    if row.assignee and row.assignee != actor and role != rbac.BUSINESS_HEAD:
        raise ApiError("invalid_state_transition", "任务已由其他人员领取")
    row.assignee = actor
    row.state = TASK_IN_PROGRESS
    _audit(session, actor=actor, action="remediation_task.claimed", outcome=TASK_IN_PROGRESS,
           object_ref=project_id, basis=f"task_id={task_id}")
    session.commit()
    return _task_payload(row, viewer_role=role)


def assign_task(session: Session, *, project_id: str, task_id: str, role: str, actor: str,
                assignee: str, assignee_role: str | None = None) -> dict[str, Any]:
    row = _get_task(session, project_id, task_id)
    if not assignee or not assignee.strip():
        raise ApiError("invalid_request", "责任人必填")
    _assert_can_work(role, row, actor=actor)
    if row.state in TASK_TERMINAL:
        raise ApiError("invalid_state_transition", "已关闭任务不能转派")
    if assignee_role and assignee_role != row.assignee_role:
        if role != rbac.BUSINESS_HEAD or assignee_role not in rbac.ROLES:
            raise ApiError("forbidden", "仅经营负责人可变更责任角色")
        row.assignee_role = assignee_role
    row.assignee = assignee.strip()
    if row.state == TASK_OPEN:
        row.state = TASK_IN_PROGRESS
    _audit(session, actor=actor, action="remediation_task.assigned", outcome=row.assignee_role,
           object_ref=project_id, basis=f"task_id={task_id};assignee={row.assignee}")
    session.commit()
    return _task_payload(row, viewer_role=role)


def submit_evidence(session: Session, *, project_id: str, task_id: str, role: str, actor: str,
                    evidence_refs: list[str], comment: str | None = None) -> dict[str, Any]:
    row = _get_task(session, project_id, task_id)
    _assert_can_work(role, row, actor=actor)
    if row.task_type == "termination_correction":
        raise ApiError("invalid_state_transition", "明确不满足项不能通过补证任务关闭；请纠正事实或按终止处置办理")
    if row.task_type != "evidence_supplement":
        raise ApiError("invalid_state_transition", "仅资料补证任务可以提交证据")
    if row.state not in {TASK_OPEN, TASK_IN_PROGRESS, TASK_EVIDENCE_SUBMITTED}:
        raise ApiError("invalid_state_transition", "当前任务状态不能提交证据")
    refs = [x.strip() for x in evidence_refs if isinstance(x, str) and x.strip()]
    if not refs:
        raise ApiError("invalid_request", "至少提交一个可追溯证据引用")
    row.assignee = row.assignee or actor
    row.evidence_refs = sorted(set((row.evidence_refs or []) + refs))
    row.resolution = comment.strip() if comment and comment.strip() else row.resolution
    row.state = TASK_EVIDENCE_SUBMITTED
    _audit(session, actor=actor, action="remediation_task.evidence_submitted", outcome=TASK_EVIDENCE_SUBMITTED,
           object_ref=project_id, basis=f"task_id={task_id};evidence_count={len(refs)}")
    session.commit()
    return _task_payload(row, viewer_role=role)


def resolve_task(session: Session, *, project_id: str, task_id: str, role: str, actor: str,
                 resolution: str) -> dict[str, Any]:
    row = _get_task(session, project_id, task_id)
    _assert_can_work(role, row, actor=actor)
    if row.task_type == "evidence_supplement":
        raise ApiError("invalid_state_transition", "资料补证任务请提交证据并等待核验/重算，不可手工宣称已解决")
    if row.state in TASK_TERMINAL:
        raise ApiError("invalid_state_transition", "已关闭任务不能再提交处置结论")
    if not resolution or not resolution.strip():
        raise ApiError("invalid_request", "处理结论必填，并将在重算前保留为待核验")
    row.assignee = row.assignee or actor
    row.resolution = resolution.strip()
    row.state = TASK_RESOLVED_PENDING_RECALCULATION
    _audit(session, actor=actor, action="remediation_task.resolved_pending_recalculation",
           outcome=TASK_RESOLVED_PENDING_RECALCULATION, object_ref=project_id, basis=f"task_id={task_id}")
    session.commit()
    return _task_payload(row, viewer_role=role)
