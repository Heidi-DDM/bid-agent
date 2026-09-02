# R024：admission_results 生成执行器服务层（F008 §4.6 / F023 §2 第 6 步 / F024 §2）
#
# 职责：一次 gate 匹配运行（MatchRun + MatchItem 已落库）→ 聚合生成 AdmissionResult
# 不可变快照（四类结论/分数/阻断待补复核队列/经理候选/解释），并把项目准入状态
# 迁移到推导结论态。只接受 match 引擎的 gate 输出，不自动投标、不推断缺失字段。
#
# 设计约束（文档单一事实源）：
# - F008 §4.6 四类结论为结构化对象；blocked/pending/review 每项含 req/clause/text；
# - ADR-001 §2.2/§2.3：缺失默认阻断（blocked_missing_data）、响应性硬失败
#   （blocked_hard_requirement）、资格性失败（not_qualified）、全满足
#   （qualified_full_score）；manual_review 不谎报为缺失或失败；
# - 责任人/截止时间等依赖企业资料字段责任人（R022），未就绪前不推断；
# - 本模块不做 commit，由 worker 执行器统一提交（避免嵌套事务）。
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from runtime.db.models import AdmissionResult, MatchRun, Project

logger = logging.getLogger("runtime.db.admission_service")

# 人工决策终态：机器重算不得回退这些状态（F023 §4 最新结果驱动迁移仅限审批前）
_HUMAN_TERMINAL_STATES = {
    "approved_for_bidding", "rejected_by_approver", "archived",
    "pending_bid_approval", "blocked_waiver_expired",
}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _queue_item(entry: dict[str, Any], req: dict[str, Any] | None) -> dict[str, Any]:
    """matrix 条目 → 阻断/待补/复核队列项（queues_summary 兼容 {req,clause,text}）。

    仅携带既有事实字段；责任人/截止时间无来源（R022）不推断（AGENTS 禁止编造）。
    """
    return {
        "req": entry.get("requirement_id"),
        "clause": entry.get("clause_ref"),
        "text": (req or {}).get("assertion") or entry.get("match_reason"),
        "match_result": entry.get("match_result"),
        "req_type": entry.get("req_type"),
        "failure_effect": (req or {}).get("failure_effect"),
    }


def _manager_candidates(evidence: dict[str, list[dict]], as_of: str) -> list[dict]:
    """F007 经理候选快照：as_of 时点可用（active/已核验/可调派/证书未过期）。

    只陈述快照事实，不宣称某经理满足某条规则——规则判定结果由矩阵
    project_manager 条目承载（NQ-H-003 等），主推荐仅按证书等级排序标记。
    """
    from runtime.core.matching import _snapshot_eligible

    candidates: list[dict] = []
    for m in evidence.get("manager_profile") or []:
        if not _snapshot_eligible(m, as_of):
            continue
        if m.get("status") != "active" or m.get("availability") != "available":
            continue
        candidates.append({
            "manager_id": m.get("manager_id"),
            "display_name": m.get("display_name"),
            "cert_level": m.get("cert_level"),
            "cert_valid_until": m.get("cert_valid_until"),
            "specialty": m.get("specialty") or [],
            "active_project_count": len(m.get("active_projects") or []),
            "verified_at": m.get("verified_at"),
            "evidence_refs": m.get("evidence_refs") or [],
        })
    candidates.sort(key=lambda c: (c.get("cert_level") or "", c.get("manager_id") or ""))
    for idx, c in enumerate(candidates):
        c["primary"] = idx == 0
    return candidates


def _derive_conclusion(engine_result: dict[str, Any],
                       matrix_by_id: dict[str, dict],
                       req_by_id: dict[str, dict],
                       ) -> tuple[dict[str, Any], str]:
    """四类结论之一 internal_admission_result + 项目准入状态（ADR-001 §2.2/§2.3）。

    优先级：硬失败（资格/响应性）→ 缺失阻断 → 人工复核（不谎报）→ 全满足。
    """
    matrix = engine_result.get("matrix", [])
    complete = bool(engine_result.get("coverage", {}).get("complete"))
    blocked = engine_result.get("blocked", [])
    pending = engine_result.get("pending", [])
    review = engine_result.get("review", [])

    # 1) 硬性失败：证据充分但明确不满足 → not_qualified / blocked_hard_requirement
    hard_failed = [e for e in blocked if e.get("req_type") == "hard_requirement"]
    if hard_failed:
        first = hard_failed[0]
        effect = (req_by_id.get(first.get("requirement_id")) or {}).get("failure_effect")
        status = "not_qualified" if effect == "not_qualified" else "blocked_hard_requirement"
        return {
            "status": status,
            "eligible": False,
            "failed_requirements": [e.get("requirement_id") for e in hard_failed],
            "decision": f"硬性要求明确不满足（{status}），一票否决，不得进入审批（F008 §6.2）",
        }, status

    # 2) 关键证据缺失/无法核验 → 默认阻断，不推断满足
    if pending:
        return {
            "status": "blocked_missing_data",
            "eligible": False,
            "pending_requirements": [e.get("requirement_id") for e in pending],
            "decision": "存在待补/无法核验项，按缺失默认阻断（ADR-001 §2.4），不得进入审批",
        }, "blocked_missing_data"

    # 3) 需人工复核（解析漏项/规则无法执行/主观评审未过）→ 不谎报为缺失或失败
    if review or not complete:
        reason = "存在需人工复核项" if review else "规则未全量执行（coverage 不完整）"
        return {
            "status": "manual_review",
            "eligible": False,
            "review_requirements": [e.get("requirement_id") for e in review],
            "decision": f"{reason}，需人工复核/补录后重算，不参与自动准入（F008 §6.2）",
        }, "matching"

    # 4) 全部内部政策条件满足 → qualified_full_score（仅送人工审批）
    return {
        "status": "qualified_full_score",
        "eligible": True,
        "decision": "内部准入政策全满足（无阻断/待补/复核且覆盖完整），"
                    "仅进入人工审批队列，不代表评标得分，不自动投标（ADR-001 §2.2）",
    }, "qualified_full_score"


def generate_admission_result(
    session: Session,
    *,
    run: MatchRun,
    engine_result: dict[str, Any],
    requirements: list[dict[str, Any]],
    evidence: dict[str, list[dict]],
) -> AdmissionResult:
    """由一次 gate 匹配运行聚合生成 AdmissionResult（F023 §2 第 6 步）。

    run 必须先 flush（run_id 可用）；本函数不 commit，由调用方统一提交。
    仅接受 mode=gate（诊断运行不驱动准入状态，F023 §2 第 4 步）。
    """
    if run.mode != "gate":
        raise ValueError(
            f"admission 生成仅限 gate 运行（当前 mode={run.mode!r}），诊断不驱动准入状态"
        )

    matrix = engine_result.get("matrix", [])
    matrix_by_id = {
        e["requirement_id"]: e for e in matrix if e.get("requirement_id")
    }
    req_by_id = {
        r["requirement_id"]: r for r in requirements if r.get("requirement_id")
    }

    hard = [e for e in matrix if e.get("req_type") == "hard_requirement"]
    scored = [e for e in matrix if e.get("req_type") == "scored_requirement"]
    actions = [e for e in matrix if e.get("req_type") == "action_requirement"]

    # ---- 四类结论（结构化对象，F008 §4.6：不用单一布尔替代） ----
    qualification_result = {
        "status": engine_result.get("qualification_result", "pending"),
        "satisfied": sum(1 for e in hard if e.get("match_result") == "satisfied"),
        "total": len(hard),
        "items": [_queue_item(e, req_by_id.get(e.get("requirement_id")))
                  for e in hard],
    }

    def _scored_score(e: dict) -> float | None:
        s = e.get("score")
        return float(s) if s is not None else None

    scored_rows: list[dict[str, Any]] = []
    for e in scored:
        score = _scored_score(e)
        max_score = (req_by_id.get(e.get("requirement_id")) or {}).get("max_score")
        scored_rows.append({
            "requirement_id": e.get("requirement_id"),
            "clause_ref": e.get("clause_ref"),
            "match_result": e.get("match_result"),
            "score": score,
            "max_score": max_score,
            "reason": e.get("match_reason"),
        })
    scored_scores = [r["score"] for r in scored_rows if r["score"] is not None]
    scored_maxes = [
        float(r["max_score"]) for r in scored_rows if r["max_score"] is not None
    ]
    full_score_ready = all(
        r["match_result"] == "satisfied"
        and r["score"] is not None
        and r["score"] == r["max_score"]
        for r in scored_rows
    ) if scored_rows else True
    scoring_result = {
        "status": engine_result.get("scoring_result", "not_full"),
        "objective_score": round(sum(scored_scores), 4) if scored_scores else 0,
        "objective_max": round(sum(scored_maxes), 4) if scored_maxes else 0,
        "internal_full_score_ready": bool(full_score_ready),
        "items": scored_rows,
    }

    readiness = engine_result.get("operational_readiness", "not_ready")
    operational_readiness = {
        "status": readiness,
        "approval_ready": readiness == "ready",
        "action_done": sum(1 for e in actions if e.get("match_result") == "satisfied"),
        "action_total": len(actions),
        "items": [_queue_item(e, req_by_id.get(e.get("requirement_id")))
                  for e in actions],
    }

    internal_admission_result, project_state = _derive_conclusion(
        engine_result, matrix_by_id, req_by_id,
    )
    internal_admission_eligible = engine_result.get("internal_admission_eligible", False)

    # ---- 分数与缺口（F008 §4.6：不含尚不可计算或主观实际评标分） ----
    total_score = round(sum(scored_scores), 4) if scored_scores else None
    max_total_score = round(sum(scored_maxes), 4) if scored_maxes else None
    score_gap_items: list[dict[str, Any]] = []
    for r in scored_rows:
        score, max_score = r["score"], r["max_score"]
        gapped = r["match_result"] != "satisfied" or score is None
        if not gapped and max_score is not None:
            gapped = score is None or score < float(max_score)
        if not gapped:
            continue
        gap = None
        if score is not None and max_score is not None:
            gap = round(float(max_score) - float(score), 4)
        score_gap_items.append({
            "requirement_id": r["requirement_id"],
            "clause_ref": r["clause_ref"],
            "assertion": (req_by_id.get(r["requirement_id"]) or {}).get("assertion"),
            "score": score,
            "max_score": max_score,
            "gap": gap,
            "reason": r["reason"],
        })

    # ---- 三类处置队列（每项含 req/clause/text + 事实字段） ----
    blocked_items = [
        _queue_item(e, req_by_id.get(e.get("requirement_id")))
        for e in engine_result.get("blocked", [])
    ]
    pending_items = [
        _queue_item(e, req_by_id.get(e.get("requirement_id")))
        for e in engine_result.get("pending", [])
    ]
    review_items = [
        _queue_item(e, req_by_id.get(e.get("requirement_id")))
        for e in engine_result.get("review", [])
    ]

    # ---- 经理候选（F007 快照 + 项目级经理规则满足情况） ----
    manager_rules = [
        e for e in matrix
        if (req_by_id.get(e.get("requirement_id")) or {}).get("rule", {}).get("type")
        == "project_manager"
    ]
    manager_matches = _manager_candidates(evidence, run.as_of)
    if manager_rules:
        manager_matches.append({
            "_summary": {
                "project_manager_rule_satisfied": [
                    e.get("requirement_id") for e in manager_rules
                    if e.get("match_result") == "satisfied"
                ],
                "project_manager_rule_pending": [
                    e.get("requirement_id") for e in manager_rules
                    if e.get("match_result") == "unverifiable"
                ],
                "project_manager_rule_failed": [
                    e.get("requirement_id") for e in manager_rules
                    if e.get("match_result") == "not_satisfied"
                ],
            }
        })

    # ---- 解释链（F008 §4.6 / F024 §2：as_of/版本/检索/证据快照/决策依据） ----
    explanation = [
        {"step": "as_of", "text": f"判定时点 {run.as_of}（不得默认当前时间，F008 §4.1）"},
        {"step": "rule_set", "text": f"规则集 {run.rule_set_id}（版本化，历史项目保留当时规则）"},
        {"step": "coverage", "text": f"mode={run.mode} 声明 "
                                     f"{engine_result.get('coverage', {}).get('declared')} 执行 "
                                     f"{engine_result.get('coverage', {}).get('executed')} 完整 "
                                     f"{engine_result.get('coverage', {}).get('complete')}"},
        {"step": "rag_snapshot",
         "text": f"retrieval_run_id={run.retrieval_run_id or '—'} index_version="
                 f"{run.index_version or '—'} candidate_chunks="
                 f"{len(run.candidate_chunk_ids or [])} evidence_snapshot="
                 f"{(run.evidence_snapshot_hash or '—')[:12]}（向量分数不进入准入公式，F025）"},
        {"step": "decision", "text": internal_admission_result.get("decision")},
        {"step": "caveat",
         "text": "内部准入满分是企业送人工审批的保守政策，非评标委员会得分；"
                 "系统不自动投标、不报价、不签章（ADR-001）"},
    ]

    result = AdmissionResult(
        result_id=f"AR-{uuid.uuid4().hex[:12]}",
        run_id=run.run_id,
        project_id=run.project_id,
        lot_id=run.lot_id,
        rule_set_id=run.rule_set_id,
        qualification_result=qualification_result,
        scoring_result=scoring_result,
        operational_readiness=operational_readiness,
        internal_admission_result=internal_admission_result,
        internal_admission_eligible=internal_admission_eligible,
        total_score=total_score,
        max_total_score=max_total_score,
        score_gap_items=score_gap_items,
        blocked_items=blocked_items,
        pending_items=pending_items,
        review_items=review_items,
        manager_matches=manager_matches,
        explanation=explanation,
        result_freshness="current",
        state="final",
    )

    # ---- 旧快照失效：同一项目此前 current 结果置 stale（F023 §4） ----
    from sqlalchemy import select

    from runtime.db.models import AdmissionResult as _AR

    stale_rows = session.scalars(
        select(_AR).where(
            _AR.project_id == run.project_id,
            _AR.result_freshness == "current",
            _AR.result_id != result.result_id,
        )
    ).all()
    for row in stale_rows:
        row.result_freshness = "stale"

    # ---- 项目准入状态迁移（ADR-001 §2.2；人工终态不被机器重算回退） ----
    project = session.get(Project, run.project_id)
    if project is not None and project.admission_status not in _HUMAN_TERMINAL_STATES:
        if project.admission_status != project_state:
            logger.info(
                "准入状态迁移 project_id=%s %s -> %s (run=%s)",
                run.project_id, project.admission_status or "None", project_state, run.run_id,
            )
        project.admission_status = project_state

    # ---- 审计（仅追加） ----
    from runtime.db import api_service

    if stale_rows:
        api_service.audit(
            session, actor="system", action="admission.mark_stale",
            basis=f"project_id={run.project_id} new_run={run.run_id}",
            outcome=f"stale_count={len(stale_rows)}", object_ref=run.project_id,
        )
    api_service.audit(
        session, actor="system", action="admission.generated",
        basis=f"run={run.run_id} rule_set={run.rule_set_id} as_of={run.as_of}",
        outcome=f"eligible={internal_admission_eligible} project_state={project_state} "
                f"freshness={result.result_freshness}",
        object_ref=run.project_id,
    )

    session.add(result)
    session.flush()
    return result
