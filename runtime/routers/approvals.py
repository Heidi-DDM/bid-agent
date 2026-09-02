# F020：审批/豁免/审计 API（仅经营负责人，F020 §2.2.6）
# GET  /api/v1/approvals/pending
# POST /api/v1/projects/{project_id}/approval/create
# POST /api/v1/projects/{project_id}/approval/approve
# POST /api/v1/projects/{project_id}/approval/reject
# POST /api/v1/projects/{project_id}/waivers
# GET  /api/v1/projects/{project_id}/audit
from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from runtime.routers.deps import get_actor, get_db, get_role, get_request_id, require_role
from runtime.core.errors import ApiError
from runtime.db import api_service

router = APIRouter(prefix="/api/v1", tags=["approvals"])


class ApproveBody(BaseModel):
    approver: str = Field(..., description="审批人")
    basis: str | None = None
    comment: str | None = None


class RejectBody(BaseModel):
    approver: str = Field(..., description="审批人")
    comment: str = Field(..., description="驳回意见（必填）")
    basis: str | None = None


class WaiverBody(BaseModel):
    authorizer: str = Field(..., description="授权人")
    reason: str = Field(..., description="豁免原因")
    evidence_refs: list[str] = Field(..., description="证据引用（必填）")
    valid_until: str = Field(..., description="有效期（YYYY-MM-DD）")
    covered_items: list[str] = Field(..., description="覆盖项（必填）")


@router.get("/approvals/pending")
def pending_approvals(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """待审批列表，仅经营负责人；其余角色 403 forbidden（F020 §2.2.6）。"""
    require_role(role, "approval", "read", session=session, actor=role)
    items = api_service.list_pending_approvals(session, role)
    return {"request_id": request_id, "items": items}


@router.post("/projects/{project_id}/approval/create")
def create_approval(
    project_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """创建审批：仅最新结果满分可创建（F020 §2.2.6）。"""
    require_role(role, "approval", "write", session=session, actor=actor, object_ref=project_id)
    api_service.expire_waivers(session, project_id)
    data = api_service.create_approval(
        session, project_id=project_id, role=role, actor=actor
    )
    data["request_id"] = request_id
    return data


@router.post("/projects/{project_id}/approval/approve")
def approve(
    project_id: str,
    body: ApproveBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """审批通过（终态不可重复决策）。审计 actor=登录者（R024 §5.1 越权审计）。"""
    require_role(role, "approval", "approve", session=session, actor=actor, object_ref=project_id)
    data = api_service.decide_approval(
        session, project_id=project_id, role=role, actor=body.approver or actor,
        decision="approved", comment=body.comment, basis=body.basis,
    )
    data["request_id"] = request_id
    return data


@router.post("/projects/{project_id}/approval/reject")
def reject(
    project_id: str,
    body: RejectBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """驳回（comment 必填，F020 §2.2.6）。审计 actor=登录者。"""
    require_role(role, "approval", "approve", session=session, actor=actor, object_ref=project_id)
    data = api_service.decide_approval(
        session, project_id=project_id, role=role, actor=body.approver or actor,
        decision="rejected", comment=body.comment, basis=body.basis,
    )
    data["request_id"] = request_id
    return data


@router.post("/projects/{project_id}/waivers")
def add_waiver(
    project_id: str,
    body: WaiverBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """豁免登记（四字段必填；过期自动失效回阻断，F020 §2.2.6）。审计 actor=登录者。"""
    require_role(role, "approval", "approve", session=session, actor=actor, object_ref=project_id)
    data = api_service.add_waiver(
        session, project_id=project_id, role=role, actor=body.authorizer or actor,
        reason=body.reason, evidence_refs=body.evidence_refs,
        valid_until=body.valid_until, covered_items=body.covered_items,
    )
    data["request_id"] = request_id
    return data


@router.get("/projects/{project_id}/audit")
def audit_timeline(
    project_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """审计时间线（仅经营负责人，F020 §2.2.6）。"""
    require_role(role, "approval", "read", session=session, actor=role, object_ref=project_id)
    items = api_service.list_audit(session, project_id, role)
    return {"request_id": request_id, "items": items}
