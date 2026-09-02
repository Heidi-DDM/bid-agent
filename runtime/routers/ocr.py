"""F020 OCR routing and manual-review endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime.core.errors import ApiError
from runtime.db import api_service, worker_service
from runtime.db.models import AnalysisJob, EvidenceFile
from runtime.routers.deps import get_actor, get_db, get_request_id, get_role, require_role

router = APIRouter(prefix="/api/v1/ocr", tags=["ocr"])


class OcrRouteBody(BaseModel):
    material_id: str = Field(..., min_length=1)
    actor: str | None = None


class OcrReviewBody(BaseModel):
    reviewer: str = Field(..., min_length=1)
    outcome: str = Field(..., pattern="^(approved|rejected|manual_review)$")
    comment: str | None = None


@router.post("/route")
def route_ocr(
    body: OcrRouteBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    require_role(role, "ocr", "write", session=session, actor=actor, object_ref=body.material_id)
    material = api_service.get_material_or_404(session, body.material_id)
    if material.owner_type != "enterprise":
        raise ApiError("invalid_state_transition", "OCR 仅允许处理企业资料")
    job, created = worker_service.create_job(
        session, kind="ocr.route", input_ref=body.material_id,
        project_id=material.project_id,
    )
    api_service.audit(session, actor=body.actor or actor, action="ocr.route",
                      basis=f"material_id={body.material_id}",
                      outcome="created" if created else "idempotent_reuse",
                      object_ref=body.material_id)
    session.commit()
    return {"request_id": request_id, "job_id": job.job_id, "created": created}


@router.get("/jobs/{job_id}")
def ocr_job_status(
    job_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    require_role(role, "ocr", "read", session=session, actor=role, object_ref=job_id)
    job = session.get(AnalysisJob, job_id)
    if job is None or job.kind != "ocr.route":
        raise ApiError("not_found", f"OCR 任务不存在: {job_id}")
    return {"request_id": request_id, "job_id": job.job_id, "status": job.status,
            "error_code": job.error_code, "material_id": job.input_ref}


@router.get("/review-queue")
def ocr_review_queue(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    require_role(role, "ocr", "review", session=session, actor=role)
    rows = session.scalars(
        select(EvidenceFile).where(
            (EvidenceFile.ocr_confidence.is_(None)) | (EvidenceFile.ocr_confidence < 0.8)
        ).order_by(EvidenceFile.created_at)
    ).all()
    return {"request_id": request_id, "items": [
        {"evidence_id": row.evidence_id, "material_id": row.material_id,
         "ocr_confidence": row.ocr_confidence, "page_no": row.page_no,
         "file_type": row.file_type}
        for row in rows
    ]}


@router.post("/review/{evidence_id}")
def review_ocr(
    evidence_id: str,
    body: OcrReviewBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    require_role(role, "ocr", "review", session=session, actor=body.reviewer, object_ref=evidence_id)
    evidence = session.get(EvidenceFile, evidence_id)
    if evidence is None:
        raise ApiError("not_found", f"OCR 证据不存在: {evidence_id}")
    api_service.audit(session, actor=body.reviewer or actor, action="ocr.review",
                      basis=f"evidence_id={evidence_id}", outcome=body.outcome,
                      object_ref=evidence_id)
    session.commit()
    return {"request_id": request_id, "evidence_id": evidence_id,
            "status": body.outcome}
