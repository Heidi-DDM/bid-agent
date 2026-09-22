"""F026：搜索简报、统一待选池、经营策略预筛规则与进度面板 API。"""
from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from fastapi import APIRouter, Depends, Query

from runtime.core import rbac
from runtime.core.errors import ApiError
from runtime.db import api_service
from runtime.db.models import AnnouncementCandidate, Project, RemediationTask, SelectionPoolItem
from runtime.discovery import service
from runtime.routers.deps import get_actor, get_db, get_request_id, get_role, require_role

router = APIRouter(prefix="/api/v1", tags=["discovery"])


class DismissBody(BaseModel):
    reason: str | None = Field(None, max_length=500)


class DismissBatchBody(BaseModel):
    candidate_ids: list[str] = Field(..., min_length=1, max_length=200)
    reason: str | None = Field(None, max_length=500)


class RuleConfigBody(BaseModel):
    industry_keywords: list[str] = Field(default_factory=list, max_length=200)
    exclude_keywords: list[str] = Field(default_factory=list, max_length=200)
    business_profile: str = Field("", max_length=800, description="企业业务画像：AI 预筛相关性判定基准（ADR-006 v2）")


class RulePreviewBody(RuleConfigBody):
    limit: int = Field(200, ge=10, le=500)


def _pool_access(role: str, session: Session, actor: str, *, write: bool = False, object_ref: str | None = None) -> None:
    require_role(role, rbac.RES_ANNOUNCEMENT, "write" if write else "read", session=session, actor=actor, object_ref=object_ref)


@router.get("/discovery/pool")
def list_pool(
    status: str | None = Query(default=None), limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0), sort: str = Query(default="publish_date"),
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role), actor: str = Depends(get_actor), session: Session = Depends(get_db),
) -> dict:
    """待选池简报列表。sort：publish_date（默认，发布时间优先、缺日期沉底）/ last_seen。"""
    _pool_access(role, session, actor)
    if sort not in {"publish_date", "last_seen"}:
        raise ApiError("invalid_request", "sort 仅支持 publish_date 或 last_seen")
    query = (
        select(SelectionPoolItem, AnnouncementCandidate)
        .outerjoin(AnnouncementCandidate, SelectionPoolItem.candidate_id == AnnouncementCandidate.candidate_id)
    )
    count_query = select(func.count()).select_from(SelectionPoolItem)
    if status:
        statuses = [x.strip() for x in status.split(",") if x.strip()]
        query = query.where(SelectionPoolItem.pool_status.in_(statuses))
        count_query = count_query.where(SelectionPoolItem.pool_status.in_(statuses))
    if sort == "publish_date":
        # 发布时间优先（新→旧）；无发布日期的沉底，再按最近发现时间稳定排序。
        query = query.order_by(
            AnnouncementCandidate.publish_date.desc().nullslast(),
            SelectionPoolItem.last_seen_at.desc(),
        )
    else:
        query = query.order_by(SelectionPoolItem.last_seen_at.desc())
    rows = session.execute(query.offset(offset).limit(limit)).all()
    return {"request_id": request_id,
            "items": [service.pool_payload(session, item, candidate=candidate) for item, candidate in rows],
            "total": session.scalar(count_query) or 0, "limit": limit, "offset": offset, "sort": sort}


@router.get("/discovery/briefing")
def search_briefing(
    job_id: str | None = Query(default=None),
    request_id: str = Depends(get_request_id), role: str = Depends(get_role),
    actor: str = Depends(get_actor), session: Session = Depends(get_db),
) -> dict:
    """搜索增量简报：最近一次（或指定 job）搜索的入池状态分布，只统计事实。"""
    _pool_access(role, session, actor)
    briefing = service.search_briefing(session, job_id=job_id)
    if briefing is None:
        raise ApiError("not_found", f"搜索简报不存在（job_id={job_id or '最近'}）")
    return {"request_id": request_id, "briefing": briefing}


@router.post("/discovery/candidates/{candidate_id}/select")
def select_deep_dive(
    candidate_id: str, request_id: str = Depends(get_request_id), role: str = Depends(get_role),
    actor: str = Depends(get_actor), session: Session = Depends(get_db),
) -> dict:
    _pool_access(role, session, actor, write=True, object_ref=candidate_id)
    try:
        item = service.set_pool_status(session, candidate_id=candidate_id, status=service.POOL_DEEP_DIVE, actor=actor)
    except KeyError:
        raise ApiError("not_found", f"待选池候选不存在: {candidate_id}")
    except ValueError as exc:
        raise ApiError("invalid_state_transition", str(exc))
    api_service.audit(session, actor=actor, action="discovery.pool.select_deep_dive",
                      basis="人工从搜索简报选择深入", outcome="deep_dive", object_ref=candidate_id)
    session.commit()
    return {"request_id": request_id, "item": service.pool_payload(session, item)}


@router.post("/discovery/candidates/{candidate_id}/dismiss")
def dismiss_candidate(
    candidate_id: str, body: DismissBody, request_id: str = Depends(get_request_id), role: str = Depends(get_role),
    actor: str = Depends(get_actor), session: Session = Depends(get_db),
) -> dict:
    _pool_access(role, session, actor, write=True, object_ref=candidate_id)
    try:
        item = service.set_pool_status(session, candidate_id=candidate_id, status=service.POOL_DISMISSED,
                                       actor=actor, reason=body.reason)
    except KeyError:
        raise ApiError("not_found", f"待选池候选不存在: {candidate_id}")
    except ValueError as exc:
        raise ApiError("invalid_state_transition", str(exc))
    api_service.audit(session, actor=actor, action="discovery.pool.dismiss",
                      basis=f"reason={body.reason or '未填写'}", outcome="dismissed", object_ref=candidate_id)
    session.commit()
    return {"request_id": request_id, "item": service.pool_payload(session, item)}


@router.post("/discovery/candidates/dismiss-batch")
def dismiss_candidates_batch(
    body: DismissBatchBody, request_id: str = Depends(get_request_id), role: str = Depends(get_role),
    actor: str = Depends(get_actor), session: Session = Depends(get_db),
) -> dict:
    """批量软删除：逐条走单条状态机（已深入的仍不可删），统一原因只记一次审计汇总。"""
    _pool_access(role, session, actor, write=True, object_ref=",".join(body.candidate_ids[:5]))
    result = service.dismiss_batch(session, candidate_ids=body.candidate_ids, actor=actor, reason=body.reason)
    api_service.audit(session, actor=actor, action="discovery.pool.dismiss_batch",
                      basis=f"reason={body.reason or '未填写'} · 共提交 {len(body.candidate_ids)} 条",
                      outcome=f"dismissed={len(result['dismissed'])} failed={len(result['failed'])}",
                      object_ref=None)
    session.commit()
    return {"request_id": request_id, **result}


@router.post("/discovery/candidates/{candidate_id}/restore")
def restore_candidate(
    candidate_id: str, request_id: str = Depends(get_request_id), role: str = Depends(get_role),
    actor: str = Depends(get_actor), session: Session = Depends(get_db),
) -> dict:
    """ADR-006：恢复被 AI 预筛排除的候选（仅 excluded_by=llm；人工删除不可由此恢复）。"""
    _pool_access(role, session, actor, write=True, object_ref=candidate_id)
    try:
        item = service.restore_candidate(session, candidate_id=candidate_id, actor=actor)
    except KeyError:
        raise ApiError("not_found", f"待选池候选不存在: {candidate_id}")
    except ValueError as exc:
        raise ApiError("invalid_state_transition", str(exc))
    api_service.audit(session, actor=actor, action="discovery.pool.restore",
                      basis="人工恢复 AI 预筛排除的候选", outcome="needs_manual_review",
                      object_ref=candidate_id)
    session.commit()
    return {"request_id": request_id, "item": service.pool_payload(session, item)}


@router.get("/discovery/rules")
def get_rules(
    request_id: str = Depends(get_request_id), role: str = Depends(get_role), actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    _pool_access(role, session, actor)
    version, config = service.active_rule(session)
    return {"request_id": request_id, "version": version, "config": config}


@router.put("/discovery/rules")
def put_rules(
    body: RuleConfigBody, request_id: str = Depends(get_request_id), role: str = Depends(get_role),
    actor: str = Depends(get_actor), session: Session = Depends(get_db),
) -> dict:
    require_role(role, rbac.RES_APPROVAL, "write", session=session, actor=actor)
    row = service.update_rules(session, config=body.model_dump(), actor=actor)
    api_service.audit(session, actor=actor, action="discovery.rules.updated", basis="经营策略预筛规则版本化更新",
                      outcome=row.version, object_ref=row.rule_profile_id)
    session.commit()
    return {"request_id": request_id, "version": row.version, "config": row.config}


@router.post("/discovery/rules/preview")
def preview_rules(
    body: RulePreviewBody, request_id: str = Depends(get_request_id), role: str = Depends(get_role),
    actor: str = Depends(get_actor), session: Session = Depends(get_db),
) -> dict:
    """规则试跑：对最近 N 条候选回放预筛，返回分布与样例；不落库、不切换生效版本。"""
    require_role(role, rbac.RES_APPROVAL, "write", session=session, actor=actor)
    result = service.preview_rules(session, config=body.model_dump(exclude={"limit"}), limit=body.limit)
    api_service.audit(session, actor=actor, action="discovery.rules.previewed",
                      basis=f"sampled={result['total_sampled']} limit={body.limit}",
                      outcome="preview", object_ref=None)
    session.commit()
    return {"request_id": request_id, **result}


@router.get("/manager/dashboard")
def manager_dashboard(
    request_id: str = Depends(get_request_id), role: str = Depends(get_role), actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    require_role(role, rbac.RES_APPROVAL, "read", session=session, actor=actor)
    project_states = dict(session.execute(
        select(Project.admission_status, func.count()).group_by(Project.admission_status)
    ).all())
    pool_states = dict(session.execute(
        select(SelectionPoolItem.pool_status, func.count()).group_by(SelectionPoolItem.pool_status)
    ).all())
    task_states = dict(session.execute(
        select(RemediationTask.state, func.count()).group_by(RemediationTask.state)
    ).all())
    blocked = session.scalars(
        select(Project).where(Project.admission_status.in_([
            "collecting", "parsed", "matching", "blocked_missing_data", "blocked_hard_requirement", "pending_bid_approval",
        ])).order_by(Project.updated_at.desc()).limit(50)
    ).all()
    return {
        "request_id": request_id,
        "cards": {
            "projects_total": sum(project_states.values()), "projects_by_state": project_states,
            "pool_by_status": pool_states, "tasks_by_state": task_states,
            "screening_count": pool_states.get(service.POOL_PENDING, 0) + pool_states.get(service.POOL_NEEDS_REVIEW, 0),
            "deep_dive_count": pool_states.get(service.POOL_DEEP_DIVE, 0),
            "open_tasks": sum(v for k, v in task_states.items() if k not in {"closed", "rejected", "cancelled"}),
        },
        "blocked_projects": [{"project_id": p.project_id, "project_name": p.project_name,
                              "current_stage": p.admission_status, "updated_at": p.updated_at.isoformat()} for p in blocked],
        "notice": "管理面板仅显示流程状态和任务聚合；不构成投标建议或审批结论。",
    }
