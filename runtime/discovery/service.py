"""F026：确定性搜索预筛、待选池同步与版本化规则。

不调用模型、不读取企业私有资料。输出只说明标题/类型规则的命中情况，
不得解释为商业价值或投标建议。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runtime.collecting.announcement_type import TYPE_LABELS, is_default_include, classify_type, project_key
from runtime.collecting.registry import _normalize_keyword
from runtime.db.models import AnalysisJob, AnnouncementCandidate, DiscoveryRuleProfile, SelectionPoolItem

POOL_PENDING = "pending"
POOL_NEEDS_REVIEW = "needs_manual_review"
POOL_DEEP_DIVE = "deep_dive"
POOL_DISMISSED = "dismissed"
POOL_SUPERSEDED = "superseded"
POOL_EXPIRED = "expired"

# 简报/卡片展示的公告要点键（F005 主卡口径；仅建档抓详情后的候选有值）。
HIGHLIGHT_FIELDS = (
    "qualification", "budget_amount", "purchaser", "agency",
    "duration", "open_location", "joint_venture", "quality",
)
_NULLISH = {"", "null", "none", "/", "无", "待补"}

DEFAULT_RULES: dict[str, Any] = {
    "industry_keywords": [
        "施工", "工程", "EPC", "总承包", "建设", "改造", "装修", "道路", "建筑", "市政", "房建", "机电", "安装",
    ],
    # 默认不以行业词自动排除；避免列表页短标题误删真实招标。
    "exclude_keywords": [],
    # ADR-006 v2：企业业务画像——AI 预筛按此判定相关性（经营负责人可改；留空用内置默认）。
    "business_profile": "",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def active_rule(session: Session) -> tuple[str, dict[str, Any]]:
    row = session.scalar(
        select(DiscoveryRuleProfile).where(DiscoveryRuleProfile.is_active.is_(True))
        .order_by(DiscoveryRuleProfile.created_at.desc()).limit(1)
    )
    if row is None:
        return "builtin-v1", dict(DEFAULT_RULES)
    config = dict(DEFAULT_RULES)
    config.update(row.config or {})
    return row.version, config


def _words(config: dict[str, Any], key: str) -> list[str]:
    values = config.get(key) or []
    return [str(v).strip() for v in values if str(v).strip()]


def screen_candidate(candidate: AnnouncementCandidate, *, rule_version: str, config: dict[str, Any]) -> dict[str, Any]:
    """按标题可证实事实执行预筛。非招标类型可排除，其余保守保留。"""
    title = candidate.title or ""
    normalized = _normalize_keyword(title)
    a_type = classify_type(title)
    industries = [word for word in _words(config, "industry_keywords") if _normalize_keyword(word) in normalized]
    exclusions = [word for word in _words(config, "exclude_keywords") if _normalize_keyword(word) in normalized]
    if not is_default_include(a_type):
        state = "excluded_non_tender"
        reason = "标题明确属于非招标公告类型"
        pool_status = POOL_EXPIRED
    elif exclusions:
        # 策略性排除不是事实否定：保留为人工查看，不能自动删除。
        state = "needs_manual_review"
        reason = "命中经营策略排除关键词，需人工确认"
        pool_status = POOL_NEEDS_REVIEW
    elif industries:
        state = "brief"
        reason = "命中行业关键词"
        pool_status = POOL_PENDING
    else:
        state = "needs_manual_review"
        reason = "标题未命中行业关键词，列表页信息不足"
        pool_status = POOL_NEEDS_REVIEW
    missing = []
    if candidate.publish_date is None:
        missing.append("publish_date")
    if not candidate.url:
        missing.append("source_url")
    if not candidate.region:
        missing.append("region")
    if not candidate.category:
        missing.append("category")
    return {
        "rule_version": rule_version,
        "screen_state": state,
        "reason": reason,
        "announcement_type": a_type,
        "announcement_type_label": TYPE_LABELS.get(a_type, a_type),
        "matched_industries": industries,
        "matched_exclusions": exclusions,
        "missing_fields": missing,
        "screened_at": _now().isoformat(),
        "source": "title_deterministic_rules",
        "pool_status": pool_status,
    }


def sync_candidate(session: Session, candidate: AnnouncementCandidate) -> SelectionPoolItem:
    """将一个新增搜索候选增量合并到待选池；不覆盖人工处理状态。"""
    existing = session.scalar(
        select(SelectionPoolItem).where(SelectionPoolItem.candidate_id == candidate.candidate_id)
    )
    if existing is not None:
        existing.last_seen_at = _now()
        return existing
    version, config = active_rule(session)
    screening = screen_candidate(candidate, rule_version=version, config=config)
    key = project_key(candidate.title)
    now = _now()
    # 新结果只替换仍未人工处理的同项目候选。已深入/已删除/已导入绝不自动覆盖。
    older = session.scalars(
        select(SelectionPoolItem)
        .where(SelectionPoolItem.project_key == key,
               SelectionPoolItem.pool_status.in_([POOL_PENDING, POOL_NEEDS_REVIEW]))
        .order_by(SelectionPoolItem.last_seen_at.asc())
    ).all()
    item = SelectionPoolItem(
        pool_item_id=f"POOL-{uuid.uuid4().hex[:16]}", candidate_id=candidate.candidate_id,
        project_key=key, pool_status=screening.pop("pool_status"), screening=screening,
        first_seen_at=now, last_seen_at=now,
    )
    session.add(item)
    for old in older:
        old.pool_status = POOL_SUPERSEDED
        old.replaced_by_candidate_id = candidate.candidate_id
    return item


def sync_search_pool(session: Session, search_job_id: str) -> int:
    rows = session.scalars(
        select(AnnouncementCandidate).where(AnnouncementCandidate.search_job_id == search_job_id)
    ).all()
    for row in rows:
        sync_candidate(session, row)
    return len(rows)


# ── 公告要点/截止时间提取（F026 v1.1：快速否决前置到待选池） ────────────────

def _summary_value(summary: dict[str, Any], key: str) -> str | None:
    """从 detail_summary 取一个字段的非缺失值；缺失/占位值一律返回 None（不推断）。"""
    item = summary.get(key)
    if not isinstance(item, dict) or item.get("missing"):
        return None
    value = item.get("value")
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _NULLISH:
        return None
    return text[:160] + ("…" if len(text) > 160 else "")


def candidate_highlights(candidate: AnnouncementCandidate | None) -> dict[str, str] | None:
    """已建档候选的公告要点（资质/金额/采购人/工期等）；未建档返回 None。"""
    if candidate is None or not candidate.detail_summary:
        return None
    summary = candidate.detail_summary if isinstance(candidate.detail_summary, dict) else {}
    highlights = {key: value for key in HIGHLIGHT_FIELDS if (value := _summary_value(summary, key))}
    return highlights or None


def candidate_deadline(candidate: AnnouncementCandidate | None) -> str | None:
    """投标截止（来自公告详情 deadline_bid）；未建档/缺失返回 None，不推断。"""
    if candidate is None or not candidate.detail_summary:
        return None
    summary = candidate.detail_summary if isinstance(candidate.detail_summary, dict) else {}
    value = _summary_value(summary, "deadline_bid")
    return value[:64] if value else None


# ── 搜索增量简报（F026 v1.1：每次搜索给一份可扫读摘要，不是投标建议） ──────────

SEARCH_JOB_KINDS = ("announcement.search", "announcement.manual_entry")


def search_briefing(session: Session, job_id: str | None = None) -> dict[str, Any] | None:
    """最近一次（或指定）搜索任务的入池增量摘要：只聚合池状态，不判断价值。"""
    query = select(AnalysisJob).where(
        AnalysisJob.kind.in_(SEARCH_JOB_KINDS), AnalysisJob.deleted_at.is_(None)
    ).order_by(AnalysisJob.created_at.desc())
    job = session.scalar(query.where(AnalysisJob.job_id == job_id).limit(1)) if job_id \
        else session.scalar(query.limit(1))
    if job is None:
        return None
    try:
        params = json.loads(job.input_ref or "{}")
    except (ValueError, TypeError):
        params = {}
    rows = session.execute(
        select(SelectionPoolItem.pool_status, func.count())
        .join(AnnouncementCandidate, SelectionPoolItem.candidate_id == AnnouncementCandidate.candidate_id)
        .where(AnnouncementCandidate.search_job_id == job.job_id)
        .group_by(SelectionPoolItem.pool_status)
    ).all()
    by_status = {status: count for status, count in rows}
    rule_version = None
    latest_item = session.scalar(
        select(SelectionPoolItem)
        .join(AnnouncementCandidate, SelectionPoolItem.candidate_id == AnnouncementCandidate.candidate_id)
        .where(AnnouncementCandidate.search_job_id == job.job_id)
        .order_by(SelectionPoolItem.first_seen_at.desc()).limit(1)
    )
    if latest_item is not None:
        rule_version = (latest_item.screening or {}).get("rule_version")
    added = sum(by_status.values())
    return {
        "job": {
            "job_id": job.job_id, "kind": job.kind,
            "keyword": params.get("keyword") or None, "region": params.get("region") or None,
            "manual": job.kind == "announcement.manual_entry",
            "status": job.status, "created_at": job.created_at.isoformat(),
        },
        "added": added,
        "by_status": by_status,
        "to_process": by_status.get(POOL_PENDING, 0) + by_status.get(POOL_NEEDS_REVIEW, 0),
        "auto_excluded": by_status.get(POOL_EXPIRED, 0),
        "rule_version": rule_version,
        "notice": "简报只统计本次搜索入池与预筛状态分布，不构成投标建议。",
    }


def preview_rules(session: Session, *, config: dict[str, Any], limit: int = 200) -> dict[str, Any]:
    """规则试跑：对最近 N 条候选回放预筛（不落库、不换版本），返回分布与样例。"""
    normalized = {
        "industry_keywords": _words(config, "industry_keywords"),
        "exclude_keywords": _words(config, "exclude_keywords"),
    }
    rows = session.scalars(
        select(AnnouncementCandidate).order_by(AnnouncementCandidate.created_at.desc()).limit(limit)
    ).all()
    distribution: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    for row in rows:
        result = screen_candidate(row, rule_version="preview", config=normalized)
        state = result["screen_state"]
        distribution[state] = distribution.get(state, 0) + 1
        if len(samples.setdefault(state, [])) < 3:
            samples[state].append(row.title[:80])
    return {
        "total_sampled": len(rows), "distribution": distribution, "samples": samples,
        "config": normalized, "rule_version": "preview",
        "notice": "试跑只回放最近候选，不改变生效规则与历史简报。",
    }


def pool_payload(session: Session, item: SelectionPoolItem, *, candidate: AnnouncementCandidate | None = None) -> dict[str, Any]:
    if candidate is None:
        candidate = session.get(AnnouncementCandidate, item.candidate_id)
    screening = dict(item.screening or {})
    # Candidate is retained as audit evidence; a missing row should not be invented.
    fact = None
    if candidate is not None:
        fact = {
            "candidate_id": candidate.candidate_id, "title": candidate.title,
            "source_name": candidate.source_name, "source_url": candidate.url,
            "publish_date": candidate.publish_date.isoformat() if candidate.publish_date else None,
            "import_status": candidate.import_status,
            "search_job_id": candidate.search_job_id,
            # 建档完成后的项目入口（import.html 上传招标文件/解析）；未建档为 null
            "project_id": candidate.project_id,
            # F026 v1.1：截止时间与公告要点仅在建档抓详情后存在；未建档为 null（不推断）。
            "deadline_bid": candidate_deadline(candidate),
            "highlights": candidate_highlights(candidate),
        }
    return {
        "pool_item_id": item.pool_item_id, "candidate": fact, "project_key": item.project_key,
        "pool_status": item.pool_status, "screening": screening,
        "first_seen_at": item.first_seen_at.isoformat(), "last_seen_at": item.last_seen_at.isoformat(),
        "selected_at": item.selected_at.isoformat() if item.selected_at else None,
        "dismissed_at": item.dismissed_at.isoformat() if item.dismissed_at else None,
        "dismissed_by": item.dismissed_by, "dismissed_reason": item.dismissed_reason,
        "replaced_by_candidate_id": item.replaced_by_candidate_id,
    }


def set_pool_status(session: Session, *, candidate_id: str, status: str, actor: str, reason: str | None = None) -> SelectionPoolItem:
    item = session.scalar(select(SelectionPoolItem).where(SelectionPoolItem.candidate_id == candidate_id))
    if item is None:
        raise KeyError(candidate_id)
    now = _now()
    if status == POOL_DEEP_DIVE:
        if item.pool_status in {POOL_DISMISSED, POOL_EXPIRED, POOL_SUPERSEDED}:
            raise ValueError(f"当前待选池状态不能选择深入: {item.pool_status}")
        item.pool_status = POOL_DEEP_DIVE
        item.selected_at = now
    elif status == POOL_DISMISSED:
        if item.pool_status == POOL_DEEP_DIVE:
            raise ValueError("已选择深入的候选不可从待选池删除；请在项目工作台按项目流程处理")
        item.pool_status = POOL_DISMISSED
        item.dismissed_at = now
        item.dismissed_by = actor
        item.dismissed_reason = (reason or "").strip() or None
    else:
        raise ValueError(f"不支持的待选池状态: {status}")
    item.last_seen_at = now
    return item


def restore_candidate(session: Session, *, candidate_id: str, actor: str) -> SelectionPoolItem:
    """ADR-006：恢复被 AI 预筛排除的候选（仅 excluded_by=llm 的 expired 可恢复）。"""
    item = session.scalar(select(SelectionPoolItem).where(SelectionPoolItem.candidate_id == candidate_id))
    if item is None:
        raise KeyError(candidate_id)
    screening = dict(item.screening or {})
    if item.pool_status != POOL_EXPIRED or screening.get("excluded_by") != "llm":
        raise ValueError(f"仅 AI 预筛排除的候选可恢复（当前 {item.pool_status}）")
    item.pool_status = POOL_NEEDS_REVIEW
    screening["excluded_by"] = None
    screening["excluded_reason"] = None
    screening["restored_by"] = actor
    screening["restored_at"] = _now().isoformat()
    item.screening = screening
    item.last_seen_at = _now()
    return item


def dismiss_batch(session: Session, *, candidate_ids: list[str], actor: str, reason: str | None = None) -> dict[str, Any]:
    """批量软删除：逐条走单条状态机（不放宽任何一条），返回逐条结果。"""
    dismissed: list[str] = []
    failed: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate_id in candidate_ids:
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        try:
            set_pool_status(session, candidate_id=candidate_id, status=POOL_DISMISSED, actor=actor, reason=reason)
            dismissed.append(candidate_id)
        except KeyError:
            failed.append({"candidate_id": candidate_id, "error": "not_found"})
        except ValueError as exc:
            failed.append({"candidate_id": candidate_id, "error": str(exc)})
    return {"dismissed": dismissed, "failed": failed}


def update_rules(session: Session, *, config: dict[str, Any], actor: str) -> DiscoveryRuleProfile:
    normalized = {
        "industry_keywords": _words(config, "industry_keywords"),
        "exclude_keywords": _words(config, "exclude_keywords"),
        # 企业业务画像：AI 预筛相关性判定基准（ADR-006 v2），留空回退内置默认
        "business_profile": str(config.get("business_profile") or "").strip()[:800],
    }
    old = session.scalars(select(DiscoveryRuleProfile).where(DiscoveryRuleProfile.is_active.is_(True))).all()
    for item in old:
        item.is_active = False
    version = f"discovery-{_now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    row = DiscoveryRuleProfile(
        rule_profile_id=f"DRP-{uuid.uuid4().hex[:16]}", version=version, config=normalized,
        is_active=True, created_by=actor,
    )
    session.add(row)
    return row
