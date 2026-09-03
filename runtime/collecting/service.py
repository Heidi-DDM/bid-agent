# R004/F004：公告搜索与详情入库执行编排（worker 执行器专用）
# 流程（每次搜索任务）：
#   源校验 → region 覆盖判断 → robots 预检 → 限频预查 → 抓列表页（1 页/源，红线
#   单源 ≤1 次/5 分钟）→ 解析 → 关键词过滤 → 候选落库（announcement_candidates）。
#   候选确认入库（import_detail）：robots + 限频 → 抓详情原文 → 原文固化
#   （material_service.import_material，public/raw 不可覆盖）→ Project 建档。
# 所有抓取失败/被拒均为"如实记录 + 可重试"，不静默伪造成功（F004/F021 §5）；
# 限频器为 worker 进程级（单实例部署，多实例需换共享存储——schema §5.2）。
from __future__ import annotations

import json
import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from runtime.collecting.parsers import parse_hebtig_list
from runtime.collecting.registry import SourceSpec, get, validate_sources
from runtime.core.compliance import ComplianceError, RateLimiter, parse_robots
from runtime.core.fetcher import FetchError, RobotsUnavailable, fetch_robots, fetch_text
from runtime.db import api_service, material_service
from runtime.db.models import AnnouncementCandidate, AnalysisJob

logger = logging.getLogger("runtime.collecting")

# worker 进程级限频器（单 worker 轮询模型；正式期多实例需 Redis 等共享实现）
_limiter = RateLimiter()

FetchFn = Callable[[str], str]
RobotsFn = Callable[[str], str | None]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _robots_allows(robots_fn: RobotsFn, url: str) -> tuple[bool, str | None]:
    """robots 预检：返回 (允许, 跳过原因)。

    robots.txt 缺失（404/空）→ 按 RFC 9309 允许（fetched=False 记录待复核）；
    网络不可达 → 保守跳过（schema §5.2：不可判定交人工复核，不静默抓取）。
    """
    try:
        text = robots_fn(url)
    except RobotsUnavailable as exc:
        return False, f"robots.txt 不可达，保守跳过: {exc}"
    rules = parse_robots(text)
    from urllib.parse import urlparse

    path = urlparse(url).path or "/"
    if not rules.allows(path):
        return False, f"robots.txt 禁止采集路径: {path}"
    return True, None


def _record_fetch(url: str) -> str:
    """限频预查 + 抓取后登记。违反红线抛 ComplianceError。"""
    from urllib.parse import urlparse

    domain = urlparse(url).netloc
    _limiter.check(domain, record=False)
    return domain


def _skip_candidate_save(session: Session, *, search_job_id: str, source: SourceSpec,
                         item: dict) -> AnnouncementCandidate:
    cand = AnnouncementCandidate(
        candidate_id=uuid.uuid4().hex[:16],
        search_job_id=search_job_id,
        source_id=source.source_id,
        source_name=source.name,
        title=item["title"],
        url=item["url"],
        category=item.get("category"),
        region=None,          # 列表页不逐条标注地区：不推断（待详情回源）
        publish_date=None,    # 同上：缺省待补
        import_status="pending",
    )
    session.add(cand)
    return cand


def search_sources(
    session: Session,
    *,
    keyword: str,
    region: str | None,
    sources: list[str] | None,
    search_job_id: str,
    fetch_fn: FetchFn | None = None,
    robots_fn: RobotsFn | None = None,
) -> list[dict]:
    """执行一次 manual_trigger 搜索：逐源抓列表 → 解析 → 过滤 → 候选落库。

    返回每源结果摘要 [{source_id, name, status: ok|skipped|error, count, note}]；
    源级失败/限频/合规拒绝均为如实摘要（不使整个任务失败，不伪造成功）。
    """
    fetch = fetch_fn or fetch_text
    robots = robots_fn or fetch_robots
    valid_ids, unknown = validate_sources(sources)
    summary: list[dict] = []
    if unknown:
        summary.append({"source_id": ",".join(unknown), "name": "未注册源",
                        "status": "error", "count": 0,
                        "note": f"未注册/未启用（清单 §2 以外平台不接入）: {unknown}"})
    for sid in valid_ids:
        source = get(sid)
        assert source is not None
        entry = {"source_id": sid, "name": source.name, "status": "ok", "count": 0, "note": None}
        try:
            # region 覆盖判断（源为河北域平台，不做逐条推断）
            if not source.covers_region(region):
                entry.update(status="skipped",
                             note=f"region={region!r} 超出源覆盖范围（{source.region_scope}）")
                summary.append(entry)
                continue
            allowed, reason = _robots_allows(robots, source.list_url)
            if not allowed:
                entry.update(status="skipped", note=reason)
                summary.append(entry)
                continue
            domain = _record_fetch(source.list_url)
            html = fetch(source.list_url)
            _limiter.check(domain, record=True)
            items = parse_hebtig_list(html, source)
            # 关键词过滤：标题整体包含 keyword（大小写不敏感；多词按整串匹配）
            kw = (keyword or "").strip().lower()
            hits = [it for it in items if not kw or kw in it["title"].lower()]
            for it in hits:
                _skip_candidate_save(session, search_job_id=search_job_id, source=source, item=it)
            entry["count"] = len(hits)
            entry["note"] = f"列表命中 {len(items)} 条，关键词过滤后 {len(hits)} 条"
            session.commit()
        except ComplianceError as exc:
            session.rollback()
            entry.update(status="skipped", note=f"限频/合规拒绝: {exc}")
        except FetchError as exc:
            session.rollback()
            entry.update(status="error", note=f"抓取失败: {exc}")
        except Exception as exc:  # 解析/落库异常：如实失败并保摘要
            session.rollback()
            logger.exception("源 %s 搜索异常", sid)
            entry.update(status="error", note=f"{type(exc).__name__}: {exc}")
        summary.append(entry)
    return summary


def import_candidate_detail(
    session: Session,
    *,
    candidate: AnnouncementCandidate,
    actor: str,
    store_root: str,
    fetch_fn: FetchFn | None = None,
    robots_fn: RobotsFn | None = None,
) -> dict:
    """候选公告确认入库：抓详情原文 → 原文固化 → Project 建档。

    原文入库走 material_service（public / source_type=official_platform /
    raw 不可覆盖 / 内容哈希去重，重复内容 created=False 不重复建）；
    详情页缺失字段不推断，留待人工回源（规则 1）。
    """
    fetch = fetch_fn or fetch_text
    robots = robots_fn or fetch_robots
    allowed, reason = _robots_allows(robots, candidate.url)
    if not allowed:
        raise ComplianceError(reason)
    domain = _record_fetch(candidate.url)
    html = fetch(candidate.url)
    if not html.strip():
        raise FetchError("详情页抓取结果为空")
    _limiter.check(domain, record=True)

    # 对象库禁止 .html/.htm（F019 §4：防脚本内容入库）→ 净化正文以 .txt 固化；
    # 净化口径见 runtime/collecting/parsers.html_to_text（不含推断补全）。
    from runtime.collecting.parsers import html_to_text

    text = html_to_text(html)
    if not text:
        raise FetchError("详情页净化后无可见文本（疑似非公告正文页面）")
    project = api_service.create_project(
        session,
        project_id=f"PJ-{uuid.uuid4().hex[:10]}",
        project_name=candidate.title[:250],
        actor=actor,
    )
    tmp = Path(tempfile.gettempdir()) / f"{candidate.candidate_id}.txt"
    tmp.write_text(text, encoding="utf-8")
    try:
        imported = material_service.import_material(
            session,
            src_path=str(tmp),
            material_id=f"MAT-{candidate.candidate_id}",
            material_type="announcement",
            source_type="official_platform",
            owner_type="public",
            classification="public",
            permission_scope="public_read",
            data_owner=actor,
            store_root=store_root,
            project_id=project.project_id,
            actor=actor,
        )
    finally:
        tmp.unlink(missing_ok=True)

    candidate.project_id = project.project_id
    candidate.import_status = "imported"
    session.add(candidate)
    api_service.audit(
        session, actor=actor, action="announcement.import.completed",
        basis=f"candidate={candidate.candidate_id} source={candidate.source_id}",
        outcome=f"project={project.project_id} material={imported.material.material_id} "
                f"created={imported.created} url={candidate.url}",
        object_ref=project.project_id,
    )
    session.commit()
    logger.info("公告入库完成 candidate=%s project=%s material=%s created=%s",
                candidate.candidate_id, project.project_id,
                imported.material.material_id, imported.created)
    return {"project_id": project.project_id, "material_id": imported.material.material_id,
            "created": imported.created}


def mark_import_failed(session: Session, candidate_id: str, error: Exception) -> None:
    """候选导入失败落账（如实记录，供重试与人工查看）；异常继续上抛由任务层处理。"""
    cand = session.get(AnnouncementCandidate, candidate_id)
    if cand is None:
        return
    cand.import_status = "failed"
    cand.error_message = f"{type(error).__name__}: {str(error)[:500]}"
    session.commit()


def save_result_summary(session: Session, job_id: str, summary: list[dict]) -> None:
    job = session.get(AnalysisJob, job_id)
    if job is not None:
        job.result_summary = summary
        session.commit()