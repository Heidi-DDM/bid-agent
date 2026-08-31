# F018 §4.2：analysis_jobs 任务状态机
# 纯逻辑模块（不依赖数据库/SQLAlchemy），保证确定性可测：
#   pending -> running -> completed
#                |    \-> retryable -> pending（attempts < max_attempts）
#                |                \-> failed
#                \-> cancelled
#
# 规则：
# - 幂等键（input_ref + kind + project_id）唯一，重复提交返回既有任务（F019 §3 analysis_jobs）；
# - running 超过 JOB_RUNNING_TIMEOUT_SECONDS 无心跳 -> retryable，不推进业务状态（F018 §4.4）；
# - 任何异常都不得直接把项目推进到下一业务状态；
# - cancelled / completed / failed 为终态，不可再迁移。
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
RETRYABLE = "retryable"
FAILED = "failed"
CANCELLED = "cancelled"

ACTIVE_STATES = {PENDING, RUNNING}
TERMINAL_STATES = {COMPLETED, FAILED, CANCELLED}
# 允许恢复的中间态
RECOVERABLE_STATES = {RUNNING, RETRYABLE}

MAX_ATTEMPTS_DEFAULT = 3
HEARTBEAT_STALE_SECONDS_DEFAULT = 600


@dataclass
class AnalysisJob:
    job_id: str
    kind: str
    status: str = PENDING
    attempts: int = 0
    max_attempts: int = MAX_ATTEMPTS_DEFAULT
    input_ref: Optional[str] = None
    project_id: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    heartbeat_at: Optional[str] = None  # ISO 时间戳
    idempotency_key: Optional[str] = field(default=None, compare=False)


def build_idempotency_key(kind: str, input_ref: str, project_id: str | None = None) -> str:
    """幂等键：kind + input_ref + project_id，保证同一输入不重复建任务。"""
    return f"{kind}:{input_ref}:{project_id or ''}"


def validate_transition(current: str, target: str) -> None:
    allowed = TRANSITIONS.get(current, set())
    if target not in allowed:
        raise ValueError(f"非法状态迁移: {current} -> {target}")


# 状态迁移表：目标状态集合
TRANSITIONS: dict[str, set[str]] = {
    PENDING: {RUNNING, CANCELLED},
    RUNNING: {COMPLETED, RETRYABLE, FAILED, CANCELLED},
    RETRYABLE: {PENDING, CANCELLED},
    COMPLETED: set(),
    FAILED: set(),
    CANCELLED: set(),
}


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, set())


def claim_next_jobs(
    jobs: list[AnalysisJob],
    *,
    now_iso: str,
    heartbeat_stale_seconds: int = HEARTBEAT_STALE_SECONDS_DEFAULT,
    max_claims: int = 1,
    runner_id: str = "worker-1",
) -> list[AnalysisJob]:
    """worker 领取任务：优先 pending；running 心跳超时（stale）可回收为 retryable 后再领取。

    返回被领取（标记为 running）的任务；失败任务不自动重跑。
    """
    import datetime as _dt

    claimed: list[AnalysisJob] = []
    for job in jobs:
        if len(claimed) >= max_claims:
            break
        if job.status == PENDING:
            job.status = RUNNING
            job.attempts += 1
            job.heartbeat_at = now_iso
            job.runner = runner_id  # type: ignore[attr-defined]
            claimed.append(job)
            continue
        if job.status == RUNNING:
            # 心跳超时 -> retryable（F018 §4.4：进程异常时任务保持 running 超时后恢复为 retryable）
            if _is_stale(job.heartbeat_at, now_iso, heartbeat_stale_seconds):
                job.status = RETRYABLE
                job.error_code = "heartbeat_timeout"
                # 不直接重跑；由 retryable -> pending 再领取
        if job.status == RETRYABLE:
            if job.attempts < job.max_attempts:
                job.status = PENDING
    return claimed


def _is_stale(heartbeat_at: str | None, now_iso: str, stale_seconds: int) -> bool:
    if not heartbeat_at:
        return False
    from datetime import datetime

    try:
        hb = datetime.fromisoformat(heartbeat_at)
        now = datetime.fromisoformat(now_iso)
        return (now - hb).total_seconds() > stale_seconds
    except ValueError:
        return False


def heartbeat(job: AnalysisJob, now_iso: str) -> None:
    if job.status != RUNNING:
        raise ValueError(f"仅 running 任务可心跳，当前 {job.status}")
    job.heartbeat_at = now_iso


def complete(job: AnalysisJob) -> None:
    validate_transition(job.status, COMPLETED)
    job.status = COMPLETED


def fail(job: AnalysisJob, error_code: str, error_message: str | None = None) -> None:
    if job.status in TERMINAL_STATES:
        raise ValueError(f"终态任务不可失败，当前 {job.status}")
    if job.attempts >= job.max_attempts:
        job.status = FAILED
    else:
        job.status = RETRYABLE
    job.error_code = error_code
    if error_message:
        job.error_message = error_message


def cancel(job: AnalysisJob) -> None:
    validate_transition(job.status, CANCELLED)
    job.status = CANCELLED


def retry(job: AnalysisJob) -> None:
    """retryable -> pending（人工或策略触发重试；不自动重跑失败任务）。"""
    validate_transition(job.status, PENDING)
    job.status = PENDING
    job.error_code = None
    job.error_message = None


def summarize(jobs: list[AnalysisJob]) -> dict:
    counts = {s: 0 for s in (PENDING, RUNNING, COMPLETED, RETRYABLE, FAILED, CANCELLED)}
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
    counts["total"] = len(jobs)
    return counts