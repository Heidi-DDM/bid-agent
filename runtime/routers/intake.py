# F020：搜索/推送 + 入库 API
# POST /api/v1/intake/announcement/search（测试期 manual_trigger，只保存公开事实，不触发解析/匹配/准入）
# GET  /api/v1/intake/announcement/search/{job_id}
# POST /api/v1/intake/announcement
# POST /api/v1/intake/tender-document（multipart 上传完整招标文件，绑定 project_id；未上传完整文件不建解析任务）
# GET  /api/v1/intake/{id}
# POST /api/v1/intake/{id}/reparse
from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from sqlalchemy.orm import Session

from runtime.routers.deps import get_actor, get_db, get_role, get_request_id, require_role
from runtime.core.config import object_store_root
from runtime.core.errors import ApiError
from runtime.db import api_service, material_service, worker_service
from runtime.db.models import AnalysisJob, Project

router = APIRouter(prefix="/api/v1/intake", tags=["intake"])

# 招标文件可解析扩展名（F005：docx/pdf；.gef/.etb 不支持 -> unsupported_format）
TENDER_SUFFIXES = {".docx", ".pdf"}
FORBIDDEN_TENDER_SUFFIXES = {".gef", ".etb"}


@router.post("/announcement/search")
async def search_announcement(
    request: Request,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """搜索与结构化推送（F020 §2.2.1）：只保存公开公告事实，不触发解析/匹配/准入。

    测试期仅 manual_trigger（F004 采集红线）；创建异步 job，轮询 GET .../search/{job_id}。
    """
    require_role(role, "announcement", "write", session=session, actor=actor)
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise ApiError("invalid_request", "请求体必须是合法 JSON")
    else:
        payload = dict(await request.form())
    keyword = str(payload.get("keyword") or "")
    region = str(payload.get("region") or "")
    collect_mode = str(payload.get("collect_mode") or "manual_trigger")
    if collect_mode != "manual_trigger":
        raise ApiError("invalid_request", "测试期仅支持 manual_trigger（F004 红线）")
    if not keyword and not region:
        raise ApiError("invalid_request", "keyword 或 region 至少填一个")

    project_id = f"PJ-{uuid.uuid4().hex[:10]}"
    project_name = keyword.strip() or f"公告搜索-{region}"
    api_service.create_project(session, project_id=project_id, project_name=project_name, actor=actor)

    job, created = worker_service.create_job(
        session, kind="announcement.search", input_ref=project_id, project_id=project_id
    )
    return {"request_id": request_id, "job_id": job.job_id, "project_id": project_id, "created": created}


@router.get("/announcement/search/{job_id}")
def search_announcement_status(
    job_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """轮询搜索结果（F020 §2.2.1）。"""
    require_role(role, "announcement", "read", session=session, actor=role, object_ref=job_id)
    from runtime.db.models import AnalysisJob

    job = session.get(AnalysisJob, job_id)
    if job is None:
        raise ApiError("not_found", f"任务不存在: {job_id}")
    items: list[dict] = []
    if job.status == "completed":
        project = session.get(Project, job.project_id) if job.project_id else None
        if project is not None:
            items = [api_service.project_card(session, project)]
    return {
        "request_id": request_id,
        "status": job.status,
        "items": items,
        "error_code": job.error_code,
    }


@router.post("/announcement")
def intake_announcement(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
    file: UploadFile = File(...),
    project_id: str = Form(...),
    material_id: str = Form(...),
    source_type: str = Form("manual_entry"),
    data_owner: str = Form(...),
) -> dict:
    """公告原文入库（公开事实，raw 不可覆盖；F020 §2.2 / F004）。"""
    require_role(role, "announcement", "write", session=session, actor=actor, object_ref=project_id)
    if not data_owner or not data_owner.strip():
        raise ApiError("invalid_request", "data_owner 必填")
    content = file.file.read()
    if not content:
        raise ApiError("invalid_request", "空文件：未上传公告原文")

    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.gettempdir()) / f"{uuid.uuid4().hex}.pdf"
    tmp.write_bytes(content)
    try:
        imported = material_service.import_material(
            session,
            src_path=str(tmp),
            material_id=material_id,
            material_type="announcement",
            source_type=source_type,
            owner_type="public",
            classification="public",
            permission_scope="public_read",
            data_owner=data_owner,
            store_root=object_store_root(),
            project_id=project_id,
            actor=actor,
        )
    except material_service.ValidationError as exc:
        raise ApiError("invalid_request", str(exc))
    finally:
        tmp.unlink(missing_ok=True)
    return {
        "request_id": request_id,
        "material_id": imported.material.material_id,
        "version": imported.material.version,
        "content_hash": imported.material.content_hash,
        "created": imported.created,
    }


@router.post("/tender-document")
def intake_tender_document(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
    file: UploadFile = File(...),
    project_id: str = Form(...),
    material_id: str = Form(...),
    source_type: str = Form("uploaded"),
    data_owner: str = Form(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """上传完整招标文件（docx/pdf）并创建解析任务（F020 §2.2.2）。

    前置校验：未上传完整文件（缺 file 或空文件）-> 400 invalid_request，不创建解析任务；
    .gef/.etb -> 415 unsupported_format；sha256 重复 -> 200 返回既有版本（created=false）。
    """
    require_role(role, "tender_document", "write", session=session, actor=actor, object_ref=project_id)
    if not data_owner or not data_owner.strip():
        raise ApiError("invalid_request", "data_owner 必填")
    filename = (file.filename or "").lower()
    suffix = Path(filename).suffix if filename else ""
    if suffix in FORBIDDEN_TENDER_SUFFIXES:
        raise ApiError("unsupported_format", f"不支持加密固化格式: {suffix}（F005 §约束）")
    if suffix not in TENDER_SUFFIXES:
        raise ApiError("unsupported_format", "仅支持 docx/pdf 招标文件")

    content = file.file.read()
    if not content:
        raise ApiError("invalid_request", "空文件：未上传完整招标文件")
    if not idempotency_key or not idempotency_key.strip():
        raise ApiError("invalid_request", "Idempotency-Key 请求头必填")

    import tempfile

    tmp = Path(tempfile.gettempdir()) / f"{uuid.uuid4().hex}{suffix}"
    tmp.write_bytes(content)
    try:
        imported = material_service.import_material(
            session,
            src_path=str(tmp),
            material_id=material_id,
            material_type="tender_document",
            source_type=source_type,
            owner_type="public",
            classification="public",
            permission_scope="public_read",
            data_owner=data_owner,
            store_root=object_store_root(),
            project_id=project_id,
            actor=actor,
        )
    except material_service.ValidationError as exc:
        raise ApiError("invalid_request", str(exc))
    finally:
        tmp.unlink(missing_ok=True)

    # 幂等：sha256 重复（created=false）不重复创建解析任务（F020 §2.2.2）
    if imported.created:
        job, _ = worker_service.create_job(
            session,
            kind="parse.tender_document",
            input_ref=material_id,
            project_id=project_id,
            idempotency_key=f"parse.tender_document:{idempotency_key.strip()}",
        )
        job_id = job.job_id
    else:
        from sqlalchemy import select

        existing = session.scalar(
            select(AnalysisJob).where(
                AnalysisJob.idempotency_key == f"parse.tender_document:{idempotency_key.strip()}"
            )
        )
        job_id = existing.job_id if existing else None
    return {
        "request_id": request_id,
        "job_id": job_id,
        "material_id": imported.material.material_id,
        "version": imported.material.version,
        "content_hash": imported.material.content_hash,
        "created": imported.created,
        "idempotency_key": idempotency_key,
    }


@router.get("/{intake_id}")
def intake_status(
    intake_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """入库任务状态（status + parse_status，F020 §2.2.2）。"""
    require_role(role, "tender_document", "read", session=session, actor=role, object_ref=intake_id)
    job = session.get(AnalysisJob, intake_id)
    if job is None:
        # 兼容按 material_id 查询
        material = api_service.get_material_or_404(session, intake_id)
        return {
            "request_id": request_id,
            "id": intake_id,
            "status": "completed" if material.parse_status != "pending" else "pending",
            "parse_status": material.parse_status,
        }
    material = None
    if job.input_ref:
        material = api_service.get_material_or_404(session, job.input_ref)
    return {
        "request_id": request_id,
        "id": intake_id,
        "status": job.status,
        "parse_status": material.parse_status if material else "pending",
        "error_code": job.error_code,
    }


@router.post("/{intake_id}/reparse")
def reparse(
    intake_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """重新解析（F020 §2.2）：仅已入库材料可重解析；创建新任务并轮询。"""
    require_role(role, "tender_document", "write", session=session, actor=role, object_ref=intake_id)
    material = api_service.get_material_or_404(session, intake_id)
    job, created = worker_service.create_job(
        session, kind="parse.tender_document", input_ref=material.material_id,
        project_id=material.project_id,
    )
    return {"request_id": request_id, "job_id": job.job_id, "created": created}
