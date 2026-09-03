# F020：搜索/推送 + 入库 API
# POST /api/v1/intake/announcement/search（测试期 manual_trigger，真实抓取列表→候选落库）
# GET  /api/v1/intake/announcement/search/{job_id}（轮询：候选公告卡片 + 逐源结果摘要）
# POST /api/v1/intake/announcement/candidates/{candidate_id}/import（候选确认→详情原文入库）
# GET  /api/v1/intake/announcement/candidates/{candidate_id}（候选/导入状态）
# POST /api/v1/intake/announcement
# POST /api/v1/intake/tender-document（multipart 上传完整招标文件，绑定 project_id；未上传完整文件不建解析任务）
# GET  /api/v1/intake/{id}
# POST /api/v1/intake/{id}/reparse
from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime.collecting import registry as source_registry
from runtime.routers.deps import get_actor, get_db, get_role, get_request_id, require_role
from runtime.core.config import object_store_root
from runtime.core.errors import ApiError
from runtime.db import api_service, material_service, worker_service
from runtime.db.models import AnalysisJob, AnnouncementCandidate

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
    """搜索公告（F020 §2.2.1）：真实抓取已冻结 L1 平台列表页 → 候选落库。

    测试期仅 manual_trigger（F004 采集红线）；创建异步 job 由 worker 执行
    （robots 预检 + 单源限频 ≤1 次/5 分钟 + 透明 UA），轮询 GET .../search/{job_id}
    获取候选；候选确认入库走 candidates/{id}/import。搜索本身不创建 Project、
    不触发解析/匹配/准入（候选确认入库后才建档）。
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
    keyword = str(payload.get("keyword") or "").strip()
    region = str(payload.get("region") or "").strip() or None
    collect_mode = str(payload.get("collect_mode") or "manual_trigger")
    if collect_mode != "manual_trigger":
        raise ApiError("invalid_request", "测试期仅支持 manual_trigger（F004 红线）")
    if not keyword and not region:
        raise ApiError("invalid_request", "keyword 或 region 至少填一个")

    # sources：JSON 数组或逗号分隔字符串；未注册/未启用源如实拒绝（清单 §2 以外不接入）
    raw_sources = payload.get("sources") or []
    if isinstance(raw_sources, str):
        raw_sources = [s.strip() for s in raw_sources.split(",") if s.strip()]
    if not isinstance(raw_sources, list):
        raise ApiError("invalid_request", "sources 必须是数组或逗号分隔字符串")
    raw_sources = [str(s).strip() for s in raw_sources if str(s).strip()]
    valid, unknown = source_registry.validate_sources(raw_sources)
    if unknown:
        raise ApiError("invalid_request",
                       f"未注册/未启用数据源: {unknown}（docs/合规数据源清单.md §2 以外平台不接入）")

    job, created = worker_service.create_job(
        session,
        kind="announcement.search",
        input_ref=json.dumps({"keyword": keyword, "region": region, "sources": valid},
                             ensure_ascii=False),
        project_id=None,
    )
    return {"request_id": request_id, "job_id": job.job_id, "created": created}


def _candidate_card(c: AnnouncementCandidate) -> dict:
    """候选公告卡片（对齐 F020 §2.2.1 公告事实卡字段；列表页未标注字段=null 待补）。"""
    return {
        "candidate_id": c.candidate_id,
        "project_id": c.project_id,
        "project_name": c.title,
        "state": "collecting",
        "region": c.region,
        "project_type": c.category,
        "budget_cap": None,
        "bond": None,
        "bid_validity": None,
        "deadline": c.publish_date.isoformat() if c.publish_date else None,
        "source": c.source_name,
        "source_url": c.url,
        "parse_status": "pending",
        "tender_file": None,
        "updated": None,
        "import_status": c.import_status,
        "note": "候选公告（列表页事实），待投标专员确认后抓详情原文入库",
    }


@router.get("/announcement/search/{job_id}")
def search_announcement_status(
    job_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """轮询搜索结果（F020 §2.2.1）：候选公告卡片数组 + 逐源结果摘要。"""
    require_role(role, "announcement", "read", session=session, actor=role, object_ref=job_id)
    job = session.get(AnalysisJob, job_id)
    if job is None:
        raise ApiError("not_found", f"任务不存在: {job_id}")
    candidates: list[dict] = []
    if job.status == "completed":
        rows = session.scalars(
            select(AnnouncementCandidate)
            .where(AnnouncementCandidate.search_job_id == job_id)
            .order_by(AnnouncementCandidate.created_at)
        ).all()
        candidates = [_candidate_card(c) for c in rows]
    return {
        "request_id": request_id,
        "status": job.status,
        "items": candidates,
        "summary": job.result_summary if isinstance(job.result_summary, list) else None,
        "error_code": job.error_code,
    }


@router.post("/announcement/candidates/{candidate_id}/import")
def import_candidate(
    candidate_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """候选公告确认入库（F020 §2.2.1 扩展）：投递 announcement.import_detail 任务。

    worker 执行 robots 预检 + 限频 → 抓详情原文 → 原文固化（Material，raw 不可覆盖）
    → Project 建档。同源 5 分钟内仅可抓 1 次（红线），紧邻搜索后导入会触发
    限频重试，属合规预期行为。
    """
    require_role(role, "announcement", "write", session=session, actor=actor,
                 object_ref=candidate_id)
    candidate = session.get(AnnouncementCandidate, candidate_id)
    if candidate is None:
        raise ApiError("not_found", f"候选公告不存在: {candidate_id}")
    if candidate.import_status == "imported":
        raise ApiError("invalid_request",
                       f"候选公告已入库 project_id={candidate.project_id}（raw 不可覆盖，不重复导入）")
    # 幂等：同候选不重复建任务；failed 重试追加序号（避免幂等键命中失败任务）
    existing_count = len(session.scalars(
        select(AnalysisJob).where(
            AnalysisJob.kind == "announcement.import_detail",
            AnalysisJob.input_ref == candidate_id,
        )
    ).all())
    job, _ = worker_service.create_job(
        session,
        kind="announcement.import_detail",
        input_ref=candidate_id,
        project_id=None,
        idempotency_key=f"announcement.import_detail:{candidate_id}:{existing_count}",
    )
    candidate.import_job_id = job.job_id
    candidate.requested_by = actor
    session.commit()
    return {"request_id": request_id, "job_id": job.job_id, "candidate_id": candidate_id}


@router.get("/announcement/candidates/{candidate_id}")
def candidate_status(
    candidate_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """候选公告与导入状态（轮询 import 任务结果）。"""
    require_role(role, "announcement", "read", session=session, actor=role, object_ref=candidate_id)
    candidate = session.get(AnnouncementCandidate, candidate_id)
    if candidate is None:
        raise ApiError("not_found", f"候选公告不存在: {candidate_id}")
    job_status = None
    if candidate.import_job_id:
        job = session.get(AnalysisJob, candidate.import_job_id)
        job_status = job.status if job else None
    card = _candidate_card(candidate)
    card["job_status"] = job_status
    card["error_message"] = candidate.error_message
    card["requested_by"] = candidate.requested_by
    return {"request_id": request_id, "candidate": card}


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
