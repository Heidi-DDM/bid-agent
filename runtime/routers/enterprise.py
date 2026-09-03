# F020：企业资料库后台 API（数据管理员；F020 §2.2.7）
# GET  /api/v1/enterprise/qualifications
# GET  /api/v1/enterprise/safety-license
# GET  /api/v1/enterprise/managers
# POST /api/v1/qualifications、/performances、/personnel、/evidences（资料维护，缺证据默认 pending_verification）
# R022 追加：批量导入（xlsx/结构化记录）、核验队列、批量核验/驳回/重导入、过期扫描
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime.routers.deps import get_actor, get_db, get_role, get_request_id, require_role
from runtime.core.errors import ApiError
from runtime.db import api_service
from runtime.db import enterprise_service

router = APIRouter(prefix="/api/v1", tags=["enterprise"])


class ImportBatchBody(BaseModel):
    """R022 批量导入批次：一个 kind 一请求（结构化记录数组，与 r006/r007
    演练产物同构；xlsx 由前端/脚本转成该结构后调用，避免 runtime 引入 openpyxl）。"""
    kind: str = Field(..., description="qualifications/performances/personnel/managers")
    rows: list[dict] = Field(..., min_length=1)
    category: str | None = Field(None, description="personnel 子类：registered_builder/technical_title/post_certificate")
    source: str = Field("manual", description="来源标注（文件名/批次号）")
    data_owner: str = Field(..., description="数据责任人（缺省行级覆盖）")
    material_id: str | None = None


class VerifyBody(BaseModel):
    kind: str = Field(...)
    ids: list[str] = Field(..., min_length=1)
    action: str = Field(..., pattern="^(approve|reject)$")
    comment: str | None = None


class ReimportBody(BaseModel):
    kind: str = Field(...)
    ids: list[str] = Field(..., min_length=1)


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


# ── R022 批量导入与核验（F022 §2 / 08-清单 §2.2）────────────────

@router.post("/enterprise/import")
def import_batch(
    body: ImportBatchBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """批量导入企业资料（数据管理员；缺证据默认 pending_verification）。"""
    require_role(role, "enterprise", "write", session=session, actor=actor)
    kind = body.kind
    if kind == "qualifications":
        result = enterprise_service.import_qualifications(
            session, body.rows, data_owner=body.data_owner, actor=actor,
            source=body.source, material_id=body.material_id)
    elif kind == "performances":
        result = enterprise_service.import_performances(
            session, body.rows, data_owner=body.data_owner, actor=actor,
            source=body.source, material_id=body.material_id)
    elif kind == "personnel":
        if not body.category:
            raise ApiError("invalid_request", "personnel 导入必须提供 category（registered_builder/technical_title/post_certificate）")
        result = enterprise_service.import_personnel(
            session, body.rows, category=body.category,
            data_owner=body.data_owner, actor=actor, source=body.source)
    elif kind == "managers":
        result = enterprise_service.import_managers(
            session, body.rows, data_owner=body.data_owner, actor=actor,
            source=body.source)
    else:
        raise ApiError("invalid_request",
                       f"未知导入类型: {kind}（允许 qualifications/performances/personnel/managers）")
    session.commit()
    return {"request_id": request_id, "import": result}


@router.get("/enterprise/verification-queue")
def verification_queue(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
    kind: str | None = None,
    status: str = "pending_verification",
) -> dict:
    """核验队列：待核验（默认）/已驳回/已过期记录（数据管理员/经营负责人）。"""
    require_role(role, "enterprise", "read", session=session, actor=role)
    queue = enterprise_service.list_verification_queue(
        session, kind=kind, status=status)
    return {"request_id": request_id, "status": status, "count": len(queue),
            "items": queue}


@router.post("/enterprise/verify")
def verify_batch(
    body: VerifyBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """批量核验：approve（pending→active，填 verified_at；无证据拒绝）/ reject。"""
    require_role(role, "enterprise", "verify", session=session, actor=actor)
    result = enterprise_service.verify_records(
        session, kind=body.kind, ids=body.ids, action=body.action,
        actor=actor, comment=body.comment)
    session.commit()
    return {"request_id": request_id, "verify": result}


@router.post("/enterprise/reimport")
def reimport_batch(
    body: ReimportBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """重导入：rejected/expired → pending_verification（新核验周期，审计留痕）。"""
    require_role(role, "enterprise", "write", session=session, actor=actor)
    result = enterprise_service.reimport_records(
        session, kind=body.kind, ids=body.ids, actor=actor)
    session.commit()
    return {"request_id": request_id, "reimport": result}


@router.post("/enterprise/expire-overdue")
def expire_overdue(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """手动触发过期扫描：active 且 valid_until 已过的记录置 expired（数据管理员）。"""
    require_role(role, "enterprise", "verify", session=session, actor=actor)
    count = enterprise_service.expire_overdue(session)
    session.commit()
    return {"request_id": request_id, "expired": count}
