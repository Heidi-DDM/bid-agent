# R004/F004：公告搜索与详情入库执行编排（worker 执行器专用）
# 流程（每次搜索任务）：
#   源校验 → region 覆盖判断 → robots 预检 → 限频预查 → 抓列表页（策略有界分页，
#   同一源一次搜索批次只登记 1 次限频事件）→ 解析 → 关键词过滤 → 候选落库。
#   候选确认入库（import_detail）：robots + 限频 → 抓详情原文 → 原文固化
#   （material_service.import_material，public/raw 不可覆盖）→ Project 建档。
# 所有抓取失败/被拒均为"如实记录 + 可重试"，不静默伪造成功（F004/F021 §5）；
# 限频器为 worker 进程级（单实例部署，多实例需换共享存储——schema §5.2）。
from __future__ import annotations

import json
import logging
import tempfile
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from runtime.collecting.parsers import parse_announce_list as parse_hebtig_list
from runtime.collecting.registry import (
    SourceSpec,
    _normalize_keyword,
    get,
    keyword_matches,
    validate_sources,
)
from runtime.core.compliance import (
    ComplianceError,
    CollectionPolicy,
    RateLimiter,
    build_user_agent,
    get_collection_policy,
    parse_robots,
)
from runtime.core.fetcher import FetchError, RobotsUnavailable, fetch_robots, fetch_text
from runtime.db import api_service, material_service
from runtime.db.models import AnnouncementCandidate, AnalysisJob

logger = logging.getLogger("runtime.collecting")

# worker 进程级限频器（单 worker 轮询模型；正式期多实例需 Redis 等共享实现）
_limiter = RateLimiter()
_limiter_policy_signature: tuple[object, ...] | None = None
_limiter_instance_id: int | None = None

FetchFn = Callable[[str], str]
RobotsFn = Callable[[str], str | None]


def _retry_clock_hint(retry_after_seconds: int) -> str:
    """限频剩余秒数 → 人类可读「几点可重试」时刻（本地时区，含秒内粗粒度）。

    纯展示辅助：不绕过红线，仅把服务端算好的窗口剩余换算为可读时刻。
    """
    from datetime import timedelta as _td

    return (datetime.now().astimezone() + _td(seconds=retry_after_seconds)).strftime("%H:%M:%S")


def _policy_limiter(policy: CollectionPolicy) -> RateLimiter:
    """返回当前策略对应的进程级限频器；策略切换后绝不沿用旧窗口。"""
    global _limiter, _limiter_policy_signature, _limiter_instance_id

    signature = (
        policy.name,
        policy.enforce_rate_limit,
        policy.single_source_seconds,
        policy.global_max_calls_per_hour,
    )
    if (_limiter_policy_signature != signature
            or _limiter_instance_id != id(_limiter)):
        _limiter = RateLimiter.from_policy(policy)
        _limiter_policy_signature = signature
        _limiter_instance_id = id(_limiter)
    return _limiter


def limiter_next_allowed(domain: str | None):
    """查询进程级限频器：该域下一次可抓取时刻（无限制返回 None）。

    供健康检查/诊断端点读取（只查询不记录）；多实例部署时换共享存储实现。
    """
    return _policy_limiter(collection_policy()).next_allowed_at(domain)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _robots_precheck(robots_fn: RobotsFn, url: str) -> tuple[bool, str, str | None]:
    """robots 预检留痕（ADR-003：不阻断，仅记录，供审计与逐源展示）。

    返回 (是否允许, robots_status, 说明)：robots.txt 缺失（404/空）→ missing
    （RFC 9309 默认允许）；网络不可达 → unreachable（如实记录，不阻断抓取）；
    Disallow 命中 → disallowed（原因打全命中规则，仍继续抓取——阻断与否由
    业务侧源启用登记决定，不由运行时硬判）。
    """
    try:
        text = robots_fn(url)
    except RobotsUnavailable as exc:
        return True, "unreachable", f"robots.txt 不可达: {exc}"
    rules = parse_robots(text)
    from urllib.parse import urlparse

    path = urlparse(url).path or "/"
    if not rules.fetched:
        return True, "missing", "robots.txt 缺失/不可达（RFC 9309 默认允许）"
    if not rules.allows(path):
        rule = rules.matched_disallow(path)
        detail = f"Disallow: {rule}" if rule else "无 Disallow 命中（Allow 生效则不应拒绝）"
        return False, "disallowed", (
            f"robots.txt 命中 {detail}；预检留痕不阻断（ADR-003），业务侧未配置跳过"
        )
    return True, "allowed", None


def _record_fetch(url: str, limiter: RateLimiter) -> str:
    """限频预查 + 抓取后登记。违反红线抛 ComplianceError。"""
    from urllib.parse import urlparse

    domain = urlparse(url).netloc
    limiter.check(domain, record=False)
    return domain


def collection_policy() -> CollectionPolicy:
    """返回当前采集策略；未知 COLLECTION_POLICY 由合规模块 fail-closed。"""
    return get_collection_policy()


def _default_fetchers(policy: CollectionPolicy) -> tuple[FetchFn, RobotsFn]:
    """将当前策略的 UA 注入真实抓取器；测试替身不经此路径。"""
    user_agent = build_user_agent(transparent=policy.transparent_ua)

    def fetch(url: str) -> str:
        return fetch_text(url, user_agent=user_agent)

    def robots(url: str) -> str | None:
        return fetch_robots(url, user_agent=user_agent)

    return fetch, robots


def _skip_candidate_save(session: Session, *, search_job_id: str, source: SourceSpec,
                         item: dict) -> AnnouncementCandidate:
    """候选落库（列表页可确证事实：标题/链接/分区/发布日；地区不逐条标注不推断）。"""
    publish_date = None
    if item.get("publish_date"):
        from datetime import date, datetime as _dt

        try:
            publish_date = _dt.strptime(item["publish_date"], "%Y-%m-%d").date()
        except ValueError:
            publish_date = None  # 格式不符 → 待详情回源，不硬塞
    cand = AnnouncementCandidate(
        candidate_id=uuid.uuid4().hex[:16],
        search_job_id=search_job_id,
        source_id=source.source_id,
        source_name=source.name,
        title=item["title"],
        url=item["url"],
        category=item.get("category"),
        region=None,          # 列表页不逐条标注地区：不推断（待详情回源）
        publish_date=publish_date,
        import_status="pending",
    )
    session.add(cand)
    return cand


def _list_page_count(policy: CollectionPolicy, source: SourceSpec) -> int:
    """计算本次搜索页数：策略是硬上限，未探测分页形态的源只抓首页。"""
    if not source.page_param:
        return 1
    return min(policy.max_pages, source.max_pages)


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
    """执行一次 manual_trigger 搜索：逐源抓列表 → 解析 → 关键词过滤 → 候选落库。

    v1.7（09-优化方案 §3.1）：搜索只定义联网采集范围（关键词/地区/来源）；
    「种类」「规模」由调用方在候选结果内筛选（GET .../search/{job_id}/candidates），
    不在本执行器过滤——保证一次联网检索先返回完整候选集，不在抓取端收窄。

    返回每源结果摘要 [{source_id, name, status: ok|skipped|error, count, note}]；
    源级失败/限频/合规拒绝均为如实摘要（不使整个任务失败，不伪造成功）。
    """
    policy = collection_policy()
    default_fetch, default_robots = _default_fetchers(policy)
    fetch = fetch_fn or (default_fetch if policy.network_allowed else None)
    robots = robots_fn or default_robots
    limiter = _policy_limiter(policy)
    valid_ids, unknown = validate_sources(sources)
    summary: list[dict] = []
    if unknown:
        summary.append({"source_id": ",".join(unknown), "name": "未注册源",
                        "status": "error", "count": 0,
                        "note": f"未注册/未启用（清单 §2 以外平台不接入）: {unknown}",
                        "environment": policy.name, "data_source": "unregistered"})
    for sid in valid_ids:
        source = get(sid)
        assert source is not None
        pages_to_fetch = _list_page_count(policy, source)
        entry = {"source_id": sid, "name": source.name, "status": "ok", "count": 0, "note": None,
                 "environment": policy.name, "data_source": source.source_id,
                 "pages_requested": pages_to_fetch, "pages_fetched": 0,
                 "region_inference": (
                     "source_scope_only" if policy.infer_region and source.region_scope
                     else "disabled"
                 )}
        recalled = False
        try:
            if policy.fixture_only and fetch is None:
                entry.update(
                    status="skipped",
                    note=f"采集策略 {policy.name} 仅允许本地 fixture；未提供 fixture，未访问真实平台",
                )
                summary.append(entry)
                continue
            # region 覆盖判断（源为河北域平台，不做逐条推断）：
            # 精确覆盖 → 正常抓取；超范围 → 放宽二次召回（仍抓列表 + 关键词过滤，
            # 候选地区不推断，标「待核实」交详情页回源）——不整源丢弃（用户实测：
            # 网页合规空结果断链，地区一卡死就空了）。
            if not source.covers_region(region):
                recalled = True
                entry["note"] = (
                    f"region={region!r} 超出平台覆盖（{source.region_scope}）→ "
                    f"放宽二次召回，命中项地区待核实（列表页不标注，不推断归属）"
                )
            # v1.10：带站内检索的平台（search_param 非空）——有 keyword 时一次任务抓
            # 「平台检索结果页」而非默认首页；robots 预检仍按默认列表路径判定（同域同前缀，
            # robots 按路径前缀规则，query 不改变命中）。无 keyword/无检索参数 → 默认列表页。
            list_url = source.list_url
            if keyword and source.search_param:
                list_url = f"{source.list_url}?{source.search_param}={urllib.parse.quote(_normalize_keyword(keyword))}"
            # robots 预检留痕不阻断（ADR-003）：记录 robots_status 于摘要，
            # 不跳过源（阻断与否由业务侧源启用登记决定）。
            robots_status, robots_note = "missing", None
            if policy.enforce_robots:
                _, robots_status, robots_note = _robots_precheck(robots, source.list_url)
                entry["robots_status"] = robots_status
            domain = _record_fetch(source.list_url, limiter) if policy.enforce_rate_limit else None
            assert fetch is not None
            # 多页属于同一个 manual_trigger 搜索批次：只在第 1 页成功后登记一次
            # 源级事件，后续页不重复调用 limiter，避免分页变相绕过或触发“每页一次”
            # 的错误语义。策略 max_pages 是硬上限，source.max_pages 是源级上限。
            items_by_url: dict[str, dict] = {}
            for page in range(1, pages_to_fetch + 1):
                page_url = source.page_url(page, list_url=list_url)
                html = fetch(page_url)
                if page == 1 and policy.enforce_rate_limit and domain is not None:
                    limiter.check(domain, record=True)
                entry["pages_fetched"] = page
                page_items = parse_hebtig_list(html, source)
                for item in page_items:
                    # 平台偶尔会在相邻页重复条目；URL 是列表页可确证的稳定键，
                    # 不用标题推断或覆盖事实。
                    items_by_url.setdefault(item["url"], item)

            items = list(items_by_url.values())
            # 关键词过滤（v1.10：确定性多词 AND 归一匹配——keyword_matches；
            # 空关键词恒命中；种类/规模不在抓取端过滤，候选集内筛选）
            hits = [it for it in items if keyword_matches(keyword, it["title"])]
            for it in hits:
                _skip_candidate_save(session, search_job_id=search_job_id, source=source, item=it)
            entry["count"] = len(hits)
            fetch_note = "平台检索页命中" if list_url != source.list_url else "列表命中"
            robots_suffix = f"；robots 预检[{robots_status}]{'：' + robots_note if robots_note else ''}" if robots_status != "missing" else ""
            if recalled:
                entry["note"] = (
                    f"放宽召回：抓取 {entry['pages_fetched']} 页、去重后 {len(items)} 条，关键词过滤后 {len(hits)} 条；"
                    f"region={region!r} 超出平台覆盖（{source.region_scope}），候选地区待核实"
                    f"{robots_suffix}"
                )
            else:
                entry["note"] = (
                    f"{fetch_note}：抓取 {entry['pages_fetched']} 页、去重后 {len(items)} 条，"
                    f"关键词过滤后 {len(hits)} 条{robots_suffix}"
                )
            session.commit()
        except ComplianceError as exc:
            session.rollback()
            retry_note = ""
            if exc.retry_after_seconds is not None:
                when = _retry_clock_hint(exc.retry_after_seconds)
                retry_note = f"（可重试时刻：约 {when}，即 {exc.retry_after_seconds} 秒后）"
            entry.update(status="skipped",
                         note=f"限频/合规拒绝: {exc}{retry_note}")
            entry["retry_after_seconds"] = exc.retry_after_seconds
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
    policy = collection_policy()
    default_fetch, default_robots = _default_fetchers(policy)
    fetch = fetch_fn or (default_fetch if policy.network_allowed else None)
    robots = robots_fn or default_robots
    limiter = _policy_limiter(policy)
    if policy.fixture_only and fetch is None:
        raise ComplianceError(f"采集策略 {policy.name} 仅允许本地 fixture；未提供 fixture")
    # robots 预检留痕不阻断（ADR-003）：记录 robots_status，不因 Disallow 拒绝抓取。
    robots_precheck_note = None
    if policy.enforce_robots:
        _, robots_status, robots_note = _robots_precheck(robots, candidate.url)
        if robots_note:
            robots_precheck_note = f"robots 预检[{robots_status}]: {robots_note}"
    domain = _record_fetch(candidate.url, limiter) if policy.enforce_rate_limit else None
    assert fetch is not None
    html = fetch(candidate.url)
    if not html.strip():
        raise FetchError("详情页抓取结果为空")
    if policy.enforce_rate_limit and domain is not None:
        limiter.check(domain, record=True)

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
    precheck_suffix = f" {robots_precheck_note}" if robots_precheck_note else ""
    api_service.audit(
        session, actor=actor, action="announcement.import.completed",
        basis=f"candidate={candidate.candidate_id} source={candidate.source_id}",
        outcome=f"project={project.project_id} material={imported.material.material_id} "
                f"created={imported.created} url={candidate.url}{precheck_suffix}",
        object_ref=project.project_id,
    )
    session.commit()
    logger.info("公告入库完成 candidate=%s project=%s material=%s created=%s",
                candidate.candidate_id, project.project_id,
                imported.material.material_id, imported.created)
    return {"project_id": project.project_id, "material_id": imported.material.material_id,
            "created": imported.created}


def mark_import_failed(session: Session, candidate_id: str, error: Exception) -> None:
    """候选导入失败落账（如实记录，供重试与人工查看）；异常继续上抛由任务层处理。

    限频类失败（ComplianceError 带 retry_after_seconds）在落账文案中附加
    「可重试时刻/秒数」，前端据此展示倒计时；不绕过红线（窗口内重试仍被拒）。
    """
    cand = session.get(AnnouncementCandidate, candidate_id)
    if cand is None:
        return
    cand.import_status = "failed"
    suffix = ""
    if isinstance(error, ComplianceError) and error.retry_after_seconds is not None:
        when = _retry_clock_hint(error.retry_after_seconds)
        suffix = f"（限频：约 {when}（{error.retry_after_seconds} 秒后）可重试）"
    cand.error_message = f"{type(error).__name__}: {str(error)[:500]}{suffix}"
    session.commit()


def save_result_summary(session: Session, job_id: str, summary: list[dict]) -> None:
    job = session.get(AnalysisJob, job_id)
    if job is not None:
        job.result_summary = summary
        session.commit()
