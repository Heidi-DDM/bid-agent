# F020：匹配任务编排 API
# POST /api/v1/projects/{project_id}/match       —— 仅任务编排器（parse.completed 触发）或重算流程内部调用；
#                                                  前端不暴露手动匹配按钮（F020 §2.1/§2.2.5）。
# POST /api/v1/projects/{project_id}/recalculate —— 补录核验后重算；同一证据版本重复调用返回既有任务（幂等）。
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime.routers.deps import get_actor, get_db, get_role, get_request_id, require_role
from runtime.core import orchestration
from runtime.core.errors import ApiError
from runtime.db import api_service, lifecycle_service, worker_service

router = APIRouter(prefix="/api/v1/projects", tags=["match"])


class RecalculateBody(BaseModel):
    evidence_version: str = Field(..., description="新证据版本引用（幂等键一部分）")
    actor: str | None = None


def _project_materials(session: Session, project_id: str) -> list[dict]:
    from runtime.db.models import Material

    rows = session.scalars(
        select(Material).where(Material.project_id == project_id)
    ).all()
    return [
        {
            "material_id": m.material_id,
            "material_type": m.material_type,
            "parse_status": m.parse_status,
            "version": m.version,
        }
        for m in rows
    ]


@router.post("/{project_id}/match")
def trigger_match(
    project_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """任务编排器触发匹配：仅在解析成功后首次触发一次（幂等）；前端不暴露此接口。"""
    require_role(role, "match", "write", session=session, actor=actor, object_ref=project_id)
    # ADR-004 服务端门禁：项目过期 / 身份冲突 → 409 invalid_state_transition（写审计）
    project = api_service.get_project_or_404(session, project_id)
    lifecycle_service.ensure_identity_then_deny(
        session, project, action="match", actor=actor, audit_action="match.trigger_denied"
    )
    materials = _project_materials(session, project_id)
    run = api_service.latest_match_run(session, project_id)
    existing_runs = 1 if run is not None else 0
    if not orchestration.should_trigger_first_match(materials, existing_runs):
        raise ApiError("invalid_state_transition",
                       "不满足首次匹配触发条件：需解析成功且尚无匹配 run（F020 §2.1）")

    job, created = worker_service.create_job(
        session, kind="match.run", input_ref=project_id, project_id=project_id,
        retry_failed=True,
    )
    # retry_failed=True（2026-09-23）：首次 match.run 被门禁终态拒绝后，纠正数据
    # （延期登记/身份纠正）再次触发必须复活既有失败任务，否则幂等键永远占住、
    # 匹配不再执行（与 schedule_post_parse 2026-09-15 修复同一理由）；
    # 门禁在上方 ensure_identity_then_deny 已过，复活即安全。
    api_service.audit(session, actor=actor, action="match.trigger",
                      basis=f"project_id={project_id}",
                      outcome="created" if created else "idempotent_reuse",
                      object_ref=project_id)
    session.commit()
    return {"request_id": request_id, "job_id": job.job_id, "created": created}


@router.post("/{project_id}/recalculate")
def recalculate(
    project_id: str,
    body: RecalculateBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """补录核验后重算：以新证据版本创建新匹配任务，旧结果标记 stale；幂等（F020 §2.2.5）。"""
    require_role(role, "match", "write", session=session, actor=actor, object_ref=project_id)
    actor = body.actor or actor
    # ADR-004 服务端门禁（P0-03/P0-02）：过期项目不得重算进入后续流程；身份冲突不得重算
    project = api_service.get_project_or_404(session, project_id)
    lifecycle_service.ensure_identity_then_deny(
        session, project, action="recalculate", actor=actor, audit_action="match.recalculate_denied"
    )
    materials = _project_materials(session, project_id)
    run = api_service.latest_match_run(session, project_id)
    if not orchestration.can_recalculate(materials, run):
        raise ApiError("invalid_state_transition",
                       "重算前置不满足：需存在已解析材料与已有匹配 run（F020 §2.2.5）")

    # 幂等键带证据版本：同一证据版本重复调用返回既有任务（F020 §2.2.5）
    idem_key = worker_service.job_logic.build_idempotency_key(
        "match.recalculate", body.evidence_version, project_id
    )
    from runtime.db.models import AnalysisJob

    existing = session.scalar(select(AnalysisJob).where(AnalysisJob.idempotency_key == idem_key))
    if existing is not None:
        return {"request_id": request_id, "job_id": existing.job_id, "created": False}

    api_service.mark_admission_results_stale(session, project_id)
    job, created = worker_service.create_job(
        session,
        kind="match.recalculate",
        input_ref=body.evidence_version,
        project_id=project_id,
    )
    api_service.audit(session, actor=actor, action="match.recalculate",
                      basis=f"evidence_version={body.evidence_version}",
                      outcome="created", object_ref=project_id)
    session.commit()
    return {"request_id": request_id, "job_id": job.job_id, "created": created}
