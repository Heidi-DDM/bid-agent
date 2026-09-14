# R021/F021 §2.7：解析候选服务层
# 职责：
#   1) store_candidates —— 解析器产出的 RuleCandidate/MainCardCandidate 落库 parse_candidates
#      （幂等：同一 (candidate_id, material_id, version) 不重复插入；状态 pending）
#   2) review 决策 —— 投标专员 approved/rejected/revised（F005 §8 人工修正留痕：谁/何时/改了什么/依据）
#   3) 确认后写入 —— approved 的规则候选 → RuleSet/Requirement；主卡字段候选 → FieldTrace
#      （历史版本不可覆盖：RuleSet (project_id,version) 唯一；Requirement.rule_set_id 归属快照）
#   4) material.parse_status 流转：pending → parsed（全部 hard 候选确认后）/
#      manual_review（存在 rejected/缺候选待补）；不静默跳过、不编造。
#   5) grouped_requirements —— F021 §2.1 v1.3 解析结果聚合：写规则集前=parse_candidates 视图，
#      确认后=Requirement(+FieldTrace) 视图，按六组业务语言确定性分组（09-优化方案 §3.3）。
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
    """候选落库（幂等）。返回 (created, skipped)。

    candidate_id 全局主键：规则候选取 requirement_id（extractor 已带 material 前缀）；
    主卡候选的 field_key（project_name 等）跨材料必相同 → 前缀 material_id 避免撞库
    （2026-09-03 MAT-TEST-001 实测：rule 候选 ND-H-001-draft 与 MAT-ND-TENDER 撞主键）。
    """
    created = 0
    skipped = 0
    for cand in candidates:
        raw_id = cand.get("requirement_id") or cand.get("field_key") or ""
        if not raw_id:
            continue
        cand_id = raw_id if kind == CANDIDATE_KIND_RULE else f"{material_id}:{raw_id}"
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
    if decision == STATUS_REJECTED:
        # F021 §2.1 v1.3：标记无法确认必须携带结构化原因枚举（扫描不清/条款冲突/未识别/需业务解释），
        # 这不是上传企业材料；原因写入 review_note，允许“原因：补充说明”格式。
        if not review_note or not any(
            review_note == r or review_note.startswith(f"{r}：") or review_note.startswith(f"{r}:")
            for r in REJECT_REASONS
        ):
            raise ParseServiceError(
                f"rejected 决策必须携带结构化原因（{' / '.join(REJECT_REASONS)}），写入 review_note"
            )
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


# ════════════════════════════════════════════════════════════════════
# F021 §2.1 v1.3 / 09-优化方案 §3.3：解析结果聚合（六组业务语言分组）
# 视图切换：写规则集前=parse_candidates 视图；确认后=Requirement(+FieldTrace) 视图。
# 分组判定确定性映射（不可判定 → 归“其他”并如实标注，禁止推断）。
# ════════════════════════════════════════════════════════════════════

# 六组固定分组（顺序即前端展示顺序；任何未命中项归“其他”）
REVIEW_GROUP_ORDER: list[str] = [
    "项目基本信息",
    "资格与资质",
    "项目经理与人员",
    "业绩、财务与信用",
    "场地、设备和其他资源",
    "投标动作与风险条款",
    "其他",
]
GROUP_BASIC = REVIEW_GROUP_ORDER[0]          # 项目基本信息
GROUP_QUALIFICATION = REVIEW_GROUP_ORDER[1]  # 资格与资质
GROUP_PERSONNEL = REVIEW_GROUP_ORDER[2]      # 项目经理与人员
GROUP_EVIDENCE = REVIEW_GROUP_ORDER[3]       # 业绩、财务与信用
GROUP_RESOURCES = REVIEW_GROUP_ORDER[4]      # 场地、设备和其他资源
GROUP_ACTION = REVIEW_GROUP_ORDER[5]         # 投标动作与风险条款
GROUP_OTHER = REVIEW_GROUP_ORDER[6]

# 规则锚点 → 业务分组（extractor.ANCHORS key 全集；新增锚点必须在此登记，否则归“其他”）
ANCHOR_GROUP: dict[str, str] = {
    # 资格与资质：企业必须具备的资质/许可
    "qualification_grade": GROUP_QUALIFICATION,
    "safety_license": GROUP_QUALIFICATION,
    # 项目经理与人员：人力资源约束
    "pm_registered_builder": GROUP_PERSONNEL,
    "pm_b_cert": GROUP_PERSONNEL,
    "pm_no_active": GROUP_PERSONNEL,
    "pm_social_security": GROUP_PERSONNEL,
    "safety_officer": GROUP_PERSONNEL,
    "tech_team": GROUP_PERSONNEL,
    # 业绩、财务与信用：证据类要求（含评分/硬性属性）
    "financial_audit": GROUP_EVIDENCE,
    "credit_no_loser": GROUP_EVIDENCE,
    "scoring_tech": GROUP_EVIDENCE,
    "scoring_similar_performance": GROUP_EVIDENCE,
    "scoring_business_credit": GROUP_EVIDENCE,
    # 场地、设备和其他资源：资源约束（是否允许联合体等）
    "consortium": GROUP_RESOURCES,
    # 投标动作与风险条款：保证金/有效期/限价/报名/递交/开标
    "bid_bond": GROUP_ACTION,
    "bid_validity": GROUP_ACTION,
    "ceiling_price": GROUP_ACTION,
    "action_file_acquisition": GROUP_ACTION,
    "action_deadline_bid": GROUP_ACTION,
    "action_bid_bond_due": GROUP_ACTION,
    "action_open": GROUP_ACTION,
}

# 规则锚点 → 可读标题（展示层确定性生成，审计可查；未登记锚点用“类别 + 待复核”）
ANCHOR_TITLE: dict[str, str] = {
    "qualification_grade": "施工总承包资质等级要求",
    "safety_license": "安全生产许可证要求",
    "pm_registered_builder": "项目经理注册建造师资格",
    "pm_b_cert": "项目经理安全生产考核合格证（B 证）",
    "pm_no_active": "项目经理在施限制",
    "pm_social_security": "项目经理社保要求",
    "safety_officer": "专职安全生产管理人员配备",
    "tech_team": "专业技术团队配备",
    "financial_audit": "财务审计报告要求",
    "credit_no_loser": "信用记录要求（失信限制）",
    "consortium": "联合体投标限制",
    "bid_validity": "投标有效期要求",
    "bid_bond": "投标保证金要求",
    "ceiling_price": "最高投标限价要求",
    "scoring_tech": "技术标评审方式",
    "scoring_similar_performance": "类似业绩评分要求",
    "scoring_business_credit": "商务标/信用评分要求",
    "action_file_acquisition": "招标文件获取（报名）",
    "action_deadline_bid": "投标文件递交截止",
    "action_bid_bond_due": "投标保证金递交截止",
    "action_open": "开标时间",
}

# 主卡字段 → 可读标题（项目基本信息组）
MAIN_CARD_TITLE: dict[str, str] = {
    "project_name": "项目名称",
    "tender_no": "项目/招标编号",
    "tenderee": "招标人",
    "agency": "招标代理机构",
    "region": "建设地点",
    "ceiling_price": "最高投标限价",
    "budget_amount": "项目总投资",
    "bid_bond_amount": "投标保证金金额",
    "deadline_signup": "报名截止时间",
    "deadline_bid": "投标文件递交截止时间",
    "open_date": "开标时间",
    "project_type": "项目类型",
}

# rejected 结构化原因枚举（F021 §2.1 v1.3：标记无法确认必须携带原因，非上传材料）
REJECT_REASONS: tuple[str, ...] = ("扫描不清", "条款冲突", "未识别", "需业务解释")


def _anchor_key_of(payload: dict) -> str | None:
    rule = payload.get("rule") or {}
    return rule.get("anchor_key") if isinstance(rule, dict) else None


def _review_title(payload: dict, *, kind: str) -> str:
    """可读标题：锚点登记表 / 主卡字段表 → 兜底“类别 + 待复核”（不推断）。"""
    if kind == CANDIDATE_KIND_FIELD:
        fk = payload.get("field_key") or ""
        return MAIN_CARD_TITLE.get(fk) or f"{fk}（待复核）"
    anchor = _anchor_key_of(payload)
    if anchor and anchor in ANCHOR_TITLE:
        return ANCHOR_TITLE[anchor]
    category = payload.get("category") or "未分类"
    return f"{category}要求（待复核）"


def _review_group(payload: dict, *, kind: str) -> str:
    """确定性分组：main_card_field → 项目基本信息；action → 动作组；否则按锚点表；兜底“其他”。"""
    if kind == CANDIDATE_KIND_FIELD:
        return GROUP_BASIC
    if payload.get("req_type") == "action_requirement":
        return GROUP_ACTION
    anchor = _anchor_key_of(payload)
    if anchor and anchor in ANCHOR_GROUP:
        return ANCHOR_GROUP[anchor]
    return GROUP_OTHER


def _issue_text(payload: dict, row: ParseCandidate | None = None) -> str | None:
    """缺失/歧义原因：missing_marker 候选 → note；低置信且无锚点 → 如实标注；无则 None。"""
    if payload.get("missing_marker"):
        return payload.get("note") or "候选缺失/歧义：请人工确认原文含义"
    if row is not None and row.status == STATUS_REJECTED:
        return row.review_note or "标记无法确认（原因见复核记录）"
    return None


def grouped_requirements(
    session: Session, *, project_id: str, material_id: str | None = None, version: int | None = None,
) -> dict[str, Any]:
    """F021 §2.1 解析结果聚合：返回 {source, project_id, material_id, version, groups}。

    source 判定：
    - 规则集已写入（RuleSet 存在且材料 parsed/confirmed）→ "confirmed"：读 Requirement + FieldTrace；
    - 否则 → "candidates"：读 parse_candidates 全量（rule_candidate + main_card_field）。
    前端六组顺序 REVIEW_GROUP_ORDER 展示；audit 折叠放 material_id/content_hash/内部编码。
    """
    from runtime.db.models import Project

    # 定位该项目招标材料（显式传参优先，否则取项目最新招标文件）
    if material_id is None:
        project = session.get(Project, project_id)
        material_id = project.tender_document_ref if project else None
    if material_id is None:
        material = session.scalar(
            select(Material)
            .where(Material.project_id == project_id,
                   Material.material_type == "tender_document")
            .order_by(Material.version.desc())
            .limit(1)
        )
        if material is not None:
            material_id, version = material.material_id, material.version
    if material_id is None:
        return {"source": None, "project_id": project_id, "material_id": None,
                "version": None, "groups": [], "progress": {"total": 0, "pending": 0, "decided": 0}}
    material = session.scalar(
        select(Material)
        .where(Material.material_id == material_id)
        .order_by(Material.version.desc())
        .limit(1)
    )
    if material is None:
        return {"source": None, "project_id": project_id, "material_id": material_id,
                "version": None, "groups": [], "progress": {"total": 0, "pending": 0, "decided": 0}}
    version = version or material.version
    rule_set_id = material_ruleset_id(material_id, version)
    confirmed = session.get(RuleSet, rule_set_id) is not None

    groups: dict[str, list[dict]] = {g: [] for g in REVIEW_GROUP_ORDER}
    content_hash = material.content_hash
    audit = {"material_id": material_id, "content_hash": (content_hash or "")[:16]}

    if confirmed:
        # ── confirmed 视图：Requirement（规则） + FieldTrace（主卡字段溯源） ──
        reqs = session.scalars(
            select(Requirement).where(Requirement.rule_set_id == rule_set_id)
            .order_by(Requirement.requirement_id)
        ).all()
        for req in reqs:
            payload = {
                "requirement_id": req.requirement_id,
                "req_type": req.req_type,
                "category": req.category,
                "clause_ref": req.clause_ref,
                "assertion": req.assertion,
                "rule": req.rule,
            }
            item = {
                "id": req.requirement_id,
                "title": _review_title(payload, kind=CANDIDATE_KIND_RULE),
                "requirement_type": "hard" if req.req_type == "hard_requirement"
                else ("scored" if req.req_type == "scored_requirement" else "action"),
                "value": None,  # Requirement 无结构化 value 列；要求值见 assertion（不推断）
                "assertion": req.assertion,
                "clause_ref": req.clause_ref,
                "page_no": None,
                "confidence": None,
                "review_status": "confirmed",
                "issue": None,
                "source_link": None,
                "audit": dict(audit, requirement_id=req.requirement_id),
            }
            groups[_review_group(payload, kind=CANDIDATE_KIND_RULE)].append(item)
        traces = session.scalars(
            select(FieldTrace)
            .where(FieldTrace.object_id == f"{material_id}:v{version}")
            .order_by(FieldTrace.field_key)
        ).all()
        for tr in traces:
            groups[GROUP_BASIC].append({
                "id": f"{tr.object_id}:{tr.field_key}",
                "title": MAIN_CARD_TITLE.get(tr.field_key) or tr.field_key,
                "requirement_type": None,
                "value": tr.assertion,  # 主卡字段的“要求值”即已确认字段内容（FieldTrace 溯源）
                "assertion": tr.assertion,
                "clause_ref": tr.clause,
                "page_no": None,
                "confidence": tr.confidence,
                "review_status": "confirmed",
                "issue": None,
                "source_link": tr.source_link,
                "audit": dict(audit, field_key=tr.field_key),
            })
        source = "confirmed"
    else:
        # ── candidates 视图：待复核/已决候选全量（rule + field） ──
        rows = session.scalars(
            select(ParseCandidate)
            .where(ParseCandidate.project_id == project_id,
                   ParseCandidate.material_id == material_id,
                   ParseCandidate.version == version)
            .order_by(ParseCandidate.created_at, ParseCandidate.kind)
        ).all()
        for row in rows:
            payload = row.payload or {}
            req_type = payload.get("req_type")
            item = {
                "id": row.candidate_id,
                "title": _review_title(payload, kind=row.kind),
                "requirement_type": "hard" if req_type == "hard_requirement"
                else ("scored" if req_type == "scored_requirement"
                      else ("action" if req_type == "action_requirement" else None)),
                "value": (payload.get("value") if row.kind == CANDIDATE_KIND_FIELD else
                          ((row.revised_payload or {}).get("value"))),
                "assertion": payload.get("assertion") or "",
                "clause_ref": payload.get("clause_ref") or payload.get("clause") or "",
                "page_no": payload.get("page_no"),
                "confidence": payload.get("confidence"),
                "review_status": row.status,
                "issue": _issue_text(payload, row),
                "source_link": payload.get("source_link"),
                "audit": dict(audit, kind=row.kind,
                              requirement_id=payload.get("requirement_id"),
                              field_key=payload.get("field_key")),
            }
            groups[_review_group(payload, kind=row.kind)].append(item)
        source = "candidates"

    return {
        "source": source,
        "project_id": project_id,
        "material_id": material_id,
        "version": version,
        "groups": [
            {"group": g, "group_label": g, "items": groups[g]}
            for g in REVIEW_GROUP_ORDER
        ],
        "progress": pending_summary(session, project_id=project_id, material_id=material_id),
    }
