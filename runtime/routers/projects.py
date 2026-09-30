# F020：项目/结果/矩阵/队列/准入 API（只读，不触发新匹配）
# GET  /api/v1/projects
# GET  /api/v1/projects/{project_id}
# GET  /api/v1/projects/{project_id}/requirements
# GET  /api/v1/projects/{project_id}/match-runs/latest
# GET  /api/v1/projects/{project_id}/match-runs/{run_id}
# GET  /api/v1/projects/{project_id}/matrix
# GET  /api/v1/projects/{project_id}/admission
# GET  /api/v1/projects/{project_id}/queues
# ADR-004 Iteration 0（F020 v1.20）：
# GET  /api/v1/projects/{project_id}/readiness         阶段/截止/过期/身份校验/最新结果新鲜度/阻断原因
# POST /api/v1/projects/{project_id}/identity-check    执行项目身份校验（P0-02；可显式传字段覆盖自动推导）
# POST /api/v1/projects/{project_id}/identity-confirm  人工确认同一项目（仅 warning → confirmed）
# POST /api/v1/projects/{project_id}/bid-deadline      登记/纠正投标截止（延期公告；P0-03 数据来源）
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from runtime.routers.deps import get_actor, get_db, get_role, get_request_id, require_role
from runtime.core import rbac
from runtime.core.errors import ApiError
from runtime.db import api_service, identity_service, lifecycle_service
from runtime.db.models import MatchRun

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


class IdentityCheckBody(BaseModel):
    """身份校验显式输入（人工核对录入；缺省全部自动从公告候选/招标文件主卡推导）。"""

    expected: dict[str, str | float | int | None] | None = Field(
        default=None, description="公告侧字段覆盖：project_name/purchaser/location/project_type/"
                                  "announcement_no/tender_no/procurement_no/lot_id/budget_amount/bid_deadline")
    actual: dict[str, str | float | int | None] | None = Field(
        default=None, description="招标文件侧字段覆盖（同上键）")


class IdentityConfirmBody(BaseModel):
    note: str = Field(..., description="人工确认依据（必填）")


class BidDeadlineBody(BaseModel):
    bid_deadline: str | None = Field(
        default=None, description="投标截止日期 YYYY-MM-DD；仅有日期时不虚构时刻"
    )
    bid_deadline_at: str | None = Field(
        default=None, description="精确截止时点：必须为含时区 ISO 8601，如 2026-09-16T09:00:00+08:00"
    )
    source: str = Field(..., description="来源依据（如：延期公告编号/招标文件页码）")


def _require_identity_writer(role: str, session: Session, actor: str, project_id: str) -> None:
    """身份校验/截止登记：投标专员（tender_document:write）或经营负责人（match:write）。"""
    if rbac.has_permission(role, "tender_document", "write") or rbac.has_permission(role, "match", "write"):
        return
    require_role(role, "tender_document", "write", session=session, actor=actor, object_ref=project_id)


@router.get("")
def list_projects(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """结构化推送列表（F020 §2.2.1）。"""
    require_role(role, "announcement", "read", session=session, actor=role)
    items, total = api_service.list_projects(session, limit=limit, offset=offset)
    return {"request_id": request_id, "items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{project_id}")
def get_project(
    project_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """项目主卡 + 原文与解析状态表（F020 §2.2.2）。"""
    require_role(role, "announcement", "read", session=session, actor=role, object_ref=project_id)
    detail = api_service.get_project_detail(session, project_id)
    detail["request_id"] = request_id
    return detail


@router.get("/{project_id}/requirements")
def list_requirements(
    project_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """项目要求（F020 §2.2.3，对齐 PROTOTYPE.requirements）。"""
    require_role(role, "result", "read", session=session, actor=role, object_ref=project_id)
    items = api_service.list_requirements(session, project_id)
    return {"request_id": request_id, "items": items}


@router.get("/{project_id}/match-runs/latest")
def match_runs_latest(
    project_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """最近匹配运行（只读，不创建 run；无 run 返回 {run_id: null}，F020 §2.2.3）。"""
    require_role(role, "match", "read", session=session, actor=role, object_ref=project_id)
    data = api_service.match_runs_latest(session, project_id)
    data["request_id"] = request_id
    return data


@router.get("/{project_id}/match-runs/compare")
def match_runs_compare(
    project_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
    base_run_id: str | None = None,
    target_run_id: str | None = None,
) -> dict:
    """R023-4 结果版本对比：base（默认次新）vs target（默认最新）逐条 diff。"""
    require_role(role, "match", "read", session=session, actor=role, object_ref=project_id)
    data = api_service.match_runs_compare(
        session, project_id, base_run_id=base_run_id, target_run_id=target_run_id)
    data["request_id"] = request_id
    return data


@router.get("/{project_id}/match-runs/{run_id}")
def match_run_detail(
    project_id: str,
    run_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """指定匹配运行详情（只读，不创建 run）。"""
    require_role(role, "match", "read", session=session, actor=role, object_ref=project_id)
    run = session.get(MatchRun, run_id)
    if run is None or run.project_id != project_id:
        raise ApiError("not_found", f"匹配运行不存在: {run_id}")
    return {
        "request_id": request_id,
        "run_id": run.run_id,
        "project_id": run.project_id,
        "rule_set_id": run.rule_set_id,
        "as_of": run.as_of,
        "mode": run.mode,
        "coverage": run.coverage,
        "status": run.status,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        # 方案 §3.6：RAG 快照随结果可回放（检索运行/索引版本/候选引用/结构化核验/证据快照）
        "retrieval_run_id": run.retrieval_run_id,
        "index_version": run.index_version,
        "candidate_chunk_ids": run.candidate_chunk_ids or [],
        "structured_verification": run.structured_verification,
        "evidence_snapshot_hash": run.evidence_snapshot_hash,
    }


@router.get("/{project_id}/matrix")
def matrix(
    project_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """矩阵页合并视图（requirements + match_items，只读，F020 §2.2.4）。"""
    require_role(role, "result", "read", session=session, actor=role, object_ref=project_id)
    view = api_service.matrix_view(session, project_id)
    view["request_id"] = request_id
    return view


@router.get("/{project_id}/admission")
def admission(
    project_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """准入摘要（F020 §2.2.4，对齐 PROTOTYPE.admission）。"""
    require_role(role, "result", "read", session=session, actor=role, object_ref=project_id)
    summary = api_service.admission_summary(session, project_id)
    summary["request_id"] = request_id
    return summary


@router.get("/{project_id}/queues")
def queues(
    project_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """三类处置队列（F020 §2.2.4，对齐 queue.html）。"""
    require_role(role, "result", "read", session=session, actor=role, object_ref=project_id)
    summary = api_service.queues_summary(session, project_id)
    summary["request_id"] = request_id
    return summary


# ---------- ADR-004 Iteration 0：就绪摘要 / 身份校验 / 截止登记 ----------

@router.get("/{project_id}/readiness")
def readiness(
    project_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """项目就绪摘要（优化方案 §10.2/§11.2）：阶段、截止倒计时、过期、身份校验、最新结果新鲜度、阻断原因。

    只陈述事实与阻断原因；过期迁移（machine 状态 → overdue）在此如实落库并审计。"""
    require_role(role, "result", "read", session=session, actor=role, object_ref=project_id)
    data = lifecycle_service.project_readiness(session, project_id)
    data["request_id"] = request_id
    return data


@router.post("/{project_id}/identity-check")
def identity_check(
    project_id: str,
    body: IdentityCheckBody | None = None,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """执行项目身份校验（P0-02）：公告侧 vs 招标文件侧，返回三态与冲突明细并落库审计。

    `identity_conflict` 由服务端阻断正式匹配与审批；显式 expected/actual 覆盖自动推导（人工核对录入）。"""
    _require_identity_writer(role, session, actor, project_id)
    body = body or IdentityCheckBody()
    data = identity_service.evaluate_project_identity(
        session, project_id=project_id, actor=actor,
        expected=body.expected, actual=body.actual,
    )
    return {"request_id": request_id, "project_id": project_id, "identity": data}


@router.post("/{project_id}/identity-confirm")
def identity_confirm(
    project_id: str,
    body: IdentityConfirmBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """人工确认为同一项目（仅 identity_warning 可确认；conflict 须纠正数据后重新校验）。"""
    _require_identity_writer(role, session, actor, project_id)
    data = identity_service.confirm_project_identity(
        session, project_id=project_id, actor=actor, note=body.note,
    )
    return {"request_id": request_id, "project_id": project_id, "identity": data}


@router.post("/{project_id}/bid-deadline")
def set_bid_deadline(
    project_id: str,
    body: BidDeadlineBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """登记/纠正投标截止（延期公告等，P0-03 数据来源）；过期项目更新为未过期后恢复 matching。"""
    _require_identity_writer(role, session, actor, project_id)
    if not body.source or not body.source.strip():
        raise ApiError("invalid_request", "source 必填（延期公告编号/招标文件页码等依据）")
    value: date | None = None
    if body.bid_deadline:
        try:
            value = date.fromisoformat(body.bid_deadline)
        except ValueError:
            raise ApiError("invalid_request", "bid_deadline 必须为 YYYY-MM-DD")
    value_at: datetime | None = None
    if body.bid_deadline_at:
        try:
            value_at = datetime.fromisoformat(body.bid_deadline_at.replace("Z", "+00:00"))
        except ValueError:
            raise ApiError("invalid_request", "bid_deadline_at 必须为含时区 ISO 8601")
        if value_at.tzinfo is None:
            raise ApiError("invalid_request", "bid_deadline_at 必须包含时区，不得按服务器时区猜测")
    project = api_service.get_project_or_404(session, project_id)
    lifecycle_service.set_bid_deadline(
        session, project, value, value_at=value_at, actor=actor, source=body.source.strip()
    )
    session.commit()
    return {
        "request_id": request_id,
        "project_id": project_id,
        "bid_deadline": project.bid_deadline.isoformat() if project.bid_deadline else None,
        "bid_deadline_at": lifecycle_service.deadline_at_cn(project.bid_deadline_at).isoformat()
                           if project.bid_deadline_at else None,
        "deadline_precision": "datetime" if project.bid_deadline_at else
                              ("date" if project.bid_deadline else "missing"),
        "overdue": lifecycle_service.is_overdue(project),
        "state": project.admission_status,
    }
