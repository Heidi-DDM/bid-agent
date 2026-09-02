# F020：企业资料库后台 API（数据管理员；F020 §2.2.7）
# GET  /api/v1/enterprise/qualifications
# GET  /api/v1/enterprise/safety-license
# GET  /api/v1/enterprise/managers
# POST /api/v1/qualifications、/performances、/personnel、/evidences（资料维护，缺证据默认 pending_verification）
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime.routers.deps import get_actor, get_db, get_role, get_request_id, require_role
from runtime.core.errors import ApiError
from runtime.db import api_service

router = APIRouter(prefix="/api/v1", tags=["enterprise"])


class QualificationBody(BaseModel):
    category: str = Field(..., description="资质类别，如 建筑工程施工总承包")
    level: str | None = None
    specialty: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    issuer: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    data_owner: str = Field(..., description="数据责任人")


class PerformanceBody(BaseModel):
    project_name: str = Field(...)
    project_type: str = Field(...)
    specialty: str | None = None
    contract_amount: float | None = None
    completed_at: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    data_owner: str = Field(...)


class PersonnelBody(BaseModel):
    name: str = Field(...)
    category: str = Field(..., description="registered_builder / technical_title / post_certificate / other")
    specialty: str | None = None
    cert_level: str | None = None
    cert_no: str | None = None
    valid_until: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    data_owner: str = Field(...)


class EvidenceBody(BaseModel):
    material_id: str | None = None
    file_type: str = Field(...)
    object_uri: str = Field(...)
    source_hash: str = Field(...)
    classification: str = "internal"
    uploaded_by: str = Field(...)


@router.get("/enterprise/qualifications")
def list_qualifications(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """资质列表（对齐 PROTOTYPE.company_data.qualifications）。"""
    items = api_service.list_qualifications(session, role)
    return {"request_id": request_id, "items": items}


@router.get("/enterprise/safety-license")
def safety_license(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """安全生产许可证（对齐 PROTOTYPE.company_data.safety_license）。"""
    require_role(role, "enterprise", "read", session=session, actor=role)
    from runtime.db.models import Qualification

    q = session.scalar(
        select(Qualification).where(Qualification.category.ilike("%安全生产%")).limit(1)
    )
    return {
        "request_id": request_id,
        "safety_license": {
            "name": q.category if q else None,
            "no": q.issuer if q else None,
            "valid_until": q.valid_until.isoformat() if q and q.valid_until else None,
            "status": q.status if q else "pending_verification",
        },
    }


@router.get("/enterprise/managers")
def managers(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """项目经理名录（对齐 PROTOTYPE.company_data.managers；脱敏展示）。"""
    require_role(role, "enterprise", "read", session=session, actor=role)
    from runtime.db.models import Manager

    rows = session.scalars(select(Manager).order_by(Manager.manager_id)).all()
    return {
        "request_id": request_id,
        "items": [
            {
                "id": m.manager_id,
                "display_name": m.display_name,
                "specialty": m.specialty,
                "reg_cert_type": m.reg_cert_type,
                "cert_level": m.cert_level,
                "edu_safety_status": m.edu_safety_status,
                "availability": m.availability,
                "active_project": (m.active_projects[0] if m.active_projects else None),
                "status": m.status,
            }
            for m in rows
        ],
    }


@router.post("/qualifications")
def create_qualification(
    body: QualificationBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """新增资质（数据管理员；缺证据默认 pending_verification，F020 §2.2.7）。"""
    require_role(role, "enterprise", "write", session=session, actor=actor)
    from datetime import date

    from runtime.db.models import Qualification

    try:
        until = date.fromisoformat(body.valid_until) if body.valid_until else None
    except ValueError:
        raise ApiError("invalid_request", "valid_until 必须为合法日期")
    import uuid

    q = Qualification(
        qualification_id=f"Q-{uuid.uuid4().hex[:10]}",
        category=body.category,
        level=body.level,
        specialty=body.specialty,
        valid_until=until,
        issuer=body.issuer,
        evidence_refs=body.evidence_refs,
        data_owner=body.data_owner,
        status="pending_verification" if not body.evidence_refs else "active",
    )
    session.add(q)
    api_service.audit(session, actor=actor, action="qualification.create",
                      basis=f"category={body.category}",
                      outcome=f"status={q.status}", object_ref=q.qualification_id)
    session.commit()
    return {"request_id": request_id, "qualification_id": q.qualification_id, "status": q.status}


@router.post("/performances")
def create_performance(
    body: PerformanceBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """新增业绩（数据管理员；缺证据默认 pending_verification）。"""
    require_role(role, "enterprise", "write", session=session, actor=actor)
    from datetime import date

    from runtime.db.models import Performance

    try:
        done = date.fromisoformat(body.completed_at) if body.completed_at else None
    except ValueError:
        raise ApiError("invalid_request", "completed_at 必须为合法日期")
    import uuid

    p = Performance(
        performance_id=f"PF-{uuid.uuid4().hex[:10]}",
        project_name=body.project_name,
        project_type=body.project_type,
        specialty=body.specialty,
        contract_amount=body.contract_amount,
        completed_at=done,
        evidence_refs=body.evidence_refs,
        data_owner=body.data_owner,
        status="pending_verification" if not body.evidence_refs else "active",
    )
    session.add(p)
    api_service.audit(session, actor=actor, action="performance.create",
                      basis=f"project_name={body.project_name}",
                      outcome=f"status={p.status}", object_ref=p.performance_id)
    session.commit()
    return {"request_id": request_id, "performance_id": p.performance_id, "status": p.status}


@router.post("/personnel")
def create_personnel(
    body: PersonnelBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """新增人员（数据管理员；缺证据默认 pending_verification）。"""
    require_role(role, "enterprise", "write", session=session, actor=actor)
    from datetime import date

    from runtime.db.models import Personnel

    try:
        until = date.fromisoformat(body.valid_until) if body.valid_until else None
    except ValueError:
        raise ApiError("invalid_request", "valid_until 必须为合法日期")
    import uuid

    p = Personnel(
        personnel_id=f"P-{uuid.uuid4().hex[:10]}",
        name=body.name,
        category=body.category,
        specialty=body.specialty,
        cert_level=body.cert_level,
        cert_no=body.cert_no,
        valid_until=until,
        evidence_refs=body.evidence_refs,
        data_owner=body.data_owner,
        status="pending_verification" if not body.evidence_refs else "active",
    )
    session.add(p)
    api_service.audit(session, actor=actor, action="personnel.create",
                      basis=f"name={body.name} category={body.category}",
                      outcome=f"status={p.status}", object_ref=p.personnel_id)
    session.commit()
    return {"request_id": request_id, "personnel_id": p.personnel_id, "status": p.status}


@router.post("/evidences")
def create_evidence(
    body: EvidenceBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """登记证据文件元数据（数据管理员；F019 evidence_files）。"""
    require_role(role, "enterprise", "write", session=session, actor=actor)
    from runtime.db.models import EvidenceFile
    import uuid

    ev = EvidenceFile(
        evidence_id=f"E-{uuid.uuid4().hex[:10]}",
        material_id=body.material_id,
        file_type=body.file_type,
        object_uri=body.object_uri,
        source_hash=body.source_hash,
        classification=body.classification,
        uploaded_by=body.uploaded_by or actor,
    )
    session.add(ev)
    api_service.audit(session, actor=actor, action="evidence.create",
                      basis=f"file_type={body.file_type}",
                      outcome="recorded", object_ref=ev.evidence_id)
    session.commit()
    return {"request_id": request_id, "evidence_id": ev.evidence_id}
