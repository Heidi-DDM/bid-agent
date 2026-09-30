"""ADR-006：大模型辅助预筛（第二层，受约束的事实性分类）。

对确定性规则未判定（needs_manual_review）的待选池条目，按标题做封闭词表分类：
公告类别 / 是否可投招标机会 / 依据词 / 置信度。不变量（docs/10 P4 同构）：

1. 只做事实性分类，永不输出“值得投/不建议投/中标率”等商业判断；
2. 依据词（reason）必须是标题子串（机器校验），不过 → 该条不入库结论、回落人工；
3. 类别/相关性/置信度走封闭词表；LLM 判定非招标且置信度 ≥ 中 → 移出待处理
   （excluded_by=llm 留审计，人工可一键恢复），相关性结论只用于排序与徽标；
   确定性规则命中经营策略排除关键词的条目不进入 LLM 预筛（策略排除优先，2026-09-24）；
4. 开关关闭 / 出域门禁不过 / 网络失败 → 零副作用（fail-open 回落人工，不阻断搜索）；
5. 幂等：已有 llm_assist 结论的条目不重复调用。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime.collecting.registry import _normalize_keyword
from runtime.db import api_service
from runtime.db.models import SelectionPoolItem

logger = logging.getLogger("runtime.discovery.llm_prescreen")

BATCH_SIZE = 20
MAX_BATCHES = 25  # 单次任务最多处理 500 条，防失控

# 封闭词表：行业类别（相关组） + 非招标类别（排除组）
CATEGORY_RELEVANT = (
    "工程施工", "市政道路", "房建装修", "EPC总承包", "水利环保", "交通公路",
    "勘察设计咨询", "监理造价", "设备采购", "服务采购", "政府采购", "信息化",
    "能源电力", "其他招标",
)
CATEGORY_NON_TENDER = (
    "非招标-招租租赁", "非招标-中标结果", "非招标-政策法规", "非招标-新闻动态", "非招标-其他",
)
ALL_CATEGORIES = CATEGORY_RELEVANT + CATEGORY_NON_TENDER
RELEVANCE_VALUES = ("yes", "no", "uncertain")
CONFIDENCE_VALUES = ("high", "medium", "low")

# 企业业务画像（v2）：相关性 =「该企业会不会投这个标」，不是「是不是招标机会」。
# 经营负责人可在预筛规则里覆盖（rules.business_profile）；这是组织策略，不是公告事实。
DEFAULT_BUSINESS_PROFILE = (
    "房屋建筑、市政公用、装修装饰、机电安装、道路桥梁、水利水电等工程施工与 EPC 总承包；"
    "以及直接服务于上述工程的勘察设计、监理造价、工程设备与建筑材料采购。"
    "不投：纯 IT/信息化系统、办公设备与耗材、警务/医疗/教育等专业设备、物业服务、"
    "后勤服务、货物类采购等与工程施工无直接关联的机会。"
)


class PrescreenVerdict(BaseModel):
    """单条判定契约：模型只被允许填这四项。"""

    idx: int = Field(ge=0)
    category: str
    relevant: str
    reason: str = Field(min_length=1, max_length=120)
    confidence: str

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reason 不能为空白")
        return v


def _system_prompt(business_profile: str = "") -> str:
    cats = "、".join(ALL_CATEGORIES)
    profile = (business_profile or DEFAULT_BUSINESS_PROFILE).strip()
    return (
        "你是招标公告预筛分类器。企业业务画像：\n"
        f"{profile}\n"
        "任务：判断每条公告标题对该企业是否是**值得查看的投标机会**，并分类。\n"
        "硬性规则：\n"
        f"1. category 只能从以下封闭词表选择：{cats}；\n"
        "2. relevant 以企业业务画像为准：yes=该企业主营业务可投的机会（工程施工/EPC/"
        "勘察设计/监理造价/工程设备材料等）；no=与主营业务无关（纯设备/IT/办公/耗材/服务"
        "采购、招租出租、中标结果、政策法规、新闻动态等）或不是招标机会；"
        "uncertain=标题信息不足以判断；\n"
        "3. reason 必须是**标题原文中的连续片段**（3~20 字，如「智慧警务设备采购」「市政道路」"
        "「施工」「招租」），不得改写、翻译、编造；\n"
        "4. confidence：high=标题明确；medium=较明确；low=不确定；\n"
        "5. 不得输出任何投标价值判断（如值得投、中标率、建议投标）——只做相关性事实分类；\n"
        "示例：「兴隆县人民法院智慧警务设备采购项目」→ no（专业设备采购，与施工无关）；"
        "「XX市政道路改造工程施工招标公告」→ yes；「XX幼儿园玩教具采购」→ no；"
        "「XX厂房EPC总承包招标」→ yes；\n"
        '6. 只输出 JSON：{"items": [{"idx": 0, "category": "…", "relevant": "yes|no|uncertain", '
        '"reason": "…", "confidence": "high|medium|low"}]}，对每个编号各给一条。'
    )


def _build_messages(titles: list[str], business_profile: str = "") -> list[dict]:
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    return [
        {"role": "system", "content": _system_prompt(business_profile)},
        {"role": "user", "content": f"公告标题：\n{numbered}"},
    ]


def parse_verdicts(raw: Any, titles: list[str]) -> dict[int, dict[str, str]]:
    """模型输出 → {idx: 判定 dict}。schema/词表/依据词子串任一不过 → 丢弃该条（回落人工）。"""
    if not isinstance(raw, dict):
        return {}
    items = raw.get("items")
    if not isinstance(items, list):
        return {}
    out: dict[int, dict[str, str]] = {}
    for entry in items:
        try:
            verdict = PrescreenVerdict(**entry) if isinstance(entry, dict) else None
        except ValidationError:
            continue
        if verdict is None:
            continue
        if verdict.idx < 0 or verdict.idx >= len(titles):
            continue
        if verdict.category not in ALL_CATEGORIES:
            continue
        if verdict.relevant not in RELEVANCE_VALUES or verdict.confidence not in CONFIDENCE_VALUES:
            continue
        # 依据词必须是标题子串（归一比较），否则视为编造 → 丢弃
        if _normalize_keyword(verdict.reason) not in _normalize_keyword(titles[verdict.idx]):
            continue
        if verdict.idx in out:
            continue
        out[verdict.idx] = {
            "category": verdict.category, "relevant": verdict.relevant,
            "reason": verdict.reason.strip(), "confidence": verdict.confidence,
        }
    return out


def is_enabled() -> bool:
    """与 P4 兜底同一开关与出域门禁（DeepSeek public-only）。"""
    from runtime.parsing.llm_fallback import is_enabled as fallback_enabled

    return fallback_enabled()


def _default_client():
    from runtime.core import config
    from runtime.core.model import deepseek_chat_json

    def call(messages: list[dict]):
        return deepseek_chat_json(messages, permission_scope="public_read",
                                  timeout=config.llm_fallback_timeout_seconds())

    return call


def _active_business_profile(session: Session) -> str:
    """企业业务画像取自生效预筛规则（经营负责人可配置）；未配置用默认施工企业画像。"""
    from runtime.discovery.service import active_rule

    _version, config = active_rule(session)
    profile = str(config.get("business_profile") or "").strip()
    return profile or DEFAULT_BUSINESS_PROFILE


def prescreen_pool_items(
    session: Session, *, items: list[SelectionPoolItem],
    client: Optional[Callable] = None, enabled: Optional[bool] = None,
    business_profile: str = "",
) -> dict[str, int]:
    """对给定待选池条目执行 LLM 分类并落库（v2 业务画像口径）。

    判定（可恢复、留审计，AI 不删除任何事实）：
    - relevant=yes → `pending`（进主列表，可直接选择深入）；
    - relevant=no 且置信度 ≥ 中 → `expired`（excluded_by=llm，人工可一键恢复）；
    - uncertain / 低置信 → `needs_manual_review`（单独页签，不进主列表）。
    """
    from runtime.db.models import AnnouncementCandidate

    call = client or _default_client()
    on = is_enabled() if enabled is None else enabled
    stats = {"processed": 0, "classified": 0, "ai_excluded": 0, "promoted": 0, "kept_manual": 0}
    # 策略排除优先于 AI（2026-09-24 用户规则）：确定性规则命中排除关键词的条目
    # 停留 needs_manual_review 待人工处理，AI 不得将其晋级回主列表或代为排除。
    todo = [it for it in items
            if it.pool_status in ("pending", "needs_manual_review")
            and not (it.screening or {}).get("llm_assist")
            and not (it.screening or {}).get("matched_exclusions")]
    stats["kept_manual"] = len(items) - len(todo)
    if not on:
        stats["kept_manual"] += len(todo)
        return stats
    profile = business_profile or _active_business_profile(session)
    now = datetime.now(timezone.utc).isoformat()
    capped = todo[: BATCH_SIZE * MAX_BATCHES]
    stats["kept_manual"] += len(todo) - len(capped)
    for start in range(0, len(capped), BATCH_SIZE):
        batch = capped[start:start + BATCH_SIZE]
        pairs: list[tuple[SelectionPoolItem, str]] = []
        for it in batch:
            cand = session.get(AnnouncementCandidate, it.candidate_id)
            title = (cand.title if cand is not None else "") or ""
            if title:
                pairs.append((it, title))
        if not pairs:
            stats["kept_manual"] += len(batch)
            continue
        titles = [t for _, t in pairs]
        try:
            result = call(_build_messages(titles, profile))
        except Exception as exc:  # 网络/门禁异常：零副作用
            logger.warning("LLM 预筛调用异常：%s", type(exc).__name__)
            stats["kept_manual"] += len(batch)
            continue
        if not getattr(result, "ok", False):
            stats["kept_manual"] += len(batch)
            continue
        verdicts = parse_verdicts(result.data, titles)
        for i, (item, _title) in enumerate(pairs):
            stats["processed"] += 1
            verdict = verdicts.get(i)
            if verdict is None:
                continue
            screening = dict(item.screening or {})
            screening["llm_assist"] = {**verdict, "model": "deepseek", "at": now}
            if verdict["relevant"] == "yes":
                item.pool_status = "pending"          # 主列表：可直接选择深入
                stats["promoted"] += 1
            elif verdict["relevant"] == "no" and verdict["confidence"] in ("high", "medium"):
                item.pool_status = "expired"          # AI 排除（可恢复、留审计）
                screening["excluded_by"] = "llm"
                screening["excluded_reason"] = (
                    f"AI 判定与本企业业务无关（{verdict['category']}，依据「{verdict['reason']}」）")
                stats["ai_excluded"] += 1
            else:
                item.pool_status = "needs_manual_review"  # 拿不准 → 单独页签，不进主列表
            # JSONB 字段必须整体赋值（本地 dict 变更不被 SQLAlchemy 追踪）
            item.screening = screening
            stats["classified"] += 1
        session.flush()
    return stats


def prescreen_job_pool(session: Session, search_job_id: str, **kwargs) -> dict[str, int]:
    """搜索任务落池后对其待分类条目（pending/needs_manual_review）跑 LLM 预筛（worker 挂钩）。"""
    items = session.scalars(
        select(SelectionPoolItem).where(SelectionPoolItem.pool_status.in_(["pending", "needs_manual_review"]))
    ).all()
    related = []
    from runtime.db.models import AnnouncementCandidate

    for it in items:
        cand = session.get(AnnouncementCandidate, it.candidate_id)
        if cand is not None and cand.search_job_id == search_job_id:
            related.append(it)
    if not related:
        return {"processed": 0, "classified": 0, "ai_excluded": 0, "promoted": 0, "kept_manual": 0}
    stats = prescreen_pool_items(session, items=related, **kwargs)
    if stats.get("classified"):
        api_service.audit(session, actor="llm_prescreen", action="discovery.llm_prescreen",
                          basis=f"job={search_job_id}",
                          outcome=(f"classified={stats['classified']} promoted={stats.get('promoted', 0)} "
                                   f"ai_excluded={stats['ai_excluded']}"),
                          object_ref=search_job_id)
        session.commit()
    return stats


def prescreen_backlog(session: Session, *, limit: int = 400, reset: bool = False, **kwargs) -> dict[str, int]:
    """回填：对池内待分类条目跑 LLM 预筛（脚本入口）。

    reset=True：先清空已有 llm_assist 并把 AI 排除的条目复位（needs_manual_review），
    再按当前企业业务画像全量重判——用于画像/提示词变更后重跑。
    """
    if reset:
        rows = session.scalars(select(SelectionPoolItem)).all()
        reset_n = 0
        for it in rows:
            sc = dict(it.screening or {})
            if not sc.get("llm_assist") and sc.get("excluded_by") != "llm":
                continue
            if it.pool_status in ("expired", "pending", "needs_manual_review"):
                it.pool_status = "needs_manual_review"
                sc.pop("llm_assist", None)
                sc.pop("excluded_by", None)
                sc.pop("excluded_reason", None)
                it.screening = sc
                reset_n += 1
        session.flush()
        logger.info("LLM 预筛 reset：%s 条待重判", reset_n)
    items = session.scalars(
        select(SelectionPoolItem)
        .where(SelectionPoolItem.pool_status.in_(["pending", "needs_manual_review"]))
        .order_by(SelectionPoolItem.last_seen_at.desc())
        .limit(limit)
    ).all()
    stats = prescreen_pool_items(session, items=items, **kwargs)
    if stats.get("classified"):
        api_service.audit(session, actor="llm_prescreen", action="discovery.llm_prescreen_backlog",
                          basis=f"limit={limit} reset={reset}",
                          outcome=(f"classified={stats['classified']} promoted={stats.get('promoted', 0)} "
                                   f"ai_excluded={stats['ai_excluded']}"),
                          object_ref=None)
        session.commit()
    return stats
