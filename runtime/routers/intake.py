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
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, Form, Header, Request, UploadFile
from pydantic import BaseModel, Field
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime.collecting import registry as source_registry
from runtime.collecting.announcement_type import (
    TYPE_LABELS as ATYPE_LABELS,
    classify as atype_classify,
    classify_type,
    dedupe_by_project,
)
from runtime.core.compliance import get_collection_policy, normalize_url as _normalize_url, ComplianceError
from runtime.routers.deps import get_actor, get_db, get_role, get_request_id, require_role
from runtime.core.config import object_store_root
from runtime.core.errors import ApiError
from runtime.db import api_service, material_service, worker_service
from runtime.db.models import AnalysisJob, AnnouncementCandidate, MaterialVersion

router = APIRouter(prefix="/api/v1/intake", tags=["intake"])

# 招标文件可解析扩展名（F005：docx/pdf；.gef/.etb 不支持 -> unsupported_format）
TENDER_SUFFIXES = {".docx", ".pdf"}
FORBIDDEN_TENDER_SUFFIXES = {".gef", ".etb"}
# v1.7（09-优化方案 §3.2.3）：招标文件大小上限 100 MB（解析/原文固化上限，超限明确拒绝）
TENDER_MAX_BYTES = 100 * 1024 * 1024

# Agent 聊天等人工线索转登记候选的固定来源标识（人工转录，不属合规清单源；
# 后续「选择深入」仍走 robots + 限频 + 详情原文固化，不因来源放宽合规）
MANUAL_SOURCE_ID = "agent_manual"
MANUAL_JOB_KIND = "announcement.manual_entry"


class ManualCandidateBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=500, description="线索标题（Agent 聊天内容人工转录）")
    url: str = Field(..., min_length=1, max_length=500, description="公告原始链接（仅 http/https）")
    note: str | None = Field(None, max_length=500, description="人工备注（来源/时间等，如 Agent 会话日期）")


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
    try:
        policy = get_collection_policy()
    except ComplianceError as exc:
        raise ApiError("invalid_request", str(exc))
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
    # F026/ADR-005：触发方式由运行时 COLLECTION_POLICY 与调度器控制；API 不再把
    # scheduled 硬编码为 Schema 红线。无调度器时 scheduled 仍只是可审计任务类型。
    if collect_mode not in {"manual_trigger", "scheduled"}:
        raise ApiError("invalid_request", "collect_mode 仅支持 manual_trigger 或 scheduled")
    if not keyword and not region:
        raise ApiError("invalid_request", "keyword 或 region 至少填一个")
    # v1.7（09-优化方案 §3.1）：联网搜索只定义采集范围（关键词/地区/来源）；
    # 「种类」「规模」是搜索完成后的候选列表内筛选，不作为搜索前过滤条件——
    # 请求若仍携带 category/scale（旧前端），一律忽略且不写入任务，不缩小联网检索范围。
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

    # 搜索幂等按 5 分钟限频窗口分桶：窗口内重复点击复用同一任务；
    # 窗口结束后允许重新创建任务，由合规限频器再次决定是否可抓取。
    # 这样限频拒绝不会把同一搜索永久锁死，同时不提供绕过窗口的通道。
    retry_bucket = int(time.time() // 300)
    search_key = f"announcement.search:{keyword}:{region or ''}:{','.join(valid)}:{retry_bucket}"
    job, created = worker_service.create_job(
        session,
        kind="announcement.search",
        input_ref=json.dumps({"keyword": keyword, "region": region, "collect_mode": collect_mode,
                              "sources": valid},
                             ensure_ascii=False),
        project_id=None,
        idempotency_key=search_key,
    )
    return {"request_id": request_id, "job_id": job.job_id, "created": created,
            "environment": policy.name, "data_sources": valid}


def _candidate_card(c: AnnouncementCandidate, *, region_recall: bool = False) -> dict:
    """候选公告卡（v1.7 DTO，09-优化方案 §3.1.3；announcement_type 由标题确定性分类）。

    列表页可确证事实：title/publish_date/source_name/source_url/source_category
    （来源分区，非逐条工程种类）；project_type/scale 列表页未标注 → null=待详情
    回源确认，禁止推断；fact_status=list_fact_only；missing_fields[] 如实列出。

    C4/D1（2026-09-10）：region/project_type 标题确定性抽取——
    - region 优先级：详情回填（announcement_fact）> detail_summary.region
      （announcement_fact）> 标题命中市/县名（title_fact）> None（source_scope_only）；
    - project_type：标题命中 施工/EPC总承包/监理/设计/勘察/货物/服务 → 填值
      （provenance=标题）；未命中 → None 待详情；
    - missing_fields 相应收缩（不再恒缺 project_type）。

    ``region`` 只表示公告正文/详情页已经确认的项目实际地区；
    ``source_region_scope`` 是注册表声明的平台覆盖范围，不能冒充项目地区。
    ``region_provenance`` 用于区分来源范围与公告事实，避免前端把二者混用。
    """
    from runtime.collecting.hebei_regions import region_from_title
    from runtime.collecting.registry import project_type_from_title

    source_spec = source_registry.get(c.source_id)
    source_region_scope = source_spec.region_scope if source_spec else None
    detail_region = None
    detail_scale = None
    if c.detail_summary:
        f = c.detail_summary.get("region")
        if isinstance(f, dict) and not f.get("missing") and f.get("value"):
            detail_region = str(f["value"])
        # 2026-09-10：详情「建设规模」抽取 → 回填卡片「规模」（公告事实，此前恒待确认）
        fs = c.detail_summary.get("scale")
        if isinstance(fs, dict) and not fs.get("missing") and fs.get("value"):
            detail_scale = str(fs["value"])
    if c.region:
        region, region_provenance = c.region, "announcement_fact"
    elif detail_region:
        region, region_provenance = detail_region, "announcement_fact"
    else:
        title_region = region_from_title(c.title)
        if title_region:
            region, region_provenance = title_region, "title_fact"
        else:
            region = None
            region_provenance = "source_scope_only" if source_region_scope else None
    project_type = project_type_from_title(c.title)
    missing: list[str] = []
    if not region or region_provenance == "source_scope_only":
        missing.append("region")
    if not c.publish_date:
        missing.append("publish_date")
    if not project_type:
        missing.append("project_type")  # 标题未点名种类 → 如实待详情
    if not detail_scale:
        missing.append("scale")
    a_type = classify_type(c.title)   # 标题确定性分类（纯事实，不推断）
    return {
        "candidate_id": c.candidate_id,
        "source_id": c.source_id,
        "project_id": c.project_id,
        "title": c.title,
        "announcement_type": a_type,
        "type_label": ATYPE_LABELS.get(a_type, a_type),
        "publish_date": c.publish_date.isoformat() if c.publish_date else None,
        "source_name": c.source_name,
        "source_url": c.url,
        "source_category": c.category,
        "region": region,
        "source_region_scope": source_region_scope,
        "region_inferred": False,
        "region_provenance": region_provenance,
        "project_type": project_type,
        "project_type_provenance": "标题" if project_type else None,
        "scale": detail_scale,
        "scale_provenance": "公告原文" if detail_scale else None,
        "fact_status": "list_fact_only",
        "missing_fields": missing,
        "region_recall": region_recall,
        "import_status": c.import_status,
        "error_message": c.error_message,
        # 2026-09-09：详情初筛抽取结果（import 后由 announcement_prescreen 填充）。
        #   候选卡/详情卡据此展示资质/人员/信用/金额等；空则前端继续用 missing 语义
        "detail_summary": c.detail_summary if c.detail_summary else None,
    }


def _job_region(job: AnalysisJob | None) -> str | None:
    """任务检索地区（input_ref JSON 的 region 字段；人工登记任务无 region）。"""
    if job is None:
        return None
    try:
        params = json.loads(job.input_ref or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    return (str(params.get("region") or "").strip()) or None


def _candidate_region_recall(c: AnnouncementCandidate, region: str | None) -> bool:
    """候选地区是否「待核实」（C1 三态口径：broader/none 均属待核实）。

    源未注册（人工线索 agent_manual）或检索地区为空 → False；
    - broader：来源为省/全国平台，公告可能来自检索地区也可能不是（列表页不标注）；
    - none：检索地区超源覆盖（放宽二次召回所得）。
    两种情况候选逐条地区都不推断，交详情页回源；前端文案由 region_note 区分。
    """
    if not region:
        return False
    spec = source_registry.get(c.source_id)
    if spec is None:
        return False
    return spec.region_coverage(region) != "direct"


def _candidate_region_note(c: AnnouncementCandidate, region: str | None) -> str | None:
    """候选地区待核实的区分文案（C1/C2）：省/全国来源 vs 超范围放宽召回。"""
    if not region:
        return None
    spec = source_registry.get(c.source_id)
    if spec is None:
        return None
    coverage = spec.region_coverage(region)
    if coverage == "broader":
        scope_label = "全国平台" if spec.region_scope == "全国" else "省级平台"
        return (f"来源为{scope_label}（{spec.region_scope}），覆盖检索地区「{region}」→ "
                f"逐条公告地区待核实（列表页不标注，不推断归属，详情页回源确认）")
    if coverage == "none":
        return (f"检索地区「{region}」超出本候选来源覆盖（{spec.region_scope}）→ "
                f"放宽二次召回所得，地区待核实（列表页不标注，不推断归属，详情页回源确认）")
    return None


def _finalize_candidates(cards: list[dict], *, include_all: bool = False) -> list[dict]:
    """候选卡最终展示整理：默认只保留招标/资格预审类型 + 跨源去重。

    include_all=False（默认/用户验收期）：采购/中标/候选/变更/其他一律不展示
    （仅招标公告），符合用户「采购/中标/候选公示类默认全不显示」；
    include_all=True：展示全部类型，同样做跨项目归并（同项目多阶段只保留
    类型优先级最高的一条，并带 source_group / stages）。
    """
    if not include_all:
        kept, _ = atype_classify(cards)
    else:
        kept = [dict(c) for c in cards]
    return dedupe_by_project(kept)


def _policy_error() -> bool:
    """采集策略是否读取异常（COLLECTION_POLICY 无效→compliance fail-closed 抛异常）。

    供轮询/健康端点安全回退到 "unknown"，避免因策略配置问题让接口 500。
    """
    try:
        get_collection_policy()
        return False
    except Exception:
        return True


@router.get("/announcement/searches")
def list_search_jobs(
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """最近搜索任务列表（v1.7 / 09-优化方案 §3.1.7）。

    供搜索页默认展示最近任务并重新打开候选（候选不只放在临时 DOM）。
    常规 announcement.search 任务按创建时间倒序，附候选数与逐源摘要；
    Agent 人工线索登记（announcement.manual_entry）同列表展示（来源标识
    agent_manual，可打开候选走「选择深入」）。
    """
    require_role(role, "announcement", "read", session=session, actor=role)
    from sqlalchemy import func, select

    kinds = ["announcement.search", MANUAL_JOB_KIND]
    has_candidates = select(AnnouncementCandidate.candidate_id).where(
        AnnouncementCandidate.search_job_id == AnalysisJob.job_id
    ).exists()
    total = session.scalar(
        select(func.count()).select_from(AnalysisJob).where(
            AnalysisJob.kind.in_(kinds), AnalysisJob.deleted_at.is_(None), has_candidates
        )
    ) or 0
    rows = session.scalars(
        select(AnalysisJob)
        .where(AnalysisJob.kind.in_(kinds), AnalysisJob.deleted_at.is_(None), has_candidates)
        .order_by(AnalysisJob.created_at.desc())
        .limit(min(limit, 100)).offset(offset)
    ).all()
    items: list[dict] = []
    for job in rows:
        params: dict = {}
        try:
            params = json.loads(job.input_ref or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        count = session.scalar(
            select(func.count()).select_from(AnnouncementCandidate).where(
                AnnouncementCandidate.search_job_id == job.job_id
            )
        ) or 0
        is_manual = job.kind == MANUAL_JOB_KIND
        items.append({
            "job_id": job.job_id,
            "status": job.status,
            "keyword": params.get("title") if is_manual else params.get("keyword"),
            "region": params.get("region"),
            "sources": ["Agent 线索（人工转录）"] if is_manual else params.get("sources"),
            "manual": is_manual,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "summary": job.result_summary if isinstance(job.result_summary, list) else None,
            "candidates_count": count,
            "error_code": job.error_code,
        })
    return {"request_id": request_id, "items": items, "total": total,
            "limit": limit, "offset": offset}


@router.delete("/announcement/search/{job_id}")
def delete_search_job(
    job_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """软删除搜索历史（保留任务、候选和审计事实，不再显示在最近任务）。"""
    require_role(role, "announcement", "write", session=session, actor=actor, object_ref=job_id)
    job = session.get(AnalysisJob, job_id)
    if job is None or job.kind not in {"announcement.search", MANUAL_JOB_KIND}:
        raise ApiError("not_found", f"搜索任务不存在: {job_id}")
    if job.deleted_at is None:
        job.deleted_at = datetime.now(timezone.utc)
        api_service.audit(
            session,
            actor=actor,
            action="announcement.search.delete",
            basis="用户删除最近搜索任务（软删除，保留候选事实）",
            outcome="deleted",
            object_ref=job_id,
        )
        session.commit()
    return {"request_id": request_id, "job_id": job_id, "deleted": True}


@router.get("/announcement/search/{job_id}/candidates")
def list_search_candidates(
    job_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
    project_type: str | None = None,
    scale: str | None = None,
    include_all: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """对已保存候选的分页筛选（v1.7 / 09-优化方案 §3.1.5）。

    只过滤本任务已落库候选（GET .../search/{job_id}/candidates 的既有事实），
    不触发任何外网请求、不改变原搜索任务与候选事实。
    - announcement_type：按标题确定性分类；
    - project_type：按候选标题确定性关键词匹配（registry.category_matches 词表）；
    - scale：候选无规模事实（列表页不标注）→ 仅接受 unknown（=规模待确认组）；
      传入具体规模值返回空并在 note 如实说明，不推断。
    - include_all=False（默认）：只展示招标/资格预审公告；采购/中标/候选公示/变更/其他
      全部排除到后台（用户决策：默认仅显示招标公告）。
    """
    require_role(role, "announcement", "read", session=session, actor=role, object_ref=job_id)
    job = session.get(AnalysisJob, job_id)
    if job is None or job.deleted_at is not None:
        raise ApiError("not_found", f"任务不存在: {job_id}")
    q = select(AnnouncementCandidate).where(AnnouncementCandidate.search_job_id == job_id)
    note: list[str] = []
    rows = session.scalars(q.order_by(AnnouncementCandidate.created_at)).all()
    region = _job_region(job)
    items = [_candidate_card(c, region_recall=_candidate_region_recall(c, region)) for c in rows]
    for card, c in zip(items, rows):
        card["region_note"] = _candidate_region_note(c, region)
    if not include_all:
        items = _finalize_candidates(items, include_all=False)
        note.append("默认仅展示招标/资格预审公告；采购/中标/候选公示/变更/其他已按类型排除 "
                    "（?include_all=true 可查看全部类型）")
    else:
        items = _finalize_candidates(items, include_all=True)
    if rows and region and all(_candidate_region_recall(c, region) for c in rows):
        note.append(f"检索地区 {region!r} 超出本任务全部候选来源的覆盖范围 → 候选为放宽二次召回，"
                    "地区待核实（列表页不标注，不推断归属，详情页回源确认）")
    if project_type and project_type != "全部种类":
        from runtime.collecting.registry import category_matches

        items = [c for c in items if category_matches(project_type, c["title"])]
        note.append(f"种类={project_type}：按标题关键词确定性匹配（列表页不标注类别，不做推断）")
    if scale:
        if scale == "unknown" or scale == "规模待确认":
            note.append("规模：列表页不标注 → 候选规模均待详情页确认（不推断、不排除）")
        else:
            items = []
            note.append(f"规模={scale}：候选无该规模事实（列表页不标注），无法按此筛选，详情页回源确认")
    # —— 推送结果：两周内 + 发布日期降序（2026-09-09 用户需求；B2 2026-09-10 收紧）——
    #   默认列表严格满足「仅两周内」：发布日在最近 14 天（含）内；
    #   更早的不推送；发布日缺失(null)的候选不再默认展示——折叠为 date_pending_count
    #   计数返回（前端「N 条日期待确认，可展开」），不推断、不丢数据；
    #   有日期的按发布日期最新在上，无日期的排最后（展开组内）。
    two_weeks_ago = datetime.now(timezone.utc).date() - timedelta(days=14)
    for c in items:
        pd = c.get("publish_date")
        c["in_date_window"] = bool(pd) and pd >= two_weeks_ago.isoformat()
    items = [c for c in items if c["in_date_window"] or not c.get("publish_date")]
    items.sort(key=lambda c: c.get("publish_date") or "", reverse=True)
    date_pending_count = sum(1 for c in items if not c.get("publish_date"))
    if date_pending_count:
        note.append(f"{date_pending_count} 条候选列表页未载明发布日期 → 默认折叠不展示"
                    "（日期待详情页确认，不推断为两周内，也不丢弃）")
    total = len(items)
    items = items[offset:offset + limit]
    return {"request_id": request_id, "job_id": job_id, "items": items,
            "total": total, "note": note, "limit": limit, "offset": offset,
            "date_window": {"days": 14, "pending": date_pending_count}}


@router.get("/announcement/health")
def announcement_health(
    request_id: str = Depends(get_request_id),
    session: Session = Depends(get_db),
) -> dict:
    """搜索前健康检查（v1.7 / 09-优化方案 §3.1.8）。

    返回 API/数据库/worker/已启用数据源状态；worker 判定：近 60 秒有任务心跳 →
    available；从未有任务 → unknown（前端显示「任务已创建，等待处理服务」，
    不假装零结果）。不触发任何外网请求。

    F020 v1.7 契约：本端点与 /readyz 同级诊断，匿名可读（不返回业务数据，
    不携带 RBAC 门禁——业务角色均可查自身诊断，避免登录后反而 403 的伪失败）。
    """
    from datetime import datetime, timedelta, timezone

    db_ok = True
    db_error = None
    worker: dict = {"available": None, "reason": None}
    try:
        since = datetime.now(timezone.utc) - timedelta(seconds=60)
        last = session.scalar(
            select(AnalysisJob)
            .where(AnalysisJob.heartbeat_at.is_not(None))
            .order_by(AnalysisJob.heartbeat_at.desc())
            .limit(1)
        )
        if last is None or last.heartbeat_at is None:
            worker = {"available": None, "reason": "尚无任务心跳记录，无法判定 worker 是否运行"}
        elif last.heartbeat_at >= since:
            worker = {"available": True}
        else:
            worker = {"available": False,
                      "reason": f"最近心跳 {last.heartbeat_at.isoformat()} 早于 60 秒前，worker 疑似未运行"}
    except Exception as exc:  # 数据库不可达：如实报告，不伪装就绪
        db_ok = False
        db_error = f"{type(exc).__name__}: {exc}"
        worker = {"available": None, "reason": "数据库不可达，无法判定 worker"}
    sources = []
    from runtime.collecting import service as collecting_service
    from urllib.parse import urlparse as _urlparse

    for sid, s in source_registry.SOURCES.items():
        domain = _urlparse(s.base_url).netloc
        nxt = collecting_service.limiter_next_allowed(domain)
        sources.append({
            "source_id": sid, "name": s.name,
            # enabled=collectable：真实可采集公告源（惠招标等 verified），
            # 登记未实测/需合同的源(collectable=False)如实标 enabled=False，
            # 避免前端把"已登记"误当"已可采集"。registry 全量清单见 SOURCES。
            "enabled": s.collectable,
            "compliance_status": s.compliance_status,
            "region_scope": s.region_scope,
            # C1：地市源所属省份（省级检索据此覆盖地市源，前端预检与服务端同口径）
            "parent_region": s.parent_region,
            # 省级平台下辖行政区（石家庄市等市级检索同样视为覆盖，前端预检与服务端同口径）
            "admin_subregions": sorted(s.admin_subregions),
            "next_allowed_at": nxt.isoformat() if nxt else None,
        })
    return {
        "request_id": request_id,
        "api": "ok",
        "db": {"available": db_ok, "error": db_error},
        "worker": worker,
        "sources": sources,
        # C3（2026-09-10）：地区下拉由注册表生成——河北省 + 真有可采集源的地市；
        # 无源城市不出现（选了只会得到放宽召回的空结果）。
        "region_options": source_registry.region_options(),
    }


@router.get("/announcement/search/{job_id}")
def search_announcement_status(
    job_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
    include_all: bool = False,
) -> dict:
    """轮询搜索结果（F020 §2.2.1）：候选公告卡片数组 + 逐源结果摘要。

    include_all=False（默认）：只返回招标/资格预审公告（用户决策：采购/中标/候选
    公示默认不显示）；同项目跨源/跨阶段去重。include_all=true：返回全部类型。
    """
    require_role(role, "announcement", "read", session=session, actor=role, object_ref=job_id)
    job = session.get(AnalysisJob, job_id)
    if job is None or job.deleted_at is not None:
        raise ApiError("not_found", f"任务不存在: {job_id}")
    candidates: list[dict] = []
    if job.status == "completed":
        rows = session.scalars(
            select(AnnouncementCandidate)
            .where(AnnouncementCandidate.search_job_id == job_id)
            .order_by(AnnouncementCandidate.created_at)
        ).all()
        region = _job_region(job)
        candidates = _finalize_candidates(
            [_candidate_card(c, region_recall=_candidate_region_recall(c, region)) for c in rows],
            include_all=include_all,
        )
    return {
        "request_id": request_id,
        "status": job.status,
        "items": candidates,
        "summary": job.result_summary if isinstance(job.result_summary, list) else None,
        "error_code": job.error_code,
        "environment": (get_collection_policy().name if not _policy_error() else "unknown"),
        "data_sources": [s.get("source_id") for s in (job.result_summary or [])
                         if isinstance(s, dict) and s.get("source_id")],
    }


@router.get("/announcement/candidates/by-project/{project_id}")
def candidate_by_project(
    project_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """按 project_id 反查候选公告（E1，2026-09-10）。

    「继续项目」入口从搜索历史/书签进入 import.html 时可能只带 project_id
    （未携带 candidate_id）；本端点回查该项目最近的候选，供详情卡（公告自动
    详情）与「来源候选」上下文展示。只读，不改变候选状态。
    """
    require_role(role, "announcement", "read", session=session, actor=role, object_ref=project_id)
    cand = session.scalar(
        select(AnnouncementCandidate)
        .where(AnnouncementCandidate.project_id == project_id)
        .order_by(AnnouncementCandidate.created_at.desc())
        .limit(1)
    )
    if cand is None:
        raise ApiError("not_found", f"项目没有关联的公告候选: {project_id}")
    card = _candidate_card(cand)
    return {"request_id": request_id, "candidate": card}


@router.post("/announcement/candidates/manual")
def register_manual_candidate(
    body: ManualCandidateBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """Agent 聊天等外部线索转人工登记为候选（09-优化方案 §3.1.9）。

    网页合规采集空结果 ≠ 外部渠道没有线索：投标专员把 Agent 搜到的
    公告标题/链接人工转录为候选（不抓取、不推断，只登记原文链接事实），
    之后走既有「选择深入」合规链（robots + 限频 + 详情原文固化建档）。
    登记幂等：同一 URL 重复提交返回既有候选（job_id/candidate_id）。
    """
    require_role(role, "announcement", "write", session=session, actor=actor)
    try:
        url = _normalize_url(body.url)   # 仅 http/https，冗余归一化
    except Exception as exc:
        raise ApiError("invalid_request", f"链接不合法: {exc}")
    # 幂等：同 URL 已有登记任务 → 返回既有候选
    existing_job = session.scalar(
        select(AnalysisJob).where(
            AnalysisJob.kind == MANUAL_JOB_KIND,
            AnalysisJob.idempotency_key == f"announcement.manual_entry:{url}",
        )
    )
    if existing_job is not None:
        cand = session.scalar(
            select(AnnouncementCandidate).where(
                AnnouncementCandidate.search_job_id == existing_job.job_id
            )
        )
        if cand is not None:
            return {"request_id": request_id, "job_id": existing_job.job_id,
                    "candidate_id": cand.candidate_id, "created": False}
    import json as _json
    import uuid as _uuid

    job = AnalysisJob(
        job_id=_uuid.uuid4().hex[:16],
        kind=MANUAL_JOB_KIND,
        status="completed",              # 登记即终态：不作为 worker 任务领取
        attempts=0,
        max_attempts=1,
        input_ref=_json.dumps({"title": body.title[:500], "url": url, "note": body.note},
                              ensure_ascii=False)[:500],
        idempotency_key=f"announcement.manual_entry:{url}",
    )
    session.add(job)
    session.flush()
    cand = AnnouncementCandidate(
        candidate_id=_uuid.uuid4().hex[:16],
        search_job_id=job.job_id,
        source_id=MANUAL_SOURCE_ID,
        source_name="Agent 线索（人工转录）",
        title=body.title[:500],
        url=url,
        category=None,
        region=None,          # 人工转录不推断地区 → 待详情回源核实
        publish_date=None,
        import_status="pending",
        requested_by=actor,
    )
    session.add(cand)
    session.flush()
    # F026：人工转录线索同样进入待选池，以相同预筛/审计口径展示。
    from runtime.discovery.service import sync_candidate
    sync_candidate(session, cand)
    session.commit()
    return {"request_id": request_id, "job_id": job.job_id,
            "candidate_id": cand.candidate_id, "created": True}


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
    # F026：后端入口也同步人工选择深入状态，前端漏调不会造成待选池失真。
    from runtime.discovery import service as discovery_service
    try:
        discovery_service.set_pool_status(session, candidate_id=candidate_id,
                                          status=discovery_service.POOL_DEEP_DIVE, actor=actor)
    except (KeyError, ValueError):
        pass  # 历史候选尚未回填/已处理，继续按导入幂等规则处理
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


@router.get("/announcement/candidates/{candidate_id}/text")
def candidate_text(
    candidate_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    session: Session = Depends(get_db),
) -> dict:
    """候选公告固化原文（P1 溯源定位用，docs/10 §5 P1-5）。

    返回最新版本的净化正文全文 + content_hash——前端据此按 detail_summary 的
    quote/start/end 高亮定位；hash 不一致时走 relocate 兜底或提示回原页核实。
    原文事实以对象库为准，此处只读不缓存改写。
    """
    require_role(role, "announcement", "read", session=session, actor=role,
                 object_ref=candidate_id)
    candidate = session.get(AnnouncementCandidate, candidate_id)
    if candidate is None:
        raise ApiError("not_found", f"候选公告不存在: {candidate_id}")
    ver = session.scalars(select(MaterialVersion).where(
        MaterialVersion.material_id == f"MAT-{candidate_id}"
    ).order_by(MaterialVersion.version.desc())).first()
    if ver is None:
        raise ApiError("not_found", "公告原文尚未固化（候选未导入或导入未完成）")
    obj_path = Path(object_store_root()) / ver.object_uri
    if not obj_path.exists():
        raise ApiError("not_found", f"公告原文对象缺失: {ver.object_uri}")
    text = obj_path.read_text(encoding="utf-8", errors="ignore")
    return {
        "request_id": request_id,
        "candidate_id": candidate_id,
        "material_id": ver.material_id,
        "version": ver.version,
        "content_hash": ver.content_hash,
        "length": len(text),
        "text": text,
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
        raise ApiError(
            "unsupported_format",
            f"不支持加密固化格式 {suffix}：招标文件仅支持 docx/pdf，且不会绕过加密保护（F005 §约束）",
        )
    if suffix not in TENDER_SUFFIXES:
        raise ApiError("unsupported_format", "仅支持 docx/pdf 招标文件")

    content = file.file.read()
    if not content:
        raise ApiError("invalid_request", "空文件：未上传完整招标文件")
    # v1.7（09-优化方案 §3.2.3）：后缀 + 魔数 + 大小上限三重校验，不接受仅后缀正确的伪装文件
    if len(content) > TENDER_MAX_BYTES:
        raise ApiError(
            "file_too_large",
            f"招标文件超过上限 {TENDER_MAX_BYTES // (1024 * 1024)} MB（F020 §2.2.2 v1.7）",
        )
    magic = content[:8]
    if suffix == ".pdf" and not magic.startswith(b"%PDF"):
        raise ApiError("unsupported_format", "文件内容不是 PDF（魔数校验失败），请确认文件完整未损坏")
    if suffix == ".docx" and not magic.startswith(b"PK"):
        raise ApiError("unsupported_format", "文件内容不是 DOCX（魔数校验失败），请确认文件完整未损坏")
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
    # 每次重解析都是一次新执行：用调用级幂等键（2026-09-15 修复：此前走默认键
    # kind+input_ref+project_id，同一材料第二次重解析只会拿回旧任务、什么都不跑）
    import uuid as _uuid

    job, created = worker_service.create_job(
        session, kind="parse.tender_document", input_ref=material.material_id,
        project_id=material.project_id,
        idempotency_key=f"parse.tender_document:reparse:{material.material_id}:{_uuid.uuid4().hex[:12]}",
    )
    return {"request_id": request_id, "job_id": job.job_id, "created": created}
