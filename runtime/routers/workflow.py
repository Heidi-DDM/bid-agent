"""ADR-004 Iteration 1: early prescreen, preparation decisions and task center."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from runtime.core import rbac
from runtime.core.errors import ApiError
from runtime.db import workflow_service
from runtime.routers.deps import get_actor, get_db, get_request_id, get_role, require_role

router = APIRouter(prefix="/api/v1/projects", tags=["workflow"])


class PreparationCreateBody(BaseModel):
    reason: str = Field(..., min_length=1, description="投入投标准备工作的理由（不代表正式投标决定）")


class PreparationDecisionBody(BaseModel):
    comment: str = Field(..., min_length=1, description="负责人决定意见（必填）")


class TaskCreateBody(BaseModel):
    task_type: str = Field(..., description="document_check/evidence_supplement/rule_review/resource_confirmation/identity_confirmation/termination_correction/stale_review")
    title: str = Field(..., min_length=1, max_length=256)
    description: str | None = None
    assignee_role: str | None = None
    requirement_id: str | None = None
    due_at: str | None = Field(default=None, description="含时区 ISO 8601；可为空=待补")
    evidence_required: list[str] = Field(default_factory=list)


class TaskAssignBody(BaseModel):
    assignee: str = Field(..., min_length=1)
    assignee_role: str | None = None


class TaskEvidenceBody(BaseModel):
    evidence_refs: list[str] = Field(..., min_length=1)
    comment: str | None = None


class TaskResolveBody(BaseModel):
    resolution: str = Field(..., min_length=1)


def _parse_due_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError("invalid_request", "due_at 必须为含时区 ISO 8601，或留空待补")
    if parsed.tzinfo is None:
        raise ApiError("invalid_request", "due_at 必须包含时区，不得按服务器时区猜测")
    return parsed


def _task_access(role: str, session: Session, actor: str, project_id: str, action: str = "read") -> None:
    require_role(role, rbac.RES_TASK, action, session=session, actor=actor, object_ref=project_id)


@router.post("/{project_id}/quick-prescreen")
def quick_prescreen(project_id: str, request_id: str = Depends(get_request_id),
                    role: str = Depends(get_role), actor: str = Depends(get_actor),
                    session: Session = Depends(get_db)) -> dict:
    # Prescreen remains a facts-only early activity. Data admin/legal cannot turn
    # private-data maintenance or legal review into a project preparation decision.
    if role not in {rbac.BID_SPECIALIST, rbac.BUSINESS_HEAD}:
        _task_access(role, session, actor, project_id, "write")
        raise ApiError("forbidden", "仅投标专员或经营负责人可以发起快速预核")
    data = workflow_service.create_prescreen(session, project_id=project_id, actor=actor)
    data["request_id"] = request_id
    return data


@router.get("/{project_id}/quick-prescreens/latest")
def quick_prescreen_latest(project_id: str, request_id: str = Depends(get_request_id),
                           role: str = Depends(get_role), actor: str = Depends(get_actor),
                           session: Session = Depends(get_db)) -> dict:
    if role not in {rbac.BID_SPECIALIST, rbac.BUSINESS_HEAD}:
        _task_access(role, session, actor, project_id, "read")
        raise ApiError("forbidden", "当前角色无快速预核查看权限")
    return {"request_id": request_id, "item": workflow_service.latest_prescreen(session, project_id)}


@router.post("/{project_id}/preparations")
def create_preparation(project_id: str, body: PreparationCreateBody,
                       request_id: str = Depends(get_request_id), role: str = Depends(get_role),
                       actor: str = Depends(get_actor), session: Session = Depends(get_db)) -> dict:
    require_role(role, rbac.RES_PREPARATION, "write", session=session, actor=actor, object_ref=project_id)
    data = workflow_service.create_preparation(session, project_id=project_id, actor=actor, role=role, reason=body.reason)
    data["request_id"] = request_id
    return data


@router.get("/{project_id}/preparations/latest")
def preparation_latest(project_id: str, request_id: str = Depends(get_request_id),
                       role: str = Depends(get_role), actor: str = Depends(get_actor),
                       session: Session = Depends(get_db)) -> dict:
    # Preparation reasons/decisions are project-flow coordination data; they are
    # visible to the bid specialist and business head, not private-data/legal roles.
    if role not in {rbac.BID_SPECIALIST, rbac.BUSINESS_HEAD}:
        raise ApiError("forbidden", "当前角色无投标准备立项查看权限")
    data = workflow_service.latest_preparation_payload(session, project_id)
    return {"request_id": request_id, "item": data}


@router.post("/{project_id}/preparations/{preparation_id}/approve")
def approve_preparation(project_id: str, preparation_id: str, body: PreparationDecisionBody,
                        request_id: str = Depends(get_request_id), role: str = Depends(get_role),
                        actor: str = Depends(get_actor), session: Session = Depends(get_db)) -> dict:
    require_role(role, rbac.RES_PREPARATION, "approve", session=session, actor=actor, object_ref=project_id)
    data = workflow_service.decide_preparation(session, project_id=project_id, preparation_id=preparation_id,
                                               actor=actor, role=role,
                                               decision=workflow_service.PREPARATION_APPROVED, comment=body.comment)
    data["request_id"] = request_id
    return data


@router.post("/{project_id}/preparations/{preparation_id}/decline")
def decline_preparation(project_id: str, preparation_id: str, body: PreparationDecisionBody,
                        request_id: str = Depends(get_request_id), role: str = Depends(get_role),
                        actor: str = Depends(get_actor), session: Session = Depends(get_db)) -> dict:
    require_role(role, rbac.RES_PREPARATION, "approve", session=session, actor=actor, object_ref=project_id)
    data = workflow_service.decide_preparation(session, project_id=project_id, preparation_id=preparation_id,
                                               actor=actor, role=role,
                                               decision=workflow_service.PREPARATION_DECLINED, comment=body.comment)
    data["request_id"] = request_id
    return data


@router.get("/{project_id}/tasks")
def tasks(project_id: str, state: str | None = Query(default=None), assignee: str | None = Query(default=None),
          mine: bool = Query(default=False), request_id: str = Depends(get_request_id),
          role: str = Depends(get_role), actor: str = Depends(get_actor), session: Session = Depends(get_db)) -> dict:
    _task_access(role, session, actor, project_id, "read")
    return {"request_id": request_id, "items": workflow_service.list_tasks(
        session, project_id=project_id, role=role, actor=actor, state=state, assignee=assignee, mine=mine)}


@router.post("/{project_id}/tasks")
def create_task(project_id: str, body: TaskCreateBody, request_id: str = Depends(get_request_id),
                role: str = Depends(get_role), actor: str = Depends(get_actor), session: Session = Depends(get_db)) -> dict:
    _task_access(role, session, actor, project_id, "write")
    data = workflow_service.create_task(session, project_id=project_id, role=role, actor=actor,
                                        task_type=body.task_type, title=body.title, description=body.description,
                                        assignee_role=body.assignee_role, requirement_id=body.requirement_id,
                                        due_at=_parse_due_at(body.due_at), evidence_required=body.evidence_required)
    data["request_id"] = request_id
    return data


@router.post("/{project_id}/tasks/{task_id}/claim")
def claim_task(project_id: str, task_id: str, request_id: str = Depends(get_request_id),
               role: str = Depends(get_role), actor: str = Depends(get_actor), session: Session = Depends(get_db)) -> dict:
    _task_access(role, session, actor, project_id, "write")
    data = workflow_service.claim_task(session, project_id=project_id, task_id=task_id, role=role, actor=actor)
    data["request_id"] = request_id
    return data


@router.post("/{project_id}/tasks/{task_id}/assign")
def assign_task(project_id: str, task_id: str, body: TaskAssignBody,
                request_id: str = Depends(get_request_id), role: str = Depends(get_role),
                actor: str = Depends(get_actor), session: Session = Depends(get_db)) -> dict:
    _task_access(role, session, actor, project_id, "write")
    data = workflow_service.assign_task(session, project_id=project_id, task_id=task_id, role=role, actor=actor,
                                        assignee=body.assignee, assignee_role=body.assignee_role)
    data["request_id"] = request_id
    return data


@router.post("/{project_id}/tasks/{task_id}/evidence")
def task_evidence(project_id: str, task_id: str, body: TaskEvidenceBody,
                  request_id: str = Depends(get_request_id), role: str = Depends(get_role),
                  actor: str = Depends(get_actor), session: Session = Depends(get_db)) -> dict:
    _task_access(role, session, actor, project_id, "write")
    data = workflow_service.submit_evidence(session, project_id=project_id, task_id=task_id, role=role,
                                            actor=actor, evidence_refs=body.evidence_refs, comment=body.comment)
    data["request_id"] = request_id
    return data


@router.post("/{project_id}/tasks/{task_id}/resolve")
def task_resolve(project_id: str, task_id: str, body: TaskResolveBody,
                 request_id: str = Depends(get_request_id), role: str = Depends(get_role),
                 actor: str = Depends(get_actor), session: Session = Depends(get_db)) -> dict:
    _task_access(role, session, actor, project_id, "write")
    data = workflow_service.resolve_task(session, project_id=project_id, task_id=task_id, role=role,
                                         actor=actor, resolution=body.resolution)
    data["request_id"] = request_id
    return data
