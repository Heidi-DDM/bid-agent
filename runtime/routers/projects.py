# F020：项目/结果/矩阵/队列/准入 API（只读，不触发新匹配）
# GET  /api/v1/projects
# GET  /api/v1/projects/{project_id}
# GET  /api/v1/projects/{project_id}/requirements
# GET  /api/v1/projects/{project_id}/match-runs/latest
# GET  /api/v1/projects/{project_id}/match-runs/{run_id}
# GET  /api/v1/projects/{project_id}/matrix
# GET  /api/v1/projects/{project_id}/admission
# GET  /api/v1/projects/{project_id}/queues
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from runtime.routers.deps import get_db, get_role, get_request_id, require_role
from runtime.core.errors import ApiError
from runtime.db import api_service
from runtime.db.models import MatchRun

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


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
