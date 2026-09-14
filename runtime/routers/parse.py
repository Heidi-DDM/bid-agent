# R021/F021：解析候选复核 API
# GET  /api/v1/parse/projects/{project_id}/materials/{material_id}/candidates  # 候选列表（含进度）
# GET  /api/v1/parse/projects/{project_id}/requirements                        # F021 §2.1 v1.3：六组聚合视图
# POST /api/v1/parse/candidates/{candidate_id}/review                          # 人工复核决策
# POST /api/v1/parse/projects/{project_id}/materials/{material_id}/confirm     # 全部确认 → 写规则集/字段溯源 → parsed
# 权限：投标专员（bid_specialist）复核；经营负责人只读。F021 §2.7：确认前不产生 RuleSet。
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime.core.errors import ApiError
from runtime.db import api_service, parse_service
from runtime.db.models import Material
from runtime.db.parse_service import REJECT_REASONS
from runtime.routers.deps import get_actor, get_db, get_request_id, get_role, require_role

router = APIRouter(prefix="/api/v1/parse", tags=["parse"])


class ReviewBody(BaseModel):
    decision: str = Field(..., pattern="^(approved|rejected|revised)$")
    reviewer: str = Field(..., min_length=1)
    review_note: str | None = None
    revised_payload: dict | None = None

    @model_validator(mode="after")
    def _review_semantics(self) -> "ReviewBody":
        """F021 §2.1 v1.3 复核语义：rejected 必带结构化原因；revised 必带修正值；approved 不带 payload。"""
        if self.decision == "rejected":
            note = (self.review_note or "").strip()
            if not note or not any(
                note == r or note.startswith(f"{r}：") or note.startswith(f"{r}:")
                for r in REJECT_REASONS
            ):
                raise ValueError(
                    f"rejected 必须携带结构化原因（{' / '.join(REJECT_REASONS)}）并写入 review_note"
                )
            if self.revised_payload:
                raise ValueError("rejected 不得携带 revised_payload（原因不是修正值）")
        if self.decision == "revised" and not self.revised_payload:
            raise ValueError("revised 必须携带 revised_payload（修正后的 assertion/value 与依据）")
        if self.decision == "approved" and self.revised_payload:
            raise ValueError("approved 不得携带 revised_payload（确认无误无需修正值）")
        return self


class ConfirmBody(BaseModel):
    actor: str = Field(..., min_length=1)
    as_of: str | None = Field(None, description="判定时点 ISO 日期；缺省用规则集锚点日期")


@router.get("/projects/{project_id}/requirements")
def project_requirements(
    project_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """解析结果聚合（F021 §2.1 v1.3 / 09-优化方案 §3.3）：六组业务语言分组，按视图切换。

    写规则集前（material.parse_status != parsed）返回 parse_candidates 视图（review_status 实时）；
    规则集已写入后返回 Requirement + FieldTrace 的 confirmed 视图。
    """
    require_role(role, "tender_document", "read", session=session, actor=role,
                 object_ref=project_id)
    data = parse_service.grouped_requirements(session, project_id=project_id)
    data["request_id"] = request_id
    return data


@router.get("/projects/{project_id}/materials/{material_id}/candidates")
def list_parse_candidates(
    project_id: str,
    material_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    require_role(role, "tender_document", "read", session=session, actor=role,
                 object_ref=material_id)
    material = api_service.get_material_or_404(session, material_id)
    if material.project_id != project_id:
        raise ApiError("not_found", f"材料不属于项目 {project_id}")
    rows = parse_service.list_candidates(
        session, project_id=project_id, material_id=material_id
    )
    return {
        "request_id": request_id,
        "project_id": project_id,
        "material_id": material_id,
        "version": material.version,
        "items": rows,
        "progress": parse_service.pending_summary(
            session, project_id=project_id, material_id=material_id
        ),
    }


@router.post("/candidates/{candidate_id}/review")
def review_candidate(
    candidate_id: str,
    body: ReviewBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    require_role(role, "tender_document", "write", session=session, actor=actor,
                 object_ref=candidate_id)
    # 定位候选 → 取出 material 版本信息
    from runtime.db.models import ParseCandidate

    cand = session.scalar(
        select(ParseCandidate).where(ParseCandidate.candidate_id == candidate_id)
    )
    if cand is None:
        raise ApiError("not_found", f"候选不存在: {candidate_id}")
    try:
        result = parse_service.decide_candidate(
            session,
            candidate_id=candidate_id,
            material_id=cand.material_id,
            version=cand.version,
            decision=body.decision,
            reviewer=body.reviewer,
            review_note=body.review_note,
            revised_payload=body.revised_payload,
        )
    except parse_service.CandidateNotFound as exc:
        raise ApiError("not_found", str(exc))
    except parse_service.CandidateAlreadyDecided as exc:
        raise ApiError("invalid_state_transition", str(exc))
    except parse_service.ParseServiceError as exc:
        raise ApiError("invalid_request", str(exc))

    # 决策后更新 material.parse_status（pending → manual_review / 维持复核中）
    api_service.audit(session, actor=body.reviewer, action="parse.candidate.review",
                      basis=f"material={cand.material_id}:v{cand.version}",
                      outcome=f"{body.decision}", object_ref=cand.material_id)
    _sync_material_parse_status(session, cand.material_id, cand.version)
    session.commit()
    return {"request_id": request_id, "candidate_id": candidate_id,
            "status": result["status"], "decided_at": result["decided_at"]}


@router.post("/projects/{project_id}/materials/{material_id}/confirm")
def confirm_parse(
    project_id: str,
    material_id: str,
    body: ConfirmBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """全部规则候选人工确认 → 写入 RuleSet/Requirement + 主卡 FieldTrace → parsed。

    前置：无 pending 候选；存在 rejected → 409（需先驳回处理/修正）；规则集已存在 → 409。
    """
    require_role(role, "tender_document", "write", session=session, actor=actor,
                 object_ref=material_id)
    material = api_service.get_material_or_404(session, material_id)
    if material.project_id != project_id:
        raise ApiError("not_found", f"材料不属于项目 {project_id}")

    progress = parse_service.pending_summary(
        session, project_id=project_id, material_id=material_id
    )
    if progress["pending"] > 0:
        raise ApiError("invalid_state_transition",
                       f"仍有 {progress['pending']} 个待决候选，不能确认（F021 §2.7）")

    # as_of：确认体显式传 → 否则取规则集锚点（seed 为 2025-10-30）；仍缺省则拒绝（不得默认当前时间）
    from runtime.db.models import Requirement, RuleSet

    as_of = body.as_of
    if not as_of:
        anchor = session.scalar(
            select(Requirement.as_of)
            .join(RuleSet, RuleSet.rule_set_id == Requirement.rule_set_id)
            .where(RuleSet.project_id == project_id)
            .limit(1)
        )
        as_of = anchor
    if not as_of:
        raise ApiError("invalid_request", "as_of 必填（判定时点不得默认当前时间，F008 §4.1）")

    try:
        confirmed = parse_service.confirm_rules_from_approved(
            session, project_id=project_id, material_id=material_id,
            version=material.version, as_of=as_of, created_by=body.actor,
        )
    except parse_service.RuleSetExists as exc:
        raise ApiError("invalid_state_transition", str(exc))
    except parse_service.ParseServiceError as exc:
        raise ApiError("invalid_request", str(exc))

    # 主卡字段溯源（approved/revised 非缺失字段）
    content_hash = material.content_hash
    fields_written = parse_service.confirm_main_card_fields(
        session, material_id=material_id, version=material.version,
        project_id=project_id, actor=body.actor, content_hash=content_hash,
    )

    # 状态：存在 rejected → manual_review（部分规则缺项，用户另行处理）；否则 parsed
    if confirmed["rejected"] > 0:
        parse_service.mark_material_manual_review(
            session, material_id=material_id, version=material.version,
            note=f"{confirmed['rejected']} 个候选被驳回，需人工补录/修正",
        )
        material_status = "manual_review"
    else:
        parse_service.mark_material_parsed(
            session, material_id=material_id, version=material.version, actor=body.actor
        )
        material_status = "parsed"

    api_service.audit(session, actor=body.actor, action="parse.confirmed",
                      basis=f"material={material_id}:v{material.version} as_of={as_of}",
                      outcome=f"rules={confirmed['created_requirements']} "
                              f"fields={fields_written} rule_set={confirmed['rule_set_id']}",
                      object_ref=project_id)
    session.commit()

    # R021-5 / F021 §2 第 7 步：确认（parsed）后自动创建 L2/L3 索引任务与首次匹配任务
    # （幂等：重复 confirm 已由 RuleSet 唯一键拦截；重复调度由任务幂等键拦截）
    scheduled: dict = {"index_jobs": [], "match_job": None}
    if material_status == "parsed":
        scheduled = api_service.schedule_post_parse(session, project_id=project_id)

    return {
        "request_id": request_id,
        "project_id": project_id,
        "material_id": material_id,
        "version": material.version,
        "material_status": material_status,
        "rule_set_id": confirmed["rule_set_id"],
        "created_requirements": confirmed["created_requirements"],
        "rejected": confirmed["rejected"],
        "main_card_fields_written": fields_written,
        "as_of": as_of,
        "index_jobs": scheduled["index_jobs"],
        "match_job": scheduled["match_job"],
    }


def _sync_material_parse_status(session: Session, material_id: str, version: int) -> None:
    """按当前候选决策状态同步 material.parse_status（pending/manual_review/parsed）。"""
    material = session.get(Material, (material_id, version))
    if material is None:
        return
    status = parse_service.material_parse_status(
        session, project_id=material.project_id or "", material_id=material_id
    )
    if status != material.parse_status and status != "pending":
        material.parse_status = status
