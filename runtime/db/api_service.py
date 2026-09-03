# F020：API 业务服务层（SQLAlchemy 会话）
# 提供材料/项目/审批/豁免/审计/队列/矩阵查询；写操作一律附加审计事件（F009 §9）。
# 依赖：runtime.db.models（F019 四层 schema）+ material_service（导入）。
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runtime.core import rbac
from runtime.core.errors import ApiError
from runtime.db.models import (
    AdmissionResult,
    Approval,
    AuditEvent,
    Material,
    MatchItem,
    MatchRun,
    Project,
    Qualification,
    Waiver,
)

logger = logging.getLogger("runtime.routers_service")

# 项目状态映射（F020 §2.2 / STATE_META 13 态子集；由准入结果推导）
PROJECT_STATE_ORDER = [
    "draft", "collecting", "parsed", "matching", "blocked_missing_data",
    "blocked_hard_requirement", "not_qualified", "qualified_full_score",
    "pending_bid_approval", "approved_for_bidding", "rejected_by_approver",
    "blocked_waiver_expired", "archived",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_datetime() -> datetime:
    return datetime.now(timezone.utc)


def audit(session: Session, *, actor: str, action: str, basis: str | None = None,
          outcome: str | None = None, object_ref: str | None = None) -> AuditEvent:
    """写审计事件（仅追加，不提供 UPDATE/DELETE 应用路径）。"""
    event = AuditEvent(
        event_id=uuid.uuid4().hex[:16],
        actor=actor,
        at=datetime.now(timezone.utc),
        action=action,
        basis=basis,
        outcome=outcome,
        object_ref=object_ref,
    )
    session.add(event)
    return event


def require_scope_or_403(session: Session, role: str, material: Material) -> None:
    """按 F003 §6.2 权限矩阵校验材料可见性；拒绝时写越权审计并抛 403。"""
    if rbac.can_read_scope(role, material.permission_scope):
        return
    audit(session, actor=role, action=f"rbac.deny.read.material",
          basis=f"permission_scope={material.permission_scope}",
          outcome="denied", object_ref=material.material_id)
    session.commit()
    raise ApiError("forbidden", "无权访问该材料（permission_scope 拒绝）")


def project_exists(session: Session, project_id: str) -> bool:
    return session.get(Project, project_id) is not None


def get_project_or_404(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise ApiError("not_found", f"项目不存在: {project_id}")
    return project


def get_material_or_404(session: Session, material_id: str) -> Material:
    material = session.scalar(
        select(Material)
        .where(Material.material_id == material_id)
        .order_by(Material.version.desc())
        .limit(1)
    )
    if material is None:
        raise ApiError("not_found", f"材料不存在: {material_id}")
    return material


def schedule_post_parse(session: Session, *, project_id: str) -> dict:
    """人工复核确认（parse_status=parsed）后的编排（R021-5 / F021 §2 第 7 步）。

    1) 为项目内已 parsed 且可索引的材料自动创建 knowledge_index 任务（幂等 index:… 键）；
    2) 尚无匹配 run 时创建一次 match.run（幂等 kind+input_ref+project_id 键）。

    F021 §2 第 7 步：投标专员确认后项目进入 parsed，编排器随后自动创建一次匹配任务，
    项目状态进入 matching；重复轮询/刷新不得重复触发。返回 {"index_jobs", "match_job"}。
    """
    from runtime.core import orchestration
    from runtime.rag import service as rag_service
    from runtime.db import worker_service
    from runtime.db.models import Material

    materials = session.scalars(
        select(Material).where(Material.project_id == project_id)
    ).all()
    material_dicts = [
        {
            "material_id": m.material_id,
            "material_type": m.material_type,
            "parse_status": m.parse_status,
            "version": m.version,
        }
        for m in materials
    ]

    # 1) 知识索引：已解析材料 → L2/L3 索引任务（幂等；向量不可用时不伪造成功）
    from typing import cast

    from runtime.db.models import AnalysisJob

    index_jobs: list[str] = []
    for material_id, version in orchestration.materials_needing_index(material_dicts):
        job, created = rag_service.create_index_job(
            session, material_id=material_id, version=version, project_id=project_id
        )
        index_jobs.append(cast(AnalysisJob, job).job_id)
        audit(session, actor="system", action="knowledge.index_triggered",
              basis=f"material={material_id}:v{version}",
              outcome="created" if created else "idempotent_reuse",
              object_ref=project_id)

    # 2) 首次匹配：尚无 run 才触发（F020 §2.1 幂等）
    run = latest_match_run(session, project_id)
    existing_runs = 1 if run is not None else 0
    match_job: str | None = None
    if orchestration.should_trigger_first_match(material_dicts, existing_runs):
        job, created = worker_service.create_job(
            session, kind="match.run", input_ref=project_id, project_id=project_id
        )
        match_job = job.job_id
        audit(session, actor="system", action="match.auto_trigger",
              basis=f"parse.confirmed project_id={project_id}",
              outcome="created" if created else "idempotent_reuse",
              object_ref=project_id)
    session.commit()
    return {"index_jobs": index_jobs, "match_job": match_job}


def create_project(session: Session, *, project_id: str, project_name: str,
                   actor: str) -> Project:
    """创建项目（幂等：已存在返回既有）。"""
    existing = session.get(Project, project_id)
    if existing:
        return existing
    project = Project(project_id=project_id, project_name=project_name)
    session.add(project)
    audit(session, actor=actor, action="project.create",
          basis=f"project_id={project_id}", outcome="created", object_ref=project_id)
    session.commit()
    return project


# ---------- 查询（只读，F020 §2.1 结果页只读） ----------

def list_projects(session: Session, *, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    """结构化推送列表（F020 §2.2.1 / GET /projects）。"""
    total = session.scalar(select(func.count()).select_from(Project))
    rows = session.scalars(
        select(Project).order_by(Project.created_at.desc()).limit(limit).offset(offset)
    ).all()
    items = [project_card(session, p) for p in rows]
    return items, total or 0


def project_card(session: Session, project: Project) -> dict[str, Any]:
    """项目卡片字段（对齐原型 data.js projects）。"""
    materials = session.scalars(
        select(Material)
        .where(Material.project_id == project.project_id)
        .order_by(Material.version.desc())
    ).all()
    tender = next((m for m in materials if m.material_type == "tender_document"), None)
    return {
        "project_id": project.project_id,
        "project_name": project.project_name,
        "state": project.admission_status or "collecting",
        "parse_status": (tender.parse_status if tender else "pending"),
        "tender_file": f"v{tender.version}" if tender else None,
        "updated": project.updated_at.date().isoformat() if project.updated_at else None,
        "materials": [
            {
                "material_id": m.material_id,
                "version": m.version,
                "material_type": m.material_type,
                "parse_status": m.parse_status,
                "content_hash": (m.content_hash[:16] if m.content_hash else None),
            }
            for m in materials
        ],
    }


def get_project_detail(session: Session, project_id: str) -> dict[str, Any]:
    """项目主卡 + 原文与解析状态表（F020 §2.2.2 / GET /projects/{id}）。"""
    project = get_project_or_404(session, project_id)
    return project_card(session, project)


def list_material_versions(session: Session, material_id: str) -> list[dict[str, Any]]:
    """版本历史（GET /materials/{id}/versions）。"""
    rows = session.scalars(
        select(Material)
        .where(Material.material_id == material_id)
        .order_by(Material.version)
    ).all()
    return [
        {
            "material_id": m.material_id,
            "version": m.version,
            "content_hash": m.content_hash,
            "parse_status": m.parse_status,
            "status": m.status,
            "imported_at": m.imported_at.isoformat() if m.imported_at else None,
            "object_uri": _material_object_uri(session, m.material_id, m.version),
        }
        for m in rows
    ]


def _material_object_uri(session: Session, material_id: str, version: int) -> str | None:
    from runtime.db.models import MaterialVersion

    row = session.scalar(
        select(MaterialVersion).where(
            MaterialVersion.material_id == material_id,
            MaterialVersion.version == version,
        )
    )
    return row.object_uri if row else None


def material_detail(session: Session, material_id: str, role: str) -> dict[str, Any]:
    """材料详情 + 版本历史（GET /materials/{id}），按角色过滤权限。"""
    material = get_material_or_404(session, material_id)
    require_scope_or_403(session, role, material)
    return {
        "material_id": material.material_id,
        "material_type": material.material_type,
        "source_type": material.source_type,
        "owner_type": material.owner_type,
        "classification": material.classification,
        "permission_scope": material.permission_scope,
        "content_hash": material.content_hash,
        "version": material.version,
        "parse_status": material.parse_status,
        "status": material.status,
        "valid_until": material.valid_until.isoformat() if material.valid_until else None,
        "data_owner": material.data_owner,
        "verified_at": material.verified_at.isoformat() if material.verified_at else None,
        "project_id": material.project_id,
        "imported_at": material.imported_at.isoformat() if material.imported_at else None,
        "versions": list_material_versions(session, material_id),
    }


# ---------- 审批/豁免（F020 §2.2.6，复用 scripts/approval 纯逻辑语义） ----------

def list_pending_approvals(session: Session, role: str) -> list[dict[str, Any]]:
    """待审批列表，仅经营负责人（F020 §5 / 原型 403 模拟页）。"""
    if not rbac.has_permission(role, "approval", "read"):
        raise ApiError("forbidden", "仅经营负责人可查看审批")
    rows = session.scalars(
        select(Approval)
        .where(Approval.state == "pending")
        .order_by(Approval.created_at)
    ).all()
    return [
        {
            "approval_id": a.approval_id,
            "project_id": a.project_id,
            "project_name": _project_name(session, a.project_id),
            "state": a.state,
            "admission_result_ref": a.admission_result_ref,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]


def _project_name(session: Session, project_id: str) -> str:
    project = session.get(Project, project_id)
    return project.project_name if project else project_id


def latest_admission(session: Session, project_id: str) -> AdmissionResult | None:
    return session.scalar(
        select(AdmissionResult)
        .where(AdmissionResult.project_id == project_id)
        .order_by(AdmissionResult.created_at.desc())
        .limit(1)
    )


def mark_admission_results_stale(session: Session, project_id: str) -> int:
    """Invalidate prior admission snapshots before a new evidence-version run."""
    rows = session.scalars(
        select(AdmissionResult).where(
            AdmissionResult.project_id == project_id,
            AdmissionResult.result_freshness == "current",
        )
    ).all()
    for row in rows:
        row.result_freshness = "stale"
    if rows:
        audit(
            session,
            actor="system",
            action="admission.mark_stale",
            basis=f"project_id={project_id}",
            outcome=f"stale_count={len(rows)}",
            object_ref=project_id,
        )
    session.commit()
    return len(rows)


def require_approval_role(role: str) -> None:
    if not rbac.has_permission(role, "approval", "approve"):
        raise ApiError("forbidden", "仅经营负责人可执行审批/豁免/审计操作")


def create_approval(session: Session, *, project_id: str, role: str, actor: str,
                    admission_result_ref: str | None = None) -> dict[str, Any]:
    """创建审批：仅最新结果满分（internal_admission_eligible=true）可创建（F020 §2.2.6）。

    方案 §3.6 门禁：匹配结果必须携带 RAG 快照（retrieval_run_id/证据快照/候选引用）
    且未过期（result_freshness=current）；缺快照或 stale 不得创建审批。
    """
    require_approval_role(role)
    result = latest_admission(session, project_id)
    if result is None or not result.internal_admission_eligible:
        audit(session, actor=actor, action="approval.create_denied",
              basis=f"project_id={project_id}", outcome="not_full_score",
              object_ref=project_id)
        session.commit()
        raise ApiError("invalid_state_transition", "内部准入未满足：只有满分才可进入投标审批")
    if result.result_freshness != "current":
        audit(session, actor=actor, action="approval.create_denied",
              basis=f"project_id={project_id}", outcome="stale_result",
              object_ref=project_id)
        session.commit()
        raise ApiError("invalid_state_transition", "准入结果已过期（stale），须按新证据版本重算后审批")
    run = session.get(MatchRun, result.run_id) if result.run_id else None
    missing = [
        key for key in ("retrieval_run_id", "evidence_snapshot_hash", "candidate_chunk_ids")
        if run is None or not getattr(run, key, None)
    ]
    if missing:
        audit(session, actor=actor, action="approval.create_denied",
              basis=f"project_id={project_id} run={run.run_id if run else None}",
              outcome=f"missing_rag_snapshot={','.join(missing)}",
              object_ref=project_id)
        session.commit()
        raise ApiError("invalid_state_transition",
                       "匹配结果缺少 RAG 快照（检索运行/证据快照/候选引用），不能创建审批（F024 §2）")
    approval = Approval(
        approval_id=f"AP-{uuid.uuid4().hex[:10]}",
        project_id=project_id,
        approver=actor,
        admission_result_ref=admission_result_ref or f"admission:{project_id}:{result.result_id}",
    )
    session.add(approval)
    # F023 §5：qualified_full_score → pending_bid_approval（进入人工审批队列的迁移点）
    project = session.get(Project, project_id)
    if project is not None:
        project.admission_status = "pending_bid_approval"
    audit(session, actor=actor, action="create_approval",
          basis=approval.admission_result_ref, outcome="pending_bid_approval",
          object_ref=project_id)
    session.commit()
    return {
        "approval_id": approval.approval_id,
        "project_id": project_id,
        "state": "pending_bid_approval",
        "admission_result_ref": approval.admission_result_ref,
    }


def _get_pending_approval(session: Session, project_id: str) -> Approval:
    approval = session.scalar(
        select(Approval)
        .where(Approval.project_id == project_id, Approval.state == "pending")
        .order_by(Approval.created_at)
        .limit(1)
    )
    if approval is None:
        raise ApiError("not_found", f"项目 {project_id} 无待审审批")
    return approval


def decide_approval(session: Session, *, project_id: str, role: str, actor: str,
                    decision: str, comment: str | None = None,
                    basis: str | None = None) -> dict[str, Any]:
    """审批/驳回：终态不可重复决策；驳回 comment 必填（F020 §2.2.6）。"""
    require_approval_role(role)
    approval = _get_pending_approval(session, project_id)
    if decision == "approved":
        approval.decision = "approved"
        approval.comment = comment
        outcome = "approved_for_bidding"
    elif decision == "rejected":
        if not comment or not comment.strip():
            raise ApiError("invalid_request", "驳回必须填写意见（comment 必填）")
        approval.decision = "rejected"
        approval.comment = comment
        outcome = "rejected_by_approver"
    else:
        raise ApiError("invalid_request", f"不支持的决策: {decision}")
    approval.decided_at = datetime.now(timezone.utc)
    approval.state = "decided"
    # F023 §5：pending_bid_approval → approved_for_bidding / rejected_by_approver
    project = session.get(Project, project_id)
    if project is not None:
        project.admission_status = outcome
    audit(session, actor=actor, action=decision,
          basis=basis or approval.admission_result_ref, outcome=outcome,
          object_ref=project_id)
    session.commit()
    return {"approval_id": approval.approval_id, "decision": approval.decision,
            "outcome": outcome, "project_id": project_id}


def add_waiver(session: Session, *, project_id: str, role: str, actor: str,
               reason: str, evidence_refs: list[str], valid_until: str,
               covered_items: list[str]) -> dict[str, Any]:
    """豁免登记：原因/证据/有效期/覆盖项必填（F020 §2.2.6）；过期自动失效回阻断。"""
    require_approval_role(role)
    try:
        _until = date.fromisoformat(valid_until)
    except ValueError:
        raise ApiError("invalid_request", "豁免有效期必须为合法日期（YYYY-MM-DD）")
    if not reason or not reason.strip():
        raise ApiError("invalid_request", "豁免原因必填")
    if not evidence_refs:
        raise ApiError("invalid_request", "豁免必须附证据（在途材料/受理回执等）")
    if not covered_items:
        raise ApiError("invalid_request", "豁免覆盖项必填")
    waiver = Waiver(
        waiver_id=f"W-{uuid.uuid4().hex[:10]}",
        project_id=project_id,
        authorizer=actor,
        reason=reason,
        evidence_refs=evidence_refs,
        valid_until=_until,
        covered_items=covered_items,
    )
    session.add(waiver)
    audit(session, actor=actor, action="add_waiver",
          basis="; ".join(evidence_refs), outcome=f"waiver:{waiver.waiver_id}",
          object_ref=project_id)
    session.commit()
    return {"waiver_id": waiver.waiver_id, "project_id": project_id, "state": "active"}


def expire_waivers(session: Session, project_id: str) -> int:
    """豁免过期自动失效：active 且 valid_until < today -> expired，审批回阻断。"""
    today = date.today()
    rows = session.scalars(
        select(Waiver).where(
            Waiver.project_id == project_id,
            Waiver.state == "active",
            Waiver.valid_until < today,
        )
    ).all()
    for waiver in rows:
        waiver.state = "expired"
        audit(session, actor="system", action="expire_waivers",
              basis=f"valid_until={waiver.valid_until.isoformat()}",
              outcome="blocked_waiver_expired", object_ref=project_id)
        # 待审审批 -> blocked_waiver_expired（F009 §6.3）；项目态同步回阻断
        approval = session.scalar(
            select(Approval).where(
                Approval.project_id == project_id, Approval.state == "pending"
            ).limit(1)
        )
        if approval is not None:
            approval.state = "blocked_waiver_expired"
        project = session.get(Project, project_id)
        if project is not None and project.admission_status == "pending_bid_approval":
            project.admission_status = "blocked_waiver_expired"
    session.commit()
    return len(rows)


def list_audit(session: Session, project_id: str, role: str) -> list[dict[str, Any]]:
    """审计时间线（F020 §2.2.6 / GET /projects/{id}/audit）。"""
    require_approval_role(role)
    rows = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.object_ref == project_id)
        .order_by(AuditEvent.at)
    ).all()
    return [
        {
            "at": e.at.isoformat() if e.at else None,
            "actor": e.actor,
            "action": e.action,
            "basis": e.basis,
            "outcome": e.outcome,
        }
        for e in rows
    ]


# ---------- 匹配/准入/队列（F020 §2.2.3/§2.2.4，只读展示） ----------

def latest_match_run(session: Session, project_id: str) -> MatchRun | None:
    return session.scalar(
        select(MatchRun)
        .where(MatchRun.project_id == project_id)
        .order_by(MatchRun.created_at.desc())
        .limit(1)
    )


def match_runs_latest(session: Session, project_id: str) -> dict[str, Any]:
    """GET /projects/{id}/match-runs/latest：只读，不创建 run（F020 §2.2.3）。"""
    run = latest_match_run(session, project_id)
    if run is None:
        return {"run_id": None}
    return {
        "run_id": run.run_id,
        "rule_set_id": run.rule_set_id,
        "rule_set_version": _rule_set_version(session, run.rule_set_id),
        "as_of": run.as_of,
        "mode": run.mode,
        "coverage": run.coverage,
        "status": run.status,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def _rule_set_version(session: Session, rule_set_id: str) -> str | None:
    from runtime.db.models import RuleSet

    row = session.get(RuleSet, rule_set_id)
    return row.version if row else None


def match_runs_compare(session: Session, project_id: str, *, base_run_id: str | None,
                       target_run_id: str | None) -> dict[str, Any]:
    """R023-4 结果版本对比：base（默认次新）vs target（默认最新）逐条 diff。

    返回：两 run 元信息（rule_set/as_of/evidence_snapshot_hash/created_at）、
    逐 requirement 的 (base_result, target_result) 对照、变化列表
    （按 changed_from→changed_to 分组）与整体摘要（satisfied 增量/缺项收敛）。
    只读不建 run；任一 run 缺失 → 404。
    """
    from runtime.db.models import MatchItem

    runs = session.scalars(
        select(MatchRun).where(MatchRun.project_id == project_id)
        .order_by(MatchRun.created_at.desc())
    ).all()
    if len(runs) < 1:
        raise ApiError("not_found", f"项目 {project_id} 无匹配运行可对比")
    target = next((r for r in runs if r.run_id == (target_run_id or runs[0].run_id)), runs[0])
    base = next((r for r in runs if r.run_id == (base_run_id or (runs[1].run_id if len(runs) > 1 else runs[0].run_id))), runs[0])
    # base 默认取 target 之前的最近一次；若同 run（仅一次运行）则 base=target（无变化）
    if base.run_id == target.run_id and len(runs) > 1:
        base = runs[1] if runs[1].run_id != target.run_id else target

    def _items_of(run_id: str) -> dict[str, dict]:
        rows = session.scalars(
            select(MatchItem).where(MatchItem.run_id == run_id)
        ).all()
        return {r.requirement_id: {"match_result": r.match_result,
                                   "score": r.score, "max_score": r.max_score,
                                   "reason": (r.match_reason or {}).get("text"),
                                   "evidence_refs": r.evidence_refs or []}
                for r in rows}

    base_items = _items_of(base.run_id)
    target_items = _items_of(target.run_id)
    all_reqs = sorted(set(base_items) | set(target_items))

    changes: list[dict] = []
    rows_out: list[dict] = []
    for rid in all_reqs:
        b = base_items.get(rid, {})
        t = target_items.get(rid, {})
        rows_out.append({
            "requirement_id": rid,
            "base": b.get("match_result"), "target": t.get("match_result"),
            "changed": b.get("match_result") != t.get("match_result"),
        })
        if b.get("match_result") != t.get("match_result"):
            changes.append({
                "requirement_id": rid,
                "from": b.get("match_result") or "（新）",
                "to": t.get("match_result") or "（移除）",
                "reason": t.get("reason"),
            })

    summary = {
        "base_satisfied": sum(1 for r in rows_out if r["base"] == "satisfied"),
        "target_satisfied": sum(1 for r in rows_out if r["target"] == "satisfied"),
        "base_unverifiable": sum(1 for r in rows_out if r["base"] == "unverifiable"),
        "target_unverifiable": sum(1 for r in rows_out if r["target"] == "unverifiable"),
        "changed_count": len(changes),
        "input_snapshot_identical": base.evidence_snapshot_hash == target.evidence_snapshot_hash
        and base.rule_set_id == target.rule_set_id and base.as_of == target.as_of,
    }
    return {
        "project_id": project_id,
        "base": {"run_id": base.run_id, "rule_set_id": base.rule_set_id,
                 "as_of": base.as_of,
                 "evidence_snapshot_hash": base.evidence_snapshot_hash,
                 "created_at": base.created_at.isoformat() if base.created_at else None},
        "target": {"run_id": target.run_id, "rule_set_id": target.rule_set_id,
                   "as_of": target.as_of,
                   "evidence_snapshot_hash": target.evidence_snapshot_hash,
                   "created_at": target.created_at.isoformat() if target.created_at else None},
        "summary": summary,
        "changes": changes,
        "matrix": rows_out,
    }


def list_requirements(session: Session, project_id: str) -> list[dict[str, Any]]:
    """项目要求列表（对齐 PROTOTYPE.requirements；无 run 时按最近 RuleSet 展示）。"""
    run = latest_match_run(session, project_id)
    rule_set_id = run.rule_set_id if run else _latest_rule_set(session, project_id)
    if rule_set_id is None:
        return []
    from runtime.db.models import Requirement

    rows = session.scalars(
        select(Requirement).where(Requirement.rule_set_id == rule_set_id)
    ).all()
    items_by_req: dict[str, dict] = {}
    if run is not None:
        for item in session.scalars(select(MatchItem).where(MatchItem.run_id == run.run_id)).all():
            items_by_req[item.requirement_id] = {
                "match": item.match_result,
                "match_reason": item.match_reason,
                "evidence_refs": item.evidence_refs or [],
                "score": item.score,
                "max_score": item.max_score,
            }
    return [
        {
            "requirement_id": r.requirement_id,
            "req_type": r.req_type,
            "category": r.category,
            "clause_ref": r.clause_ref,
            "assertion": r.assertion,
            "max_score": r.max_score,
            "score": items_by_req.get(r.requirement_id, {}).get("score"),
            "match": items_by_req.get(r.requirement_id, {}).get("match", "unverifiable"),
            "match_reason": items_by_req.get(r.requirement_id, {}).get("match_reason"),
            "evidence": items_by_req.get(r.requirement_id, {}).get("evidence_refs"),
        }
        for r in rows
    ]


def _latest_rule_set(session: Session, project_id: str) -> str | None:
    from runtime.db.models import RuleSet

    row = session.scalar(
        select(RuleSet)
        .where(RuleSet.project_id == project_id)
        .order_by(RuleSet.created_at.desc())
        .limit(1)
    )
    return row.rule_set_id if row else None


def admission_summary(session: Session, project_id: str) -> dict[str, Any]:
    """准入摘要（对齐 PROTOTYPE.admission，F020 §2.2.4）。"""
    result = latest_admission(session, project_id)
    if result is None:
        return {
            "admission": {
                "qualification": "pending", "hard_satisfied": 0, "hard_total": 0,
                "scoring": "not_full", "objective_score": 0, "objective_max": 0,
                "readiness": "not_ready", "action_done": 0, "action_total": 0,
                "eligible": False, "state": "collecting",
                "missing": [], "review": [], "manager": None, "price": None,
            }
        }
    return {
        "admission": {
            "qualification": result.qualification_result.get("status", "pending"),
            "hard_satisfied": result.qualification_result.get("satisfied", 0),
            "hard_total": result.qualification_result.get("total", 0),
            "scoring": result.scoring_result.get("status", "not_full"),
            "objective_score": result.scoring_result.get("objective_score", 0),
            "objective_max": result.scoring_result.get("objective_max", 0),
            "readiness": result.operational_readiness.get("status", "not_ready"),
            "action_done": result.operational_readiness.get("action_done", 0),
            "action_total": result.operational_readiness.get("action_total", 0),
            "eligible": result.internal_admission_eligible,
            "state": result.state,
            "missing": result.pending_items or [],
            "review": result.review_items or [],
            "manager": (result.manager_matches[0] if result.manager_matches else None),
            "price": result.scoring_result.get("price"),
        }
    }


def queues_summary(session: Session, project_id: str) -> dict[str, Any]:
    """三类处置队列（F020 §2.2.4 / GET /projects/{id}/queues）。"""
    result = latest_admission(session, project_id)
    empty = {"blocked_hard_requirement": [], "blocked_missing_data": [], "manual_review": []}
    if result is None:
        return {"queues": empty}
    return {
        "queues": {
            "blocked_hard_requirement": [
                {"project_id": project_id, "requirement_id": it.get("req"),
                 "clause": it.get("clause"), "text": it.get("text")}
                for it in (result.blocked_items or [])
            ],
            "blocked_missing_data": [
                {"project_id": project_id, "requirement_id": it.get("req"),
                 "clause": it.get("clause"), "text": it.get("text")}
                for it in (result.pending_items or [])
            ],
            "manual_review": [
                {"project_id": project_id, "requirement_id": it.get("req"),
                 "clause": it.get("clause"), "text": it.get("text")}
                for it in (result.review_items or [])
            ],
        }
    }


def matrix_view(session: Session, project_id: str) -> dict[str, Any]:
    """矩阵页合并视图：requirements + 逐条 match_items（F020 §2.2.4，只读）。"""
    return {
        "requirements": list_requirements(session, project_id),
        "run": match_runs_latest(session, project_id),
    }


# ---------- 企业资料库后台（F020 §2.2.7，数据管理员） ----------

def list_qualifications(session: Session, role: str) -> list[dict[str, Any]]:
    """资质列表（对齐 PROTOTYPE.company_data.qualifications）。"""
    if not rbac.has_permission(role, "enterprise", "read"):
        raise ApiError("forbidden", "无权访问企业资料明细")
    rows = session.scalars(
        select(Qualification).order_by(Qualification.category)
    ).all()
    return [
        {
            "name": q.category,
            "level": q.level,
            "valid_until": q.valid_until.isoformat() if q.valid_until else None,
            "status": q.status,
            "evidence": q.evidence_refs,
            "selected": q.status == "active",
        }
        for q in rows
    ]
