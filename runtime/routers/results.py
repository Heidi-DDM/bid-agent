# ADR-008 / docs/12 §4.2：结果域 API（目标态；契约见 F020 v1.23）
#
# 读：result-overview / qualification-matrix / scoring-analysis / operation-plan /
#     parse-exceptions / candidate-plans / remediation-tasks —— 全部接受可选 run_id，
#     默认仅返回最新 current 运行并回显同一 run_context（docs/12 §3.5）。
# 写：parse-exceptions/{id}/decisions（结构化处置，代替浏览器 prompt）、
#     candidate-plans/select（投标负责人）、operation-tasks/{id}（任务责任人）、
#     test-context（历史解析样本登记，审批/递交二次阻断）。
# 写接口均带角色校验、审计事件与 request_id；引用非当前版本 → 409 stale_input。
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from runtime.db import results_service
from runtime.routers.deps import get_actor, get_db, get_request_id, get_role

router = APIRouter(prefix="/api/v1", tags=["results"])


def _readable(role: str) -> None:
    # 项目可见角色（ADR-005 两级角色）：投标专员 + 经营负责人
    if role not in {"bid_specialist", "business_head"}:
        from runtime.core.errors import ApiError
        raise ApiError("forbidden", "当前角色无权查看项目结果")


# ---------------------------------------------------------------------------
# 读接口
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/result-overview")
def result_overview(
    project_id: str,
    run_id: str | None = Query(default=None, description="可选：读取指定运行；缺省=最新 current 运行"),
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """结果首屏的单一读取源：run_context、资格摘要、解析质量摘要、优先事项（≤3 项）。"""
    _readable(role)
    return {"request_id": request_id,
            **results_service.result_overview(session, project_id=project_id, run_id=run_id,
                                              role=role, actor=actor)}


@router.get("/projects/{project_id}/qualification-matrix")
def qualification_matrix(
    project_id: str,
    run_id: str | None = None,
    lot_id: str | None = None,
    state: str | None = Query(default=None, description="筛选：not_satisfied/blocked_missing_data/manual_review/satisfied"),
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """资格与资源矩阵（只返回资格项；含原因码/事实值/证据状态/人员方案回链）。"""
    _readable(role)
    return {"request_id": request_id,
            **results_service.qualification_matrix(session, project_id=project_id, run_id=run_id,
                                                    lot_id=lot_id, state=state, role=role, actor=actor)}


@router.get("/projects/{project_id}/scoring-analysis")
def scoring_analysis(
    project_id: str,
    run_id: str | None = None,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """评分分析（按五类评分分类投影；无可算项不输出 0/0，不生成报价）。"""
    _readable(role)
    return {"request_id": request_id,
            **results_service.scoring_analysis(session, project_id=project_id, run_id=run_id,
                                               role=role, actor=actor)}


@router.get("/projects/{project_id}/operation-plan")
def operation_plan(
    project_id: str,
    run_id: str | None = None,
    stage: str | None = Query(default=None, description="筛选阶段：preparation/approval_ready/submission_ready/submitted/opened"),
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """投标执行计划：阶段时间线、责任人、截止、回执与阶段门禁（独立于缺证队列）。"""
    _readable(role)
    return {"request_id": request_id,
            **results_service.operation_plan(session, project_id=project_id, run_id=run_id,
                                             stage=stage, role=role, actor=actor)}


@router.get("/projects/{project_id}/parse-exceptions")
def parse_exceptions(
    project_id: str,
    parse_version: str | None = Query(default=None, description="可选：读取指定解析例外快照版本"),
    status: str | None = Query(default=None, description="open/deep_review/resolved"),
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """解析质量的唯一队列源（按风险 rank 1-4 排序；含处置审计）。"""
    _readable(role)
    return {"request_id": request_id,
            **results_service.parse_exceptions_view(session, project_id=project_id,
                                                    parse_version=parse_version, status=status,
                                                    role=role, actor=actor)}


@router.get("/projects/{project_id}/candidate-plans")
def candidate_plans(
    project_id: str,
    run_id: str | None = None,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """候选班子与人员—条款回链（同一 person_id 约束，禁止跨方案拼接）。"""
    _readable(role)
    return {"request_id": request_id,
            **results_service.candidate_plans_view(session, project_id=project_id, run_id=run_id,
                                                   role=role, actor=actor)}


@router.get("/projects/{project_id}/remediation-tasks")
def remediation_tasks(
    project_id: str,
    run_id: str | None = None,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """按聚合缺口返回补证任务（同一缺口一条任务、覆盖条款完整）+ 资源处置建议。"""
    _readable(role)
    return {"request_id": request_id,
            **results_service.remediation_tasks_view(session, project_id=project_id, run_id=run_id,
                                                     role=role, actor=actor)}


# ---------------------------------------------------------------------------
# 写接口
# ---------------------------------------------------------------------------

class ExceptionDecisionBody(BaseModel):
    decision: str = Field(..., description="approved（通过）/ not_applicable（本文件无此条款，仅未定位候选）/ rejected（无法确认+原因，已定位候选不建规则时用）/ deep_review（转深度复核）")
    reason: str = Field(..., min_length=1, description="必填处置理由（审计留痕）")
    search_scope: str | None = Field(default=None, description="检索词或检索范围（not_applicable 必填）")
    page_refs: list[int] | None = Field(default=None, description="可选：定位页码")
    snapshot_version: str | None = Field(default=None, description="处置对象快照版本（不一致 → 409 stale_input）")
    note: str | None = Field(default=None, description="可选备注")


@router.post("/parse-exceptions/{parse_exception_id}/decisions")
def decide_parse_exception(
    parse_exception_id: str,
    body: ExceptionDecisionBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """页面内结构化处置表单的后端（代替浏览器 prompt()；docs/12 §2.2-5）。

    取消/Escape 不产生请求；提交返回 request_id 与处置回执；处置完毕沿用
    ADR-007 自动重确认链（新规则快照 + 旧匹配结果 stale + 重算任务）。
    """
    return {"request_id": request_id,
            "exception": results_service.decide_parse_exception(
                session, parse_exception_id=parse_exception_id,
                decision=body.decision, reason=body.reason,
                search_scope=body.search_scope, page_refs=body.page_refs,
                snapshot_version=body.snapshot_version, note=body.note,
                actor=actor, role=role)}


class CandidatePlanSelectBody(BaseModel):
    candidate_plan_id: str
    run_id: str
    reason: str = Field(..., min_length=1, description="选定理由（审计留痕）")


@router.post("/projects/{project_id}/candidate-plans/select")
def select_candidate_plan(
    project_id: str,
    body: CandidatePlanSelectBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """投标负责人人工选定候选班子方案（生成新快照输入并重算，旧结果 stale）。"""
    return {"request_id": request_id,
            **results_service.select_candidate_plan(
                session, project_id=project_id, candidate_plan_id=body.candidate_plan_id,
                run_id=body.run_id, reason=body.reason, actor=actor, role=role)}


class OperationTaskBody(BaseModel):
    status: str = Field(..., description="not_started/ready/completed/overdue/not_applicable")
    receipt_evidence_refs: list[str] | None = Field(default=None, description="回执证据引用（completed 必填）")
    owner: str | None = None
    version: int | None = Field(default=None, description="乐观锁版本（不一致 → 409 stale_input）")


@router.patch("/operation-tasks/{operation_task_id}")
@router.post("/operation-tasks/{operation_task_id}")
def update_operation_task(
    operation_task_id: str,
    body: OperationTaskBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """任务责任人登记执行动作状态/回执（仅更新动作计划，按阶段验证；不改资格/评分）。"""
    return {"request_id": request_id,
            "task": results_service.update_operation_task(
                session, operation_task_id=operation_task_id, status=body.status,
                receipt_evidence_refs=body.receipt_evidence_refs, version=body.version,
                owner=body.owner, actor=actor, role=role)}


class TestContextBody(BaseModel):
    historical_sample: bool = Field(..., description="登记/解除「历史解析样本／非当前项目文件」测试上下文")
    reason: str = Field(..., min_length=1, description="必填理由（审计留痕）")


@router.get("/projects/{project_id}/test-context")
def test_context(
    project_id: str,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """读取项目测试上下文（历史样本标识来自服务端，不可由前端参数伪造）。"""
    _readable(role)
    return {"request_id": request_id,
            **results_service.test_context_view(session, project_id=project_id, role=role, actor=actor)}


@router.post("/projects/{project_id}/test-context")
def set_test_context(
    project_id: str,
    body: TestContextBody,
    request_id: str = Depends(get_request_id),
    role: str = Depends(get_role),
    actor: str = Depends(get_actor),
    session: Session = Depends(get_db),
) -> dict:
    """登记/解除历史解析样本测试上下文（唯一服务端入口；审批/递交接口二次拒绝）。"""
    return {"request_id": request_id,
            **results_service.set_test_context(
                session, project_id=project_id, historical_sample=body.historical_sample,
                reason=body.reason, actor=actor, role=role)}
