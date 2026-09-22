"""F026/ADR-005：匹配结果的「建议」统一为可执行的「下一步处置」。

口径（F026 §6）：只输出下列可执行下一步，不作商业投标建议。
每一项判定只依赖可追溯的匹配/准入状态，不调用模型、不读取企业私有资料。
"""
from __future__ import annotations

# ── 逐条匹配结论 → 下一步处置 ──────────────────────────────
NEXT_STEP_SUPPLY_EVIDENCE = "补齐并核验证据"
NEXT_STEP_VERIFY_HARD = "核对硬性条件原文或企业事实"
NEXT_STEP_MANUAL_REVIEW = "人工复核"
NEXT_STEP_WAIT_TASKS = "等待解析/匹配任务完成"
NEXT_STEP_SUBMIT_APPROVAL = "提交经营负责人审批"
NEXT_STEP_NO_ACTION = "无自动动作"

MATCH_RESULT_STEP: dict[str, str] = {
    "blocked_missing_data": NEXT_STEP_SUPPLY_EVIDENCE,
    "unverifiable": NEXT_STEP_SUPPLY_EVIDENCE,
    "blocked_hard_requirement": NEXT_STEP_VERIFY_HARD,
    "not_satisfied": NEXT_STEP_VERIFY_HARD,
    "manual_review": NEXT_STEP_MANUAL_REVIEW,
    "not_evaluated": NEXT_STEP_WAIT_TASKS,
    "satisfied": NEXT_STEP_NO_ACTION,
}

# ── 项目业务准入状态 → 下一步处置 ───────────────────────────
ADMISSION_STEP: dict[str, str] = {
    "collecting": NEXT_STEP_WAIT_TASKS,
    "parsed": NEXT_STEP_WAIT_TASKS,
    "matching": NEXT_STEP_WAIT_TASKS,
    "blocked_missing_data": NEXT_STEP_SUPPLY_EVIDENCE,
    "blocked_hard_requirement": NEXT_STEP_VERIFY_HARD,
    "not_qualified": NEXT_STEP_VERIFY_HARD,
    "qualified_full_score": NEXT_STEP_SUBMIT_APPROVAL,
    "pending_bid_approval": NEXT_STEP_SUBMIT_APPROVAL,
    "approved_for_bidding": NEXT_STEP_NO_ACTION,
    "rejected_by_approver": NEXT_STEP_NO_ACTION,
    "archived": NEXT_STEP_NO_ACTION,
    "overdue": NEXT_STEP_NO_ACTION,
}


def next_step_of_match(match_result: str | None) -> str:
    """逐条匹配结论 → 下一步处置；未知/缺失状态保守回落为等待/复核。"""
    if not match_result:
        return NEXT_STEP_WAIT_TASKS
    return MATCH_RESULT_STEP.get(match_result, NEXT_STEP_MANUAL_REVIEW)


def next_step_of_admission(admission_status: str | None, *, eligible: bool = False) -> str:
    """项目准入状态 → 下一步处置。满分（eligible）时优先指向经营负责人审批。"""
    if eligible:
        return NEXT_STEP_SUBMIT_APPROVAL
    if not admission_status:
        return NEXT_STEP_WAIT_TASKS
    return ADMISSION_STEP.get(admission_status, NEXT_STEP_MANUAL_REVIEW)