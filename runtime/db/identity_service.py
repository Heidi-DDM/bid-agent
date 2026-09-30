# ADR-004 §2.3 / 优化方案 §7.1 §12.1：项目身份校验服务层（P0-02 防串档）
#
# 职责：
# - 从既有事实自动组装「公告侧（expected）」与「招标文件侧（actual）」字段（只取已确认/已抽取
#   且非缺失的值，不推断）；调用方可显式传入字段覆盖自动推导（人工核对录入）；
# - 调用纯逻辑 runtime/core/identity.compare_identity 判定三态，落库 project_identities（每项目一行）；
# - 招标文件侧/公告侧给出的投标截止回写 projects.bid_deadline（P0-03 截止门禁的数据来源）；
# - 人工确认（warning → confirmed）留痕；conflict 不允许一键确认，须纠正数据后重新校验。
# 本模块只做 flush/commit 由调用方决定的最小提交（evaluate/confirm 内部 commit，与 api_service 一致）。
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime.db.lifecycle_service import deadline_at_cn

from runtime.core import identity as identity_logic
from runtime.core.errors import ApiError
from runtime.db.models import (
    AnnouncementCandidate,
    Material,
    ParseCandidate,
    Project,
    ProjectIdentity,
)

logger = logging.getLogger("runtime.db.identity_service")

# 公告初筛 detail_summary key → 身份字段（按优先级顺序；同一身份字段首个非缺失值生效）
ANNOUNCEMENT_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("purchaser", "purchaser"),
    ("region", "location"),
    ("deadline_bid", "bid_deadline"),
    ("ceiling_price", "budget_amount"),
    ("budget_amount", "budget_amount"),
    ("tender_no", "tender_no"),
    ("procurement_no", "procurement_no"),
)
# 招标文件主卡 field_key → 身份字段
TENDER_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("project_name", "project_name"),
    ("tender_no", "tender_no"),
    ("tenderee", "purchaser"),
    ("purchaser", "purchaser"),
    ("region", "location"),
    ("project_type", "project_type"),
    ("deadline_bid", "bid_deadline"),
    ("ceiling_price", "budget_amount"),
    ("budget_amount", "budget_amount"),
)
_MISSING_MARKERS = {None, "", "__待补__", "待补", "待核实"}
_CANDIDATE_APPROVED = ("approved", "revised")


def _clean_value(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
    return None if value in _MISSING_MARKERS else value


def _put(target: dict[str, Any], key: str, value: Any) -> None:
    value = _clean_value(value)
    if value is not None and key not in target:
        target[key] = value


# ---------- 自动推导 ----------

def derive_expected(session: Session, project: Project) -> tuple[dict[str, Any], list[str]]:
    """公告侧字段：项目主卡名称 + 公告候选初筛 detail_summary（非缺失字段）。"""
    expected: dict[str, Any] = {}
    refs: list[str] = [f"project:{project.project_id}"]
    _put(expected, "project_name", project.project_name)
    candidate = session.scalar(
        select(AnnouncementCandidate)
        .where(AnnouncementCandidate.project_id == project.project_id)
        .order_by(AnnouncementCandidate.updated_at.desc())
        .limit(1)
    )
    if candidate is not None:
        refs.append(f"announcement_candidate:{candidate.candidate_id}")
        summary = candidate.detail_summary or {}
        for src, dst in ANNOUNCEMENT_FIELD_MAP:
            entry = summary.get(src)
            if not isinstance(entry, dict) or entry.get("missing"):
                continue
            _put(expected, dst, entry.get("value"))
    return expected, refs


def derive_actual(session: Session, project: Project) -> tuple[dict[str, Any], list[str]]:
    """招标文件侧字段：项目下 tender_document 材料（各取最新版本）已人工确认的主卡候选。"""
    actual: dict[str, Any] = {}
    refs: list[str] = []
    materials = session.scalars(
        select(Material)
        .where(Material.project_id == project.project_id, Material.material_type == "tender_document")
        .order_by(Material.material_id, Material.version.desc())
    ).all()
    seen: set[str] = set()
    for material in materials:
        if material.material_id in seen:
            continue  # 已取最新版本
        seen.add(material.material_id)
        refs.append(f"material:{material.material_id}:v{material.version}")
        rows = session.scalars(
            select(ParseCandidate).where(
                ParseCandidate.project_id == project.project_id,
                ParseCandidate.material_id == material.material_id,
                ParseCandidate.version == material.version,
                ParseCandidate.kind == "main_card_field",
                ParseCandidate.status.in_(_CANDIDATE_APPROVED),
            )
        ).all()
        by_key: dict[str, Any] = {}
        for row in rows:
            payload = row.revised_payload or row.payload or {}
            if payload.get("missing"):
                continue
            key = payload.get("field_key") or row.candidate_id.split(":", 1)[-1]
            by_key.setdefault(key, payload.get("value"))
        for src, dst in TENDER_FIELD_MAP:
            if src in by_key:
                _put(actual, dst, by_key[src])
    return actual, refs


# ---------- 落库辅助 ----------

_CN_TZ = timezone(timedelta(hours=8))


def _to_day(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(_CN_TZ).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    m = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", str(value))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _to_deadline_at(value: Any) -> datetime | None:
    """仅在原始值明确给出时分秒时解析精确截止，统一为中国时区。

    只给日期的事实返回 None，禁止伪造 00:00 或 23:59:59。ISO 值若带偏移按其
    原始时区换算；中文/无时区值按招投标业务中国时区解释。
    """
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return None
    if isinstance(value, datetime):
        return (value.replace(tzinfo=_CN_TZ) if value.tzinfo is None else value.astimezone(_CN_TZ))
    text = str(value).strip()
    # ISO 8601 的 T/空格时刻输入，含 Z 兼容；日期-only 解析后 hour=0，必须排除。
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if "T" in text or re.search(r"\d{1,2}:\d{2}", text):
            return parsed.replace(tzinfo=_CN_TZ) if parsed.tzinfo is None else parsed.astimezone(_CN_TZ)
    except ValueError:
        pass
    m = re.search(
        r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?\s*"
        r"(\d{1,2})[时:：](\d{1,2})(?:[分:：](\d{1,2}))?", text
    )
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        int(m.group(4)), int(m.group(5)), int(m.group(6) or 0), tzinfo=_CN_TZ)
    except ValueError:
        return None


def _to_amount(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = re.sub(r"[,\s，元人民币￥¥]", "", str(value))
    mult = Decimal(1)
    if text.endswith("亿"):
        mult, text = Decimal(10**8), text[:-1]
    elif text.endswith("万"):
        mult, text = Decimal(10**4), text[:-1]
    try:
        return Decimal(text) * mult
    except (InvalidOperation, ValueError):
        return None


def _upsert_identity(
    session: Session,
    *,
    project: Project,
    expected: dict[str, Any],
    actual: dict[str, Any],
    result: identity_logic.IdentityResult,
    refs: list[str],
    actor: str,
) -> ProjectIdentity:
    row = session.get(ProjectIdentity, project.project_id)
    if row is None:
        row = ProjectIdentity(project_id=project.project_id)
        session.add(row)
    merged = {**expected, **actual}  # 招标文件侧优先（正式匹配依据）
    row.normalized_project_name = identity_logic.normalize_project_name(
        merged.get("project_name")) or None
    row.purchaser = str(merged["purchaser"])[:256] if merged.get("purchaser") else None
    row.location = str(merged["location"])[:128] if merged.get("location") else None
    row.project_type = str(merged["project_type"])[:64] if merged.get("project_type") else None
    row.announcement_no = (str(merged.get("announcement_no") or merged.get("tender_no"))[:128]
                           if (merged.get("announcement_no") or merged.get("tender_no")) else None)
    row.procurement_no = str(merged["procurement_no"])[:128] if merged.get("procurement_no") else None
    row.lot_id = str(merged["lot_id"])[:64] if merged.get("lot_id") else None
    row.budget_amount = _to_amount(merged.get("budget_amount"))
    deadline_value = merged.get("bid_deadline")
    row.bid_deadline = _to_day(deadline_value)
    row.bid_deadline_at = _to_deadline_at(deadline_value)
    row.identity_status = result.status
    row.identity_conflicts = [c.as_dict() for c in result.conflicts]
    row.identity_warnings = [w.as_dict() for w in result.warnings]
    row.compared_fields = list(result.compared_fields)
    row.expected_snapshot = {k: str(v) for k, v in expected.items()}
    row.actual_snapshot = {k: str(v) for k, v in actual.items()}
    row.source_refs = list(refs)
    row.checked_by = actor
    row.checked_at = _now()
    # 重新校验后人工确认失效（输入已变，须重新确认）
    row.confirmed_by = None
    row.confirmed_at = None
    # P0-03 数据来源：截止时间回写项目（招标文件侧优先；缺失不覆盖既有值）
    if row.bid_deadline is not None:
        project.bid_deadline = row.bid_deadline
        project.bid_deadline_at = row.bid_deadline_at
    return row


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def identity_payload(row: ProjectIdentity | None) -> dict[str, Any]:
    """ProjectIdentity → API/前端 DTO；无记录时明确 `status=null`（未校验 ≠ 已确认）。"""
    if row is None:
        return {"status": None, "checked_at": None, "blocks_matching": False,
                "note": "尚未执行项目身份校验（未校验不等于已确认，正式匹配前须校验）"}
    return {
        "status": row.identity_status,
        "blocks_matching": row.identity_status == identity_logic.IDENTITY_CONFLICT,
        "conflicts": row.identity_conflicts or [],
        "warnings": row.identity_warnings or [],
        "compared_fields": row.compared_fields or [],
        "expected": row.expected_snapshot or {},
        "actual": row.actual_snapshot or {},
        "bid_deadline": row.bid_deadline.isoformat() if row.bid_deadline else None,
        "bid_deadline_at": deadline_at_cn(row.bid_deadline_at).isoformat() if row.bid_deadline_at else None,
        "deadline_precision": "datetime" if row.bid_deadline_at else ("date" if row.bid_deadline else "missing"),
        "source_refs": row.source_refs or [],
        "checked_by": row.checked_by,
        "checked_at": row.checked_at.isoformat() if row.checked_at else None,
        "confirmed_by": row.confirmed_by,
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
    }


# ---------- 对外操作 ----------

def evaluate_project_identity(
    session: Session,
    *,
    project_id: str,
    actor: str,
    expected: dict[str, Any] | None = None,
    actual: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行身份校验并落库（每项目一行，覆盖上次结果，审计留痕）。

    expected/actual 显式字段覆盖自动推导（人工核对录入）；返回 identity_payload。
    """
    from runtime.db import api_service

    project = session.get(Project, project_id)
    if project is None:
        raise ApiError("not_found", f"项目不存在: {project_id}")
    auto_expected, refs_e = derive_expected(session, project)
    auto_actual, refs_a = derive_actual(session, project)
    merged_expected = {**auto_expected, **{k: v for k, v in (expected or {}).items() if _clean_value(v) is not None}}
    merged_actual = {**auto_actual, **{k: v for k, v in (actual or {}).items() if _clean_value(v) is not None}}
    result = identity_logic.compare_identity(merged_expected, merged_actual)
    refs = refs_e + refs_a
    if expected or actual:
        refs.append(f"manual_input:{actor}")
    row = _upsert_identity(
        session, project=project, expected=merged_expected, actual=merged_actual,
        result=result, refs=refs, actor=actor,
    )
    api_service.audit(
        session, actor=actor, action="project.identity_checked",
        basis=f"compared={','.join(result.compared_fields) or '-'} refs={len(refs)}",
        outcome=f"{result.status} conflicts={len(result.conflicts)} warnings={len(result.warnings)}",
        object_ref=project_id,
    )
    session.commit()
    logger.info("项目身份校验 project_id=%s status=%s conflicts=%s warnings=%s",
                project_id, result.status, len(result.conflicts), len(result.warnings))
    return identity_payload(row)


def ensure_project_identity(session: Session, *, project_id: str, actor: str = "system") -> ProjectIdentity | None:
    """无校验记录时自动评估一次（不覆盖既有/人工确认结果）；返回记录（可能为 None）。"""
    row = session.get(ProjectIdentity, project_id)
    if row is not None:
        return row
    if session.get(Project, project_id) is None:
        return None
    evaluate_project_identity(session, project_id=project_id, actor=actor)
    return session.get(ProjectIdentity, project_id)


def confirm_project_identity(session: Session, *, project_id: str, actor: str, note: str | None = None) -> dict[str, Any]:
    """人工确认为同一项目：仅 identity_warning 可确认 → identity_confirmed；conflict 须纠正数据后重新校验。"""
    from runtime.db import api_service

    row = session.get(ProjectIdentity, project_id)
    if row is None:
        raise ApiError("invalid_state_transition", "尚未执行身份校验，不能直接确认（先校验再确认）")
    if row.identity_status == identity_logic.IDENTITY_CONFLICT:
        raise ApiError("invalid_state_transition",
                       "存在硬冲突/高风险冲突，不允许一键确认：请纠正项目关联或材料后重新校验（ADR-004 §2.3）")
    if not note or not note.strip():
        raise ApiError("invalid_request", "人工确认必须填写依据（note 必填）")
    row.identity_status = identity_logic.IDENTITY_CONFIRMED
    row.confirmed_by = actor
    row.confirmed_at = _now()
    api_service.audit(
        session, actor=actor, action="project.identity_confirmed",
        basis=note.strip()[:500], outcome=identity_logic.IDENTITY_CONFIRMED, object_ref=project_id,
    )
    session.commit()
    return identity_payload(row)


def identity_status(session: Session, project_id: str) -> str | None:
    row = session.get(ProjectIdentity, project_id)
    return row.identity_status if row is not None else None
