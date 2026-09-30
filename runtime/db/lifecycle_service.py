# ADR-004 §2.3/§2.4 / 优化方案 §11.3 §12.3：项目生命周期门禁（P0-02 身份冲突、P0-03 截止过期）
#
# 服务端门禁（不靠前端隐藏按钮）：
# - `bid_deadline` 已过 → 项目 `overdue`（机器状态被覆盖；人工决策终态 approved/rejected/archived
#   只暴露 overdue 计算标志、不改写单一状态字段），禁止新建匹配/重算任务与正式审批，旧结果只可查看；
# - `identity_conflict` → 禁止正式匹配与审批；
# - 截止时间是显式事实（identity-check 从公告/招标文件 deadline_bid 回写，或人工录入），
#   缺失=待补，不从 as_of 推断（AGENTS 规则 1）；缺失时门禁不生效，但 readiness 明示「待补」。
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from runtime.core.errors import ApiError
from runtime.db.models import Project, ProjectIdentity

logger = logging.getLogger("runtime.db.lifecycle_service")

OVERDUE = "overdue"
# 人工决策终态：截止已过只暴露计算标志，不改写状态（approved 进入递交阶段是正常生命周期）
_HUMAN_DECIDED = frozenset({"approved_for_bidding", "rejected_by_approver", "archived"})
# 投标截止按中国本地日期判定（UTC+8）；截止当日仍视为有效（截止时刻未知，不提前判过期）
CN_TZ = timezone(timedelta(hours=8))

GATE_LABELS = {
    "match": "新建匹配任务",
    "recalculate": "重算",
    "approval": "创建正式投标审批",
}


def today_cn(now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(CN_TZ).date()


def now_cn(now: datetime | None = None) -> datetime:
    """当前中国时区时点；naive 输入仅供兼容旧测试，按 UTC 解释。"""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(CN_TZ)


def deadline_at_cn(value: datetime | None) -> datetime | None:
    """截止时点统一为 CN 时区 aware。

    无时区后端（SQLite 测试库）读回 naive 值时必须按 CN 墙钟解释；
    不得直接 astimezone()——那会按服务器本地时区解释，UTC 环境（CI/容器）下漂移 8 小时。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        # 数据库历史异常值不能按服务器本地时区解释；统一按中国时区处理。
        return value.replace(tzinfo=CN_TZ)
    return value.astimezone(CN_TZ)


def deadline_text(project: Project) -> str:
    """面向审计/错误信息的截止事实；不将日期级事实伪装为精确时刻。"""
    if project.bid_deadline_at is not None:
        return deadline_at_cn(project.bid_deadline_at).isoformat()
    if project.bid_deadline is not None:
        return f"{project.bid_deadline.isoformat()}（仅日期，时刻待补）"
    return "待补"


def is_overdue(project: Project, *, now: datetime | None = None,
               today: date | None = None) -> bool:
    """精确截止时点优先；仅日期时保守地在次日才关闭。"""
    if project.bid_deadline_at is not None:
        return now_cn(now) >= deadline_at_cn(project.bid_deadline_at)
    if project.bid_deadline is None:
        return False
    return project.bid_deadline < (today or today_cn(now))


def refresh_overdue(session: Session, project: Project, *, actor: str = "system",
                    now: datetime | None = None, today: date | None = None) -> bool:
    """截止已过 → 机器状态迁移 overdue（仅一次，写审计）；返回是否过期（人工终态也如实返回）。

    不 commit：由调用方决定提交（门禁拒绝路径需先审计再提交再抛错）。
    """
    overdue = is_overdue(project, now=now, today=today)
    if not overdue:
        return False
    if project.admission_status in _HUMAN_DECIDED or project.admission_status == OVERDUE:
        return True
    from runtime.db import api_service

    previous = project.admission_status
    project.admission_status = OVERDUE
    api_service.audit(
        session, actor=actor, action="project.overdue",
        basis=f"bid_deadline={deadline_text(project)} now={now_cn(now).isoformat()}",
        outcome=f"{previous or 'None'} -> {OVERDUE}", object_ref=project.project_id,
    )
    logger.info("项目过期 project_id=%s bid_deadline=%s %s -> overdue",
                project.project_id, deadline_text(project), previous)
    return True


def gate_reason(session: Session, project: Project, *, action: str, actor: str = "system",
                now: datetime | None = None, today: date | None = None) -> tuple[str, str] | None:
    """返回 (code, message) 表示禁止执行 action；None 表示放行。

    action ∈ {match, recalculate, approval}。顺序：截止过期 → 身份冲突。

    ADR-008 / docs/12 §1.2（2026-09-28）：已声明「历史解析样本」测试上下文的项目
    （projects.test_context.historical_sample=true，服务端登记+审计+UI 条幅），
    对 **匹配/重算** 放行身份与截止门禁——测试用的是企业已完成项目的招标文件，
    文件—公告不一致与截止已过都是既定测试安排，不是串档缺陷；**审批不在此放行**
    （admission 与审批入口仍被 historical_sample_context 阻断）。生产模式下未声明
    测试上下文的真实项目，本门禁语义不变（ADR-004 红线）。
    """
    label = GATE_LABELS.get(action, action)
    test_ctx = getattr(project, "test_context", None) or {}
    if action in {"match", "recalculate"} and test_ctx.get("historical_sample"):
        return None
    if refresh_overdue(session, project, actor=actor, now=now, today=today):
        return (
            OVERDUE,
            f"项目已过投标截止 {deadline_text(project)}，禁止{label}；"
            f"历史结果只可查看，如有延期公告请更新截止时间后重新校验（ADR-004 §2.4）",
        )
    from runtime.db import identity_service

    status = identity_service.identity_status(session, project.project_id)
    if status == "identity_conflict":
        return (
            "identity_conflict",
            f"项目身份校验存在硬冲突/高风险冲突（公告与招标文件疑似串档），禁止{label}；"
            f"请纠正项目关联或材料后重新校验（ADR-004 §2.3）；"
            f"若为历史样本解析测试，请在结果页登记测试上下文放行（ADR-008 / docs/12 §1.2）",
        )
    # Iteration 1: a warning is not an implicit identity confirmation. It blocks
    # formal matching/recalculation, while early prescreen/preparation may display it.
    if action in {"match", "recalculate"} and status == "identity_warning":
        return (
            "identity_warning",
            f"项目身份校验仍有待确认项，禁止{label}；请提交人工核对依据并执行身份确认（ADR-004 §2.7）；"
            f"若为历史样本解析测试，请在结果页登记测试上下文放行（ADR-008 / docs/12 §1.2）",
        )
    return None


def deny_if_gated(session: Session, project: Project, *, action: str, actor: str,
                  audit_action: str, now: datetime | None = None,
                  today: date | None = None) -> None:
    """路由层用：命中门禁 → 写审计、提交、抛 invalid_state_transition。"""
    blocked = gate_reason(session, project, action=action, actor=actor, now=now, today=today)
    if blocked is None:
        return
    code, message = blocked
    from runtime.db import api_service

    api_service.audit(
        session, actor=actor, action=audit_action,
        basis=f"project_id={project.project_id} gate={action}", outcome=code,
        object_ref=project.project_id,
    )
    session.commit()
    raise ApiError("invalid_state_transition", message, detail={"gate": action, "reason": code})


def ensure_identity_then_deny(session: Session, project: Project, *, action: str,
                              actor: str, audit_action: str,
                              now: datetime | None = None,
                              today: date | None = None) -> None:
    """统一正式入口的身份校验与生命周期门禁顺序。

    先评估/复用身份记录，再按“截止过期优先、身份冲突其次”执行门禁。该函数供
    match、recalculate、approval 共用，worker 仍保留独立兜底，避免新增入口漏接。
    """
    from runtime.db import identity_service

    identity_service.ensure_project_identity(session, project_id=project.project_id, actor=actor)
    # ensure_project_identity 可能提交并刷新对象；再次读取避免使用过期 identity 状态。
    project = session.get(Project, project.project_id) or project
    deny_if_gated(session, project, action=action, actor=actor,
                  audit_action=audit_action, now=now, today=today)


def set_bid_deadline(session: Session, project: Project, value: date | None, *,
                     value_at: datetime | None = None, actor: str, source: str) -> None:
    """显式设置/纠正投标截止并审计。

    value_at 有值时必须是带时区精确时点，日期字段与其日期一致；仅 value 时为日期级
    事实，不虚构时刻。两者为 None 表示清除、回到待补。
    """
    from runtime.db import api_service

    if value_at is not None:
        if value_at.tzinfo is None:
            raise ApiError("invalid_request", "bid_deadline_at 必须包含时区")
        value_at = value_at.astimezone(CN_TZ)
        if value is None:
            value = value_at.date()
        elif value != value_at.date():
            raise ApiError("invalid_request", "bid_deadline 与 bid_deadline_at 的日期必须一致")
    previous = deadline_text(project)
    project.bid_deadline = value
    project.bid_deadline_at = value_at
    api_service.audit(
        session, actor=actor, action="project.bid_deadline_set",
        basis=f"source={source}", outcome=f"{previous} -> {deadline_text(project)}",
        object_ref=project.project_id,
    )
    # 延期后若此前被机器标记 overdue，恢复到 matching 等待重新核验（不恢复人工终态）。
    # ``set_bid_deadline`` 是事实更正入口，不能用服务器当前墙钟否定一条明确的
    # 延期事实；后续 readiness / 正式入口会按实时门禁再次将真正到期项目置为 overdue。
    if value is not None and project.admission_status == OVERDUE:
        project.admission_status = "matching"
        api_service.audit(
            session, actor=actor, action="project.overdue_recovered",
            basis=f"bid_deadline={deadline_text(project)}", outcome="overdue -> matching",
            object_ref=project.project_id,
        )


def _workflow_readiness(session: Session, project_id: str) -> dict[str, Any]:
    """Iteration 1 workbench summary. It is informational and never a gate bypass."""
    try:
        from sqlalchemy import func, select
        from runtime.db.models import PreparationRecord, RemediationTask
        preparation = session.scalar(
            select(PreparationRecord).where(PreparationRecord.project_id == project_id)
            .order_by(PreparationRecord.created_at.desc()).limit(1)
        )
        open_count = session.scalar(
            select(func.count()).select_from(RemediationTask).where(
                RemediationTask.project_id == project_id,
                RemediationTask.state.in_(("open", "in_progress", "evidence_submitted", "resolved_pending_recalculation", "overdue")),
            )
        ) or 0
        return {
            "preparation_state": preparation.state if preparation else None,
            "open_task_count": int(open_count),
            "note": "投标准备立项仅代表投入准备工作，不代表正式同意投标或递交。",
        }
    except Exception as exc:  # compatibility before migration is applied
        logger.warning("workflow readiness summary unavailable project_id=%s: %s", project_id, type(exc).__name__)
        return {"preparation_state": None, "open_task_count": None, "note": "工作流数据待迁移后可用"}


def project_readiness(session: Session, project_id: str, *, now: datetime | None = None,
                      today: date | None = None) -> dict[str, Any]:
    """项目就绪摘要（优化方案 §10.2/§11.2 `GET /projects/{id}/readiness`）：只陈述事实与阻断原因。"""
    from runtime.db import api_service, identity_service

    project = api_service.get_project_or_404(session, project_id)
    current_now = now_cn(now)
    today = today or current_now.date()
    overdue = refresh_overdue(session, project, now=current_now, today=today)
    if overdue and session.dirty:
        session.commit()
    identity_row = session.get(ProjectIdentity, project_id)
    identity = identity_service.identity_payload(identity_row)
    admission = api_service.latest_admission(session, project_id)
    run = api_service.latest_match_run(session, project_id)

    blocking: list[dict[str, str]] = []
    if project.bid_deadline is None:
        blocking.append({"code": "bid_deadline_missing",
                         "text": "投标截止时间待补：请执行身份校验或人工录入后，截止门禁方可生效"})
    if overdue:
        blocking.append({"code": OVERDUE,
                         "text": f"已过投标截止 {deadline_text(project)}：禁止新建匹配/重算/审批，历史结果只可查看"})
    if identity["status"] is None:
        blocking.append({"code": "identity_unchecked", "text": "项目身份尚未校验（未校验≠已确认）"})
    elif identity["status"] == "identity_conflict":
        blocking.append({"code": "identity_conflict", "text": "项目身份存在冲突：禁止正式匹配与审批"})
    elif identity["status"] == "identity_warning":
        blocking.append({"code": "identity_warning", "text": "项目身份存在待核对差异：请人工核对后确认"})
    if admission is not None and admission.result_freshness != "current":
        blocking.append({"code": "stale", "text": "最新准入结果已失效（stale），须按新版本重算"})
    if admission is not None and not admission.internal_admission_eligible:
        blocking.append({"code": admission.internal_admission_result.get("status", "not_eligible"),
                         "text": admission.internal_admission_result.get("decision") or "内部准入未满足"})

    days_left = (project.bid_deadline - today).days if project.bid_deadline else None
    seconds_left = (deadline_at_cn(project.bid_deadline_at) - current_now).total_seconds() if project.bid_deadline_at else None
    precision = "datetime" if project.bid_deadline_at else ("date" if project.bid_deadline else "missing")
    return {
        "project_id": project.project_id,
        "project_name": project.project_name,
        "stage": project.admission_status,
        "bid_deadline": project.bid_deadline.isoformat() if project.bid_deadline else None,
        "bid_deadline_at": deadline_at_cn(project.bid_deadline_at).isoformat() if project.bid_deadline_at else None,
        "deadline_precision": precision,
        "deadline_accuracy_note": (
            "精确截止时点，系统在该时点及之后关闭正式操作" if precision == "datetime" else
            "仅有截止日期，截止当日不提前关闭；请补充原文时分秒" if precision == "date" else
            "截止日期与时点均待补，门禁尚不能按截止事实生效"
        ),
        "days_to_deadline": days_left,
        "seconds_to_deadline": seconds_left,
        "overdue": overdue,
        "identity": identity,
        "latest_match_run": {
            "run_id": run.run_id if run else None,
            "as_of": run.as_of if run else None,
            "created_at": run.created_at.isoformat() if run and run.created_at else None,
        },
        "latest_admission": {
            "result_id": admission.result_id if admission else None,
            "status": admission.internal_admission_result.get("status") if admission else None,
            "eligible": bool(admission.internal_admission_eligible) if admission else False,
            "freshness": admission.result_freshness if admission else None,
        },
        "gates": {
            "can_match": gate_reason(session, project, action="match", now=current_now, today=today) is None,
            "can_recalculate": gate_reason(session, project, action="recalculate", now=current_now, today=today) is None,
            "can_create_approval": (
                gate_reason(session, project, action="approval", now=current_now, today=today) is None
                and admission is not None and bool(admission.internal_admission_eligible)
                and admission.result_freshness == "current"
            ),
        },
        "blocking_reasons": blocking,
        "workflow": _workflow_readiness(session, project_id),
        "caveat": "本摘要只陈述事实与阻断原因，不构成投标建议或商业承诺（ADR-001/ADR-004）",
    }
