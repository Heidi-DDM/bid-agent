# F020：材料 API（POST/GET /api/v1/materials、/{id}/versions、/{id}/verify）
from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

from runtime.routers.deps import get_actor, get_db, get_role, get_request_id, require_role
from runtime.core.config import object_store_root
from runtime.core.errors import ApiError
from runtime.db import api_service, material_service

router = APIRouter(prefix="/api/v1", tags=["materials"])

# v1.7（09-优化方案 §3.2.3/§3.5.1）：材料上传入口分离——
# 单份证明（evidence_file）仅接受 .pdf/.docx（图片待 OCR 方案确认后开放）；
# Excel/CSV 台账必须走企业资料受控导入（/enterprise/import/preview + commit，
# 类型选择→预览→字段映射→原文不可变落库），不得作为不可见附件直接入库。
EVIDENCE_SUFFIXES = {".pdf", ".docx"}
MATERIAL_MAX_BYTES = 100 * 1024 * 1024
# 补录材料字段枚举（资质/业绩/人员/项目经理/财务信用/项目专用证明/其他）——展示与审计用，非封闭字典
MATERIAL_SUBTYPES = ("qualification", "performance", "personnel", "manager",
                     "finance_credit", "project_specific", "other")


@router.post("/materials")
def create_material(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
    file: UploadFile = File(...),
    material_id: str = Form(...),
    material_type: str = Form(...),
    source_type: str = Form("uploaded"),
    owner_type: str = Form("enterprise"),
    classification: str = Form("internal"),
    permission_scope: str = Form("enterprise_read"),
    data_owner: str = Form(...),
    project_id: str | None = Form(default=None),
    valid_until: str | None = Form(default=None),
    material_subtype: str | None = Form(default=None),
    intake_mode: str | None = Form(default=None),
    requirement_ids: str | None = Form(default=None),
) -> dict:
    """导入材料（F020 §2.2.5 / F019 §4）。核验前 status=pending_verification。

    v1.7（09-优化方案 §3.2.3/§3.4.4/§3.5.1）：
    - 入口 accept 分离：本端点=单份补录证明（.pdf/.docx）；xlsx/xls/csv 拒绝并指路
      企业资料受控导入（禁止“不可见附件”直接入库）；
    - 可选字段 material_subtype/intake_mode/requirement_ids[]：requirement_ids
      回链缺失要求（落 evidence_refs，前缀 requirement:），禁止无语义默认 ID；
    - 魔数嗅探（PDF=%PDF、DOCX=PK）+ 大小上限校验。
    """
    require_role(role, "material", "write", session=session, actor=actor, object_ref=material_id)

    if not data_owner or not data_owner.strip():
        raise ApiError("invalid_request", "data_owner 必填（数据责任人制，F003 §4.1）")
    if material_subtype and material_subtype not in MATERIAL_SUBTYPES:
        raise ApiError("invalid_request",
                       f"material_subtype 不在可选范围: {material_subtype}"
                       f"（可选: {MATERIAL_SUBTYPES}）")
    if intake_mode and intake_mode not in ("supplement_evidence", "enterprise_ledger"):
        raise ApiError("invalid_request",
                       "intake_mode 仅支持 supplement_evidence（单份补录证明）/enterprise_ledger（台账资料）")
    req_ids: list[str] = []
    if requirement_ids:
        try:
            parsed = json.loads(requirement_ids)
            if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
                raise ValueError
            req_ids = parsed
        except (json.JSONDecodeError, ValueError):
            raise ApiError("invalid_request", "requirement_ids 必须是 JSON 字符串数组")
    try:
        from datetime import date

        valid_date = date.fromisoformat(valid_until) if valid_until else None
    except ValueError:
        raise ApiError("invalid_request", "valid_until 必须为合法日期（YYYY-MM-DD）")

    filename = (file.filename or "").lower()
    suffix = Path(filename).suffix if filename else ""
    if suffix in (".xlsx", ".xls", ".csv"):
        raise ApiError(
            "unsupported_format",
            f"{suffix} 属台账/清单资料：请走企业资料受控导入（选择资料类型 → 预览 → 字段映射），"
            "不作为不可见附件直接入库（F022 §5.1 v1.3）",
        )
    if suffix not in EVIDENCE_SUFFIXES:
        raise ApiError("unsupported_format",
                       f"单份补录证明仅支持 {sorted(EVIDENCE_SUFFIXES)}（图片证据待 OCR 方案确认后开放）")

    # 保存上传文件到临时目录（F018 §4.3：临时目录 -> 校验 -> 原子移动）
    tmp_dir = Path(object_store_root()).parent / ".tmp-uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    src = tmp_dir / f"{uuid.uuid4().hex}.pdf"
    try:
        content = file.file.read()
    except Exception:
        raise ApiError("invalid_request", "读取上传文件失败")
    if not content:
        src.unlink(missing_ok=True)
        raise ApiError("invalid_request", "空文件：未上传完整材料")
    if len(content) > MATERIAL_MAX_BYTES:
        src.unlink(missing_ok=True)
        raise ApiError("file_too_large",
                       f"材料超过上限 {MATERIAL_MAX_BYTES // (1024 * 1024)} MB（F020 v1.7）")
    magic = content[:8]
    if suffix == ".pdf" and not magic.startswith(b"%PDF"):
        src.unlink(missing_ok=True)
        raise ApiError("unsupported_format", "文件内容不是 PDF（魔数校验失败），请确认文件完整未损坏")
    if suffix == ".docx" and not magic.startswith(b"PK"):
        src.unlink(missing_ok=True)
        raise ApiError("unsupported_format", "文件内容不是 DOCX（魔数校验失败），请确认文件完整未损坏")
    src.write_bytes(content)

    try:
        imported = material_service.import_material(
            session,
            src_path=str(src),
            material_id=material_id,
            material_type=material_type,
            source_type=source_type,
            owner_type=owner_type,
            classification=classification,
            permission_scope=permission_scope,
            data_owner=data_owner,
            store_root=object_store_root(),
            project_id=project_id,
            valid_until=valid_date,
            actor=actor,
        )
    except material_service.ValidationError as exc:
        raise ApiError("invalid_request", str(exc))
    except material_service.HashMismatchBlocked as exc:
        raise ApiError("hash_mismatch", str(exc))
    finally:
        src.unlink(missing_ok=True)

    # 补录回链：requirement_ids 前缀 requirement: 写入 evidence_refs（原始引用保留）
    if req_ids and imported.created:
        material = imported.material
        refs = list(material.evidence_refs or [])
        refs.extend(f"requirement:{rid}" for rid in req_ids)
        material.evidence_refs = refs
        api_service.audit(
            session, actor=actor, action="material.supplement.linked",
            basis=f"material={material.material_id} subtype={material_subtype or 'other'} "
                  f"mode={intake_mode or 'supplement_evidence'}",
            outcome=f"requirements={req_ids}", object_ref=project_id or material.material_id,
        )
        session.commit()

    return {
        "request_id": request_id,
        "material_id": imported.material.material_id,
        "version": imported.material.version,
        "content_hash": imported.material.content_hash,
        "created": imported.created,
        "duplicate_of": imported.duplicate_of,
        "requirement_ids": req_ids,
    }


@router.get("/materials/{material_id}")
def get_material(
    material_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """材料详情 + 版本历史（按角色权限过滤，F020 §2.2.5）。"""
    detail = api_service.material_detail(session, material_id, role)
    detail["request_id"] = request_id
    return detail


@router.get("/materials/{material_id}/versions")
def get_material_versions(
    material_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """版本列表（F020 §2.2 / F003 §8）。"""
    # 先做一次角色可见性校验（企业明细不对外）
    material = api_service.get_material_or_404(session, material_id)
    api_service.require_scope_or_403(session, role, material)
    versions = api_service.list_material_versions(session, material_id)
    return {"request_id": request_id, "items": versions}


@router.post("/materials/{material_id}/verify")
def verify_material(
    material_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """核验通过（actor、data_owner 必填）-> status=active，写审计（F020 §2.2.5）。"""
    require_role(role, "enterprise", "verify", session=session, actor=actor, object_ref=material_id)
    material = api_service.get_material_or_404(session, material_id)
    if material.owner_type != "enterprise":
        raise ApiError("invalid_state_transition", "仅企业资料可核验")
    updated = material_service.update_metadata(
        session, material_id, verified_at=api_service._now_datetime(), actor=actor
    )
    if updated is None:
        raise ApiError("not_found", f"材料不存在: {material_id}")
    return {"request_id": request_id, "material_id": material_id,
            "status": "active", "verified_at": updated.verified_at.isoformat() if updated.verified_at else None}
