# R021/F021 §2.7：解析候选服务层
# 职责：
#   1) store_candidates —— 解析器产出的 RuleCandidate/MainCardCandidate 落库 parse_candidates
#      （幂等：同一 (candidate_id, material_id, version) 不重复插入；状态 pending）
#   2) review 决策 —— 投标专员 approved/rejected/revised（F005 §8 人工修正留痕：谁/何时/改了什么/依据）
#   3) 确认后写入 —— approved 的规则候选 → RuleSet/Requirement；主卡字段候选 → FieldTrace
#      （历史版本不可覆盖：RuleSet (project_id,version) 唯一；Requirement.rule_set_id 归属快照）
#   4) material.parse_status 流转：pending → parsed（全部 hard 候选确认后）/
#      manual_review（存在 rejected/缺候选待补）；不静默跳过、不编造。
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from runtime.db.models import FieldTrace, Material, ParseCandidate, Requirement, RuleSet

CANDIDATE_KIND_RULE = "rule_candidate"
CANDIDATE_KIND_FIELD = "main_card_field"

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_REVISED = "revised"

TERMINAL_STATUSES = (STATUS_APPROVED, STATUS_REJECTED, STATUS_REVISED)

# 硬性必查类别（缺失候选必须有人工处理才能 parsed；与 extractor 的 missing 语义一致）
HARD_MISSING_CATEGORIES = {"资质", "人员", "财务", "信用", "联合体", "响应性", "保证金"}


class ParseServiceError(Exception):
    pass


class CandidateNotFound(ParseServiceError):
    pass


class CandidateAlreadyDecided(ParseServiceError):
    pass


class RuleSetExists(ParseServiceError):
    """规则集已存在且非同一草案（历史版本不可覆盖）。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── 落库 ──────────────────────────────────────────────────────────────


def store_candidates(
    session: Session,
    *,
    project_id: str,
    material_id: str,
    version: int,
    kind: str,
    candidates: list[dict],
    actor: str = "parse.worker",
) -> tuple[int, int]:
    """候选落库（幂等）。返回 (created, skipped)。"""
    created = 0
    skipped = 0
    for cand in candidates:
        cand_id = cand.get("requirement_id") or cand.get("field_key") or ""
        if not cand_id:
            continue
        existing = session.scalar(
            select(ParseCandidate).where(
                ParseCandidate.candidate_id == cand_id,
                ParseCandidate.material_id == material_id,
                ParseCandidate.version == version,
            )
        )
        if existing is not None:
            skipped += 1
            continue
        session.add(ParseCandidate(
            candidate_id=cand_id,
            material_id=material_id,
            version=version,
            project_id=project_id,
            kind=kind,
            payload=cand,
            status=STATUS_PENDING,
            created_at=_now(),
            updated_at=_now(),
        ))
        created += 1
    session.commit()
    return created, skipped


# ── 查询 ──────────────────────────────────────────────────────────────


def list_candidates(
    session: Session,
    *,
    project_id: str | None = None,
    material_id: str | None = None,
    status: str | None = None,
    kind: str | None = None,
) -> list[dict]:
    q = select(ParseCandidate).order_by(ParseCandidate.created_at)
    if project_id:
        q = q.where(ParseCandidate.project_id == project_id)
    if material_id:
        q = q.where(ParseCandidate.material_id == material_id)
    if status:
        q = q.where(ParseCandidate.status == status)
    if kind:
        q = q.where(ParseCandidate.kind == kind)
    rows = session.scalars(q).all()
    return [_row_dict(r) for r in rows]


def _row_dict(row: ParseCandidate) -> dict:
    return {
        "candidate_id": row.candidate_id,
        "material_id": row.material_id,
        "version": row.version,
        "project_id": row.project_id,
        "kind": row.kind,
        "payload": row.payload,
        "status": row.status,
        "reviewer": row.reviewer,
        "revised_payload": row.revised_payload,
        "review_note": row.review_note,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "created_at": row.created_at.isoformat(),
    }


def pending_summary(session: Session, *, project_id: str, material_id: str) -> dict:
    """复核进度（前端展示）：总数/已决/待决 + 硬性缺失项。"""
    total = session.scalar(
        select(func.count()).select_from(ParseCandidate).where(
            ParseCandidate.project_id == project_id,
            ParseCandidate.material_id == material_id,
        )
    )
    pending = session.scalar(
        select(func.count()).select_from(ParseCandidate).where(
            ParseCandidate.project_id == project_id,
            ParseCandidate.material_id == material_id,
            ParseCandidate.status == STATUS_PENDING,
        )
    )
    return {"total": total or 0, "pending": pending or 0,
            "decided": (total or 0) - (pending or 0)}


# ── 复核决策（F005 §8 留痕） ─────────────────────────────────────────


def decide_candidate(
    session: Session,
    *,
    candidate_id: str,
    material_id: str,
    version: int,
    decision: str,
    reviewer: str,
    review_note: str | None = None,
    revised_payload: dict | None = None,
) -> dict:
    """投标专员复核：approved / rejected / revised（revised 须带修正 payload）。

    留痕：reviewer / review_note / revised_payload / decided_at；
    终态不可重复决策（F005 §8 + F020 一致性）。
    """
    if decision not in (STATUS_APPROVED, STATUS_REJECTED, STATUS_REVISED):
        raise ParseServiceError(f"不支持决策: {decision}")
    if decision == STATUS_REVISED and not revised_payload:
        raise ParseServiceError("revised 决策必须携带 revised_payload（人工修正值/依据）")
    row = session.scalar(
        select(ParseCandidate).where(
            ParseCandidate.candidate_id == candidate_id,
            ParseCandidate.material_id == material_id,
            ParseCandidate.version == version,
        )
    )
    if row is None:
        raise CandidateNotFound(f"候选不存在: {candidate_id}@{material_id}:v{version}")
    if row.status in TERMINAL_STATUSES:
        raise CandidateAlreadyDecided(f"候选已决策（{row.status}），终态不可重复决策")
    row.status = decision
    row.reviewer = reviewer
    row.review_note = review_note
    row.revised_payload = revised_payload
    row.decided_at = _now()
    row.updated_at = _now()
    session.commit()
    return _row_dict(row)


def material_parse_status(session: Session, *, project_id: str, material_id: str) -> str:
    """按候选复核进度推导 parse_status：
    - 无候选记录 → pending
    - 全部决策且无 pending → parsed（已确认通过）
    - 存在 missing_marker 候选且仍 pending 或 rejected → manual_review
    """
    rows = session.scalars(
        select(ParseCandidate).where(
            ParseCandidate.project_id == project_id,
            ParseCandidate.material_id == material_id,
        )
    ).all()
    if not rows:
        return "pending"
    if any(r.status == STATUS_PENDING for r in rows):
        return "manual_review"  # 有待决候选：人工复核中
    # 全部已决：rejected 存在 → manual_review（需人工补录/修正）；否则 parsed
    rejected = [r for r in rows if r.status == STATUS_REJECTED]
    if rejected:
        return "manual_review"
    return "parsed"


# ── 确认后写入（F021 §2.7：复核通过 → RuleSet/Requirement/FieldTrace） ──


def material_ruleset_id(material_id: str, version: int) -> str:
    """规则集草案 ID：RS-<MATERIAL>-v<version>（材料版本对齐，历史版本不可覆盖）。"""
    return f"RS-{material_id}-v{version}"


def confirm_rules_from_approved(
    session: Session,
    *,
    project_id: str,
    material_id: str,
    version: int,
    as_of: str,
    created_by: str,
) -> dict:
    """把全部 approved 规则候选写入 RuleSet/Requirement（快照式，历史不可覆盖）。

    仅 hard/scored 候选写入；rejected/revised 的处理：
    - rejected → 不写入（缺项由 review_note 记录，parse_status 保持 manual_review）
    - revised → 写入 revised_payload 内容（人工修正值，留痕可查）
    返回 {rule_set_id, created_requirements, rejected, pending}。
    """
    rows = session.scalars(
        select(ParseCandidate).where(
            ParseCandidate.project_id == project_id,
            ParseCandidate.material_id == material_id,
            ParseCandidate.version == version,
            ParseCandidate.kind == CANDIDATE_KIND_RULE,
        )
    ).all()
    if not rows:
        return {"rule_set_id": None, "created_requirements": 0, "rejected": 0, "pending": 0}

    rule_set_id = material_ruleset_id(material_id, version)
    existing = session.get(RuleSet, rule_set_id)
    if existing is not None:
        raise RuleSetExists(
            f"规则集 {rule_set_id} 已存在（历史版本不可覆盖；澄清应产生新 Material 版本）"
        )

    approved = [r for r in rows if r.status in (STATUS_APPROVED, STATUS_REVISED)]
    rejected = [r for r in rows if r.status == STATUS_REJECTED]
    pending = [r for r in rows if r.status == STATUS_PENDING]
    if pending:
        raise ParseServiceError(f"仍有 {len(pending)} 个待决候选，不能生成规则集")

    snapshot_requirements = []
    session.add(RuleSet(
        rule_set_id=rule_set_id,
        project_id=project_id,
        version=f"v{version}",
        effective_from=None,
        created_by=created_by,
        snapshot={
            "source": f"material={material_id}:v{version}",
            "as_of": as_of,
            "confirmed_by": created_by,
            "requirement_count": len(approved),
        },
        diff=None,
    ))
    created = 0
    for row in approved:
        payload = row.revised_payload or row.payload
        req_id = payload.get("requirement_id", row.candidate_id)
        req_type = payload.get("req_type", "hard_requirement")
        # missing_marker 候选（__待补__）不得写为正式规则（缺失阻断）
        if payload.get("missing_marker") or not payload.get("assertion") or payload.get("assertion") == "__待补__":
            continue
        session.add(Requirement(
            requirement_id=f"{req_id}",
            rule_set_id=rule_set_id,
            req_type=req_type,
            category=payload.get("category"),
            lot_id=None,
            clause_ref=payload.get("clause_ref") or "",
            assertion=payload.get("assertion") or "",
            rule=payload.get("rule") or {},
            evidence_required=payload.get("evidence_required") or [],
            as_of=as_of,
            missing_action="blocked_missing_data",
            failure_effect="not_qualified" if req_type == "hard_requirement" else None,
            priority=None,
            logic_group=req_type,
            operator=None,
            consortium_role="none",
            max_score=payload.get("max_score"),
            weight=payload.get("weight"),
            score_nature=payload.get("score_nature"),
            score_formula=payload.get("score_formula"),
            required_by_stage=payload.get("required_by_stage"),
        ))
        created += 1
        snapshot_requirements.append({
            "requirement_id": req_id, "req_type": req_type,
            "clause_ref": payload.get("clause_ref"),
        })
    session.commit()
    return {"rule_set_id": rule_set_id, "created_requirements": created,
            "rejected": len(rejected), "pending": len(pending)}


def confirm_main_card_fields(
    session: Session,
    *,
    material_id: str,
    version: int,
    project_id: str,
    actor: str,
    content_hash: str | None = None,
) -> int:
    """把 approved 主卡字段候选写入 FieldTrace（F005 §4.3 字段级溯源）。返回写入数。

    只写非缺失字段；__待补__/rejected 不写（缺失在解析复核 UI 呈现）。
    """
    rows = session.scalars(
        select(ParseCandidate).where(
            ParseCandidate.project_id == project_id,
            ParseCandidate.material_id == material_id,
            ParseCandidate.version == version,
            ParseCandidate.kind == CANDIDATE_KIND_FIELD,
            ParseCandidate.status.in_((STATUS_APPROVED, STATUS_REVISED)),
        )
    ).all()
    written = 0
    for row in rows:
        payload = row.revised_payload or row.payload
        value = payload.get("value") or ""
        if not value or value == "__待补__":
            continue
        session.add(FieldTrace(
            trace_id=f"FT-{uuid.uuid4().hex[:16]}",
            object_id=f"{material_id}:v{version}",
            field_key=payload.get("field_key") or row.candidate_id,
            clause=payload.get("clause") or payload.get("clause_ref") or "",
            source_link=payload.get("source_link"),
            assertion=payload.get("assertion") or value,
            confidence=payload.get("confidence"),
            source_hash=content_hash,
        ))
        written += 1
    if written:
        session.commit()
    return written


def mark_material_parsed(session: Session, *, material_id: str, version: int,
                         actor: str = "parse.review") -> None:
    """复核全部通过 → material.parse_status = parsed（触发后续索引/匹配编排）。"""
    session.execute(
        update(Material)
        .where(Material.material_id == material_id, Material.version == version)
        .values(parse_status="parsed", updated_at=_now())
    )
    session.commit()


def mark_material_manual_review(session: Session, *, material_id: str, version: int,
                                note: str = "存在待决/驳回候选") -> None:
    session.execute(
        update(Material)
        .where(Material.material_id == material_id, Material.version == version)
        .values(parse_status="manual_review", updated_at=_now())
    )
    session.commit()
