# F020：企业资料库后台 API（数据管理员；F020 §2.2.7）
# GET  /api/v1/enterprise/qualifications
# GET  /api/v1/enterprise/safety-license
# GET  /api/v1/enterprise/managers
# POST /api/v1/qualifications、/performances、/personnel、/evidences（资料维护，缺证据默认 pending_verification）
# R022 追加：批量导入（xlsx/结构化记录）、核验队列、批量核验/驳回/重导入、过期扫描
# F022 §5.1 v1.3 追加：受控导入 preview/commit（Excel/CSV 台账：嗅探→预览→字段映射→原文不可变落库+行回链）
from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime.routers.deps import get_actor, get_db, get_role, get_request_id, require_role
from runtime.core.config import object_store_root
from runtime.core.errors import ApiError
from runtime.db import api_service
from runtime.db import enterprise_service
from runtime.db.enterprise_service import wildcard_like
from runtime.db import excel_service
from runtime.db import material_service
from runtime.db.material_service import ValidationError as MaterialValidationError

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


class VerifyAndRecalcBody(BaseModel):
    """F026 §7：核验通过 + 按证据版本重匹配。核验与重算在同一事务/审计链完成。"""
    project_id: str = Field(...)
    kind: str = Field(...)
    ids: list[str] = Field(..., min_length=1)
    action: str = Field("approve", pattern="^(approve|reject)$")
    comment: str | None = None
    evidence_version: str | None = Field(
        None, description="显式证据版本（重算幂等键一部分）；缺省自动生成"
    )


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


@router.get("/enterprise/overview")
def enterprise_overview(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """F027：企业资料画像——按投标评分维度四分类聚合解析/核验/证据覆盖情况。

    只读事实聚合（计数与覆盖率），不构成资格结论、评分或投标建议；
    逐项匹配结论以匹配引擎输出为准（F008/F023）。
    """
    require_role(role, "enterprise", "read", session=session, actor=role)
    from runtime.db import enterprise_profile

    return {"request_id": request_id, **enterprise_profile.build_overview(session)}


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
    limit: int | None = None,
    offset: int = 0,
    q: str | None = None,
) -> dict:
    """项目经理名录（对齐 PROTOTYPE.company_data.managers；脱敏展示）。

    F027 2026-09-22：载荷补全 b_cert_no/cert_valid_until/reg_cert_no/organization/
    availability 等台账字段（此前页面有列但接口不返回）；支持分页与按姓名 q 过滤；
    不带 limit 时保持全量返回（兼容既有调用方）。
    """
    require_role(role, "enterprise", "read", session=session, actor=role)
    from runtime.db.models import Manager

    stmt = select(Manager).order_by(Manager.manager_id)
    count_stmt = select(Manager.manager_id)
    if q:
        cond = Manager.display_name.ilike(wildcard_like(q))
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    total = len(session.scalars(count_stmt).all())
    if limit is not None:
        limit, offset = _page(limit, offset)
        rows = session.scalars(stmt.offset(offset).limit(limit)).all()
    else:
        limit, offset = None, 0
        rows = session.scalars(stmt).all()
    return {
        "request_id": request_id,
        "total": total, "limit": limit, "offset": offset,
        "items": [
            {
                "id": m.manager_id,
                "display_name": m.display_name,
                "name": m.display_name,
                "organization": m.organization,
                "specialty": m.specialty,
                "reg_cert_type": m.reg_cert_type,
                "reg_cert_no": m.reg_cert_no,
                "cert_type": m.reg_cert_type,
                "cert_level": m.cert_level,
                "b_cert_no": m.b_cert_no,
                "cert_valid_until": m.cert_valid_until.isoformat() if m.cert_valid_until else None,
                "edu_safety_status": m.edu_safety_status,
                "availability": m.availability,
                "active_project": (m.active_projects[0] if m.active_projects else None),
                "status": m.status,
            }
            for m in rows
        ],
    }


def _page(limit: int, offset: int) -> tuple[int, int]:
    """分页参数钳制（limit 1-500，offset ≥ 0）。"""
    return max(1, min(500, limit)), max(0, offset)


@router.get("/enterprise/performances")
def list_performances(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
) -> dict:
    """企业业绩分页列表（F027 2026-09-22；台账字段全量，q 按项目名称模糊过滤）。"""
    require_role(role, "enterprise", "read", session=session, actor=role)
    from runtime.db.models import Performance

    limit, offset = _page(limit, offset)
    stmt = select(Performance).order_by(Performance.performance_id)
    count_stmt = select(Performance.performance_id)
    if q:
        cond = Performance.project_name.ilike(wildcard_like(q))
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    total = len(session.scalars(count_stmt).all())
    rows = session.scalars(stmt.offset(offset).limit(limit)).all()
    return {
        "request_id": request_id,
        "total": total, "limit": limit, "offset": offset,
        "items": [
            {
                "performance_id": p.performance_id,
                "project_name": p.project_name,
                "project_type": p.project_type,
                "specialty": p.specialty,
                "contract_amount": float(p.contract_amount) if p.contract_amount is not None else None,
                "awarded_at": p.awarded_at.isoformat() if p.awarded_at else None,
                "completed_at": p.completed_at.isoformat() if p.completed_at else None,
                "owner_org": p.owner_org,
                "evidence_refs": p.evidence_refs or [],
                "status": p.status,
            }
            for p in rows
        ],
    }


@router.get("/enterprise/personnel")
def list_personnel(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
    category: str | None = None,
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
) -> dict:
    """企业人员分页列表（F027 2026-09-22；category=registered_builder/technical_title/
    post_certificate/other，q 按姓名/证书编号模糊过滤，附各类计数）。"""
    require_role(role, "enterprise", "read", session=session, actor=role)
    from runtime.db.models import Personnel
    from sqlalchemy import func

    if category is not None and category not in (
        "registered_builder", "technical_title", "post_certificate", "other",
    ):
        raise ApiError("invalid_request",
                       "category 允许 registered_builder/technical_title/post_certificate/other")
    # 2026-09-22 业主决策：企业资料库人名不脱敏，按公司提供的台账原文展示
    # （本地库、enterprise:read 门禁 + 审计；远程演示环境另行按 docs/11 脱敏口径导入）。

    limit, offset = _page(limit, offset)
    stmt = select(Personnel).order_by(Personnel.personnel_id)
    count_stmt = select(Personnel.personnel_id)
    if category:
        stmt = stmt.where(Personnel.category == category)
        count_stmt = count_stmt.where(Personnel.category == category)
    if q:
        like = wildcard_like(q)
        cond = (Personnel.name.ilike(like) | Personnel.cert_no.ilike(like)
                | Personnel.specialty.ilike(like) | Personnel.organization.ilike(like))
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    total = len(session.scalars(count_stmt).all())
    by_category = dict(session.execute(
        select(Personnel.category, func.count()).group_by(Personnel.category)
    ).all())
    rows = session.scalars(stmt.offset(offset).limit(limit)).all()
    return {
        "request_id": request_id,
        "total": total, "limit": limit, "offset": offset,
        "by_category": by_category,
        "items": [
            {
                "personnel_id": p.personnel_id,
                "name": p.name,
                "organization": p.organization,
                "category": p.category,
                "specialty": p.specialty,
                "cert_level": p.cert_level,
                "cert_no": p.cert_no,
                "valid_until": p.valid_until.isoformat() if p.valid_until else None,
                "on_site": p.on_site,
                "on_site_project": p.on_site_project,
                "evidence_refs": p.evidence_refs or [],
                "status": p.status,
            }
            for p in rows
        ],
    }


@router.get("/enterprise/evidence-files")
def list_evidence_files(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """企业证据文件分页列表（F027 2026-09-22；附「关联资料」与文档路由/OCR 置信度）。

    linked[]：由 qualifications/performances 的 evidence_refs 反查该证据证明了哪份
    资质/哪条业绩（refs 中出现 material_id 即视为关联，兼容 material: 前缀格式）；
    review_note 记录文档路由结论（有文本层/OCR 平均置信度，回填脚本写入）。
    """
    require_role(role, "enterprise", "read", session=session, actor=role)
    from runtime.db.models import EvidenceFile, Performance, Qualification

    limit, offset = _page(limit, offset)
    total = len(session.scalars(select(EvidenceFile.evidence_id)).all())
    rows = session.scalars(
        select(EvidenceFile).order_by(EvidenceFile.evidence_id).offset(offset).limit(limit)
    ).all()

    # 关联反查：整表载入后按 refs 是否包含 material_id 匹配（量级小，方言无关）
    quals = session.scalars(select(Qualification)).all()
    perfs = session.scalars(select(Performance)).all()

    def _linked(material_id: str | None) -> list[dict]:
        if not material_id:
            return []
        out = []
        for q in quals:
            if any(material_id in str(r) for r in (q.evidence_refs or [])):
                out.append({"kind": "qualifications",
                            "label": f"{q.category}{'（' + q.level + '）' if q.level else ''}"})
        for pf in perfs:
            if any(material_id in str(r) for r in (pf.evidence_refs or [])):
                out.append({"kind": "performances", "label": pf.project_name[:40]})
        return out[:4]

    return {
        "request_id": request_id,
        "total": total, "limit": limit, "offset": offset,
        "items": [
            {
                "evidence_id": e.evidence_id,
                "file_type": e.file_type,
                "material_id": e.material_id,
                "object_uri": e.object_uri,
                "ocr_confidence": e.ocr_confidence,
                "review_note": e.review_note,
                "review_status": e.review_status,
                "valid_until": e.valid_until.isoformat() if e.valid_until else None,
                "uploaded_by": e.uploaded_by,
                "uploaded_at": e.uploaded_at.isoformat() if e.uploaded_at else None,
                "linked": _linked(e.material_id),
            }
            for e in rows
        ],
    }


@router.post("/enterprise/auto-verify")
def auto_verify(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """自动核验（F027 2026-09-22 业主决策）：台账导入且证据回链非空、有效期未过的
    待核验记录批量置 active（每条写审计，规则同人工核验门禁，仅免逐条点击）；
    无证据记录保持待核验并计数返回。"""
    require_role(role, "enterprise", "verify", session=session, actor=actor)
    result = enterprise_service.auto_verify_records(session, actor=actor)
    session.commit()
    return {"request_id": request_id, **result}


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


@router.post("/enterprise/verify-and-recalculate")
def verify_and_recalculate(
    body: VerifyAndRecalcBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """F026 §7：企业资料核验 + 按证据版本重匹配（一次调用、两步审计）。

    - 核验部分复用 verification 链（enterprise:verify）；重算部分复用
      match.recalculate 语义（match:write；ADR-004 门禁：过期/身份冲突拒绝）。
    - 只有本次确实有记录核验通过（或已是 active）才创建重算任务；
      未确证的新证据版本不得触发“满足”（F026 §5/§8.6）。
    """
    require_role(role, "enterprise", "verify", session=session, actor=actor)
    require_role(role, "match", "write", session=session, actor=actor,
                 object_ref=body.project_id)
    verify = enterprise_service.verify_records(
        session, kind=body.kind, ids=body.ids, action=body.action,
        actor=actor, comment=body.comment,
    )
    if body.action == "approve" and verify.get("changed", 0) == 0:
        raise ApiError(
            "invalid_request",
            "本次没有新增核验通过的记录（记录不存在/已 active/已过期），不触发重算——"
            "补录证据必须先经核验生效，禁止以未核验资料驱动重匹配。",
        )

    from runtime.core import orchestration
    from runtime.db import lifecycle_service
    from runtime.db import worker_service
    from runtime.routers.match import _project_materials

    project = api_service.get_project_or_404(session, body.project_id)
    lifecycle_service.ensure_identity_then_deny(
        session, project, action="recalculate", actor=actor,
        audit_action="match.recalculate_denied",
    )
    materials = _project_materials(session, body.project_id)
    run = api_service.latest_match_run(session, body.project_id)
    if not orchestration.can_recalculate(materials, run):
        raise ApiError(
            "invalid_state_transition",
            "重算前置不满足：需存在已解析材料与已有匹配 run（F020 §2.2.5）",
        )
    evidence_version = body.evidence_version or (
        "verify-recalc-" + datetime.now().strftime("%Y%m%d%H%M%S")
    )
    idem_key = worker_service.job_logic.build_idempotency_key(
        "match.recalculate", evidence_version, body.project_id
    )
    from runtime.db.models import AnalysisJob

    existing = session.scalar(select(AnalysisJob).where(AnalysisJob.idempotency_key == idem_key))
    if existing is not None:
        session.commit()
        return {"request_id": request_id, "verify": verify,
                "job_id": existing.job_id, "created": False,
                "evidence_version": evidence_version}
    api_service.mark_admission_results_stale(session, body.project_id)
    job, created = worker_service.create_job(
        session, kind="match.recalculate", input_ref=evidence_version,
        project_id=body.project_id,
    )
    api_service.audit(session, actor=actor, action="enterprise.verify_and_recalculate",
                      basis=f"kind={body.kind} ids={len(body.ids)} evidence_version={evidence_version}",
                      outcome="created", object_ref=body.project_id)
    session.commit()
    return {"request_id": request_id, "verify": verify,
            "job_id": job.job_id, "created": created,
            "project_id": body.project_id, "evidence_version": evidence_version}


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


# ── F022 §5.1 v1.3 受控导入：preview / commit（09-优化方案 §3.5）─────────

def _read_upload(file: UploadFile) -> tuple[bytes, str, str]:
    """读取上传 → (content, filename, suffix)。拒绝空文件。"""
    try:
        content = file.file.read()
    except Exception:
        raise ApiError("invalid_request", "读取上传文件失败")
    if not content:
        raise ApiError("invalid_request", "空文件：未上传台账内容")
    filename = (file.filename or "").lower()
    suffix = Path(filename).suffix if filename else ""
    return content, filename, suffix


@router.post("/enterprise/import/preview")
def enterprise_import_preview(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
    file: UploadFile = File(...),
    kind: str = Form(...),
) -> dict:
    """台账受控导入-安全预览（F022 §5.1 v1.3）：嗅探 + 表头/行数/前 5 行/
    未映射列/质量警告；无持久化副作用。权限 enterprise write（数据管理员）。
    """
    require_role(role, "enterprise", "write", session=session, actor=actor)
    if kind not in excel_service.ALLOWED_FIELD_KEYS:
        raise ApiError("invalid_request",
                       f"未知资料类型: {kind}（允许 qualifications/performances/personnel/managers）")
    content, filename, suffix = _read_upload(file)
    try:
        preview = excel_service.preview_ledger(content, suffix, kind=kind)
    except excel_service.LedgerError as exc:
        raise ApiError("unsupported_format", str(exc))
    api_service.audit(session, actor=actor, action="enterprise.ledger.preview",
                      basis=f"kind={kind} filename={filename} size={len(content)}",
                      outcome=f"rows={preview['row_count']} unmapped={len(preview['unmapped_columns'])}",
                      object_ref=filename)
    session.commit()
    return {
        "request_id": request_id,
        "filename": filename,
        "size_bytes": len(content),
        "preview": preview,
    }


@router.post("/enterprise/import/commit")
def enterprise_import_commit(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
    file: UploadFile = File(...),
    kind: str = Form(...),
    mapping: str = Form(...),
    data_owner: str = Form(...),
    source: str | None = Form(default=None),
    category: str | None = Form(default=None),
    project_id: str | None = Form(default=None),
) -> dict:
    """台账受控导入-确认（F022 §5.1 v1.3）：按 {列名: 字段键} 映射读行 →
    既有 enterprise 导入服务（幂等）；原文件同时作为企业私有证据
    （evidence_file / owner_type=enterprise）落不可变版本并回链 material_id；
    结构化行保留来源行号（evidence_refs 前缀 ledger:）。风险/损坏文件无残留。
    """
    require_role(role, "enterprise", "write", session=session, actor=actor)
    allowed = excel_service.ALLOWED_FIELD_KEYS.get(kind)
    if allowed is None:
        raise ApiError("invalid_request",
                       f"未知资料类型: {kind}（允许 qualifications/performances/personnel/managers）")
    if kind == "personnel" and not category:
        raise ApiError("invalid_request", "personnel 导入必须提供 category（registered_builder/technical_title/post_certificate）")
    try:
        mapping_obj = json.loads(mapping)
        if not isinstance(mapping_obj, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise ApiError("invalid_request", "mapping 必须是 JSON 对象（{列名: 字段键}）")
    invalid_keys = sorted(set(mapping_obj.values()) - set(allowed))
    if invalid_keys:
        raise ApiError("invalid_request",
                       f"mapping 含该资料类型不允许的字段键: {invalid_keys}（允许: {sorted(allowed)}）")

    content, filename, suffix = _read_upload(file)
    src_name = source or filename
    tmp_path: Path | None = None
    try:
        # 1) 安全嗅探 + 按映射读结构化行（含 ledger:sheet:row 行回链）
        rows = excel_service.read_ledger_rows(content, suffix, mapping_obj)
        if not rows:
            raise excel_service.LedgerError("台账无有效数据行（全部为空或未映射列）")
        # 2) 原文件 → 企业私有证据材料（evidence_file），内容重复幂等复用既有版本
        material_id = f"MAT-LEDGER-{kind[:8].upper()}-{uuid.uuid4().hex[:10]}"
        tmp_path = _write_tmp(content, suffix)
        imported = material_service.import_material(
            session,
            src_path=str(tmp_path),
            material_id=material_id,
            material_type="evidence_file",
            source_type="uploaded",
            owner_type="enterprise",
            classification="internal",
            permission_scope="enterprise_read",
            data_owner=data_owner,
            store_root=object_store_root(),
            project_id=project_id,
            actor=actor,
        )
        # 3) 结构化行 → 既有导入服务（幂等；行级默认 pending_verification）
        if kind == "qualifications":
            result = enterprise_service.import_qualifications(
                session, rows, data_owner=data_owner, actor=actor,
                source=src_name, material_id=imported.material.material_id)
        elif kind == "performances":
            result = enterprise_service.import_performances(
                session, rows, data_owner=data_owner, actor=actor,
                source=src_name, material_id=imported.material.material_id)
        elif kind == "personnel":
            result = enterprise_service.import_personnel(
                session, rows, category=category,
                data_owner=data_owner, actor=actor, source=src_name)
        else:  # managers
            result = enterprise_service.import_managers(
                session, rows, data_owner=data_owner, actor=actor, source=src_name)
        api_service.audit(session, actor=actor, action="enterprise.ledger.commit",
                          basis=f"kind={kind} source={src_name} material={imported.material.material_id}",
                          outcome=f"rows={len(rows)} created={result['created']} skipped={result['skipped']}",
                          object_ref=material_id)
        session.commit()
    except excel_service.LedgerError as exc:
        session.rollback()
        raise ApiError("unsupported_format", str(exc))
    except MaterialValidationError as exc:
        session.rollback()
        raise ApiError("invalid_request", str(exc))
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    return {
        "request_id": request_id,
        "kind": kind,
        "source": src_name,
        "rows": len(rows),
        "material": {
            "material_id": imported.material.material_id,
            "version": imported.material.version,
            "created": imported.created,
        },
        "import": result,
    }


def _write_tmp(content: bytes, suffix: str) -> Path:
    """写入临时文件（commit 流程结束后清理；异常路径无业务残留）。"""
    tmp = Path(__file__).resolve().parent.parent / ".tmp-ledger-uploads"
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / f"{uuid.uuid4().hex}{suffix or '.bin'}"
    path.write_bytes(content)
    return path
