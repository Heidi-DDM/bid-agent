# F020：材料 API（POST/GET /api/v1/materials、/{id}/versions、/{id}/verify）
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

from runtime.routers.deps import get_actor, get_db, get_role, get_request_id, require_role
from runtime.core.config import object_store_root
from runtime.core.errors import ApiError
from runtime.db import api_service, material_service

router = APIRouter(prefix="/api/v1", tags=["materials"])


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
) -> dict:
    """导入材料（F020 §2.2.5 / F019 §4）。核验前 status=pending_verification。"""
    require_role(role, "material", "write", session=session, actor=actor, object_ref=material_id)

    # 校验必填字段
    if not data_owner or not data_owner.strip():
        raise ApiError("invalid_request", "data_owner 必填（数据责任人制，F003 §4.1）")
    try:
        from datetime import date

        valid_date = date.fromisoformat(valid_until) if valid_until else None
    except ValueError:
        raise ApiError("invalid_request", "valid_until 必须为合法日期（YYYY-MM-DD）")

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

    return {
        "request_id": request_id,
        "material_id": imported.material.material_id,
        "version": imported.material.version,
        "content_hash": imported.material.content_hash,
        "created": imported.created,
        "duplicate_of": imported.duplicate_of,
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
