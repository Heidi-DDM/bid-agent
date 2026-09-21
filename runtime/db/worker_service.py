# F018 §4.2 / §4.4：worker 任务服务
# 单 worker 轮询 analysis_jobs，按状态机领取任务并写入心跳。
# 本文件依赖 SQLAlchemy（与纯逻辑状态机 runtime/core/jobs.py 分离，保证后者无依赖可测）。
from __future__ import annotations

import datetime as _dt
import logging
import uuid
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from runtime.core import jobs as job_logic
from runtime.core.jobs import AnalysisJob as JobState
from runtime.db.models import AnalysisJob

logger = logging.getLogger("runtime.worker")

STALE_SECONDS = 600  # 与 JOB_RUNNING_TIMEOUT_SECONDS 对齐（config.job_running_timeout_seconds()）


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def create_job(
    session: Session,
    *,
    kind: str,
    input_ref: str,
    project_id: Optional[str] = None,
    max_attempts: int = 3,
    idempotency_key: Optional[str] = None,
    retry_failed: bool = False,
) -> tuple[AnalysisJob, bool]:
    """创建任务；幂等键重复时返回既有任务（created=False）。

    retry_failed=True：既有任务处于终态 failed/cancelled 时将其复位 pending
    （attempts 归零、错误清空）并返回 created=True——供“确认后编排”等必须真正执行的
    调度使用。2026-09-15 背景：提前调度产生的失败 match.run 占住幂等键，confirm 之后
    永远拿回失败任务，匹配不再执行。"""
    key = idempotency_key or job_logic.build_idempotency_key(kind, input_ref, project_id)
    existing = session.scalar(
        select(AnalysisJob).where(AnalysisJob.idempotency_key == key)
    )
    if existing is not None:
        if retry_failed and existing.status in ("failed", "cancelled"):
            existing.status = job_logic.PENDING
            existing.attempts = 0
            existing.error_code = None
            existing.error_message = None
            session.add(existing)
            session.commit()
            return existing, True
        return existing, False
    job = AnalysisJob(
        job_id=uuid.uuid4().hex[:16],
        kind=kind,
        status=job_logic.PENDING,
        attempts=0,
        max_attempts=max_attempts,
        input_ref=input_ref,
        project_id=project_id,
        idempotency_key=key,
    )
    session.add(job)
    session.commit()
    return job, True


def claim_job(session: Session, runner_id: str, stale_seconds: int = STALE_SECONDS) -> Optional[AnalysisJob]:
    """领取一个可执行任务：pending 优先；running 心跳超时先回收为 retryable。

    UPDATE 幂等领取（条件更新，避免并发重复领取；单 worker 部署下仍保证正确性）。
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    now_iso = now.isoformat()

    # 1) 回收心跳超时的 running 任务 -> retryable（F018 §4.4）
    stale_cutoff = now - _dt.timedelta(seconds=stale_seconds)
    session.execute(
        update(AnalysisJob)
        .where(
            AnalysisJob.status == job_logic.RUNNING,
            AnalysisJob.heartbeat_at < stale_cutoff,
        )
        .values(status=job_logic.RETRYABLE, error_code="heartbeat_timeout", updated_at=now)
    )

    # 2) retryable（attempts < max）-> pending
    session.execute(
        update(AnalysisJob)
        .where(
            AnalysisJob.status == job_logic.RETRYABLE,
            AnalysisJob.attempts < AnalysisJob.max_attempts,
        )
        .values(status=job_logic.PENDING, updated_at=now)
    )
    session.commit()

    # 3) 领取 pending
    job = session.scalar(
        select(AnalysisJob)
        .where(AnalysisJob.status == job_logic.PENDING)
        .order_by(AnalysisJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = job_logic.RUNNING
    job.attempts += 1
    job.runner_id = runner_id
    job.heartbeat_at = now
    job.updated_at = now
    session.commit()
    return job


def heartbeat_job(session: Session, job_id: str) -> bool:
    """刷新 running 任务心跳；不存在或非 running 返回 False。"""
    now = _dt.datetime.now(_dt.timezone.utc)
    result = session.execute(
        update(AnalysisJob)
        .where(AnalysisJob.job_id == job_id, AnalysisJob.status == job_logic.RUNNING)
        .values(heartbeat_at=now, updated_at=now)
    )
    session.commit()
    return result.rowcount > 0


def finish_job(
    session: Session,
    job_id: str,
    *,
    outcome: str,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """结束任务：completed / failed（attempts 耗尽）。终态不可重复迁移。"""
    job = session.get(AnalysisJob, job_id)
    if job is None:
        return
    if job.status != job_logic.RUNNING:
        raise ValueError(f"仅 running 任务可结束，当前 {job.status}")
    if outcome == "completed":
        job.status = job_logic.COMPLETED
    elif outcome == "failed":
        job.status = job_logic.FAILED
        job.error_code = error_code
        job.error_message = error_message
    else:
        raise ValueError(f"不支持的任务结束状态: {outcome}")
    job.updated_at = _dt.datetime.now(_dt.timezone.utc)
    session.commit()


def fail_then_retryable(session: Session, job_id: str, error_code: str, error_message: str) -> None:
    """执行失败但未耗尽重试次数：退回 retryable，等待重新领取。"""
    job = session.get(AnalysisJob, job_id)
    if job is None or job.status in job_logic.TERMINAL_STATES:
        return
    job.status = (
        job_logic.FAILED
        if job.attempts >= job.max_attempts
        else job_logic.RETRYABLE
    )
    job.error_code = error_code
    job.error_message = error_message
    job.updated_at = _dt.datetime.now(_dt.timezone.utc)
    session.commit()


# ---------- ADR-004 §2.6：worker 进程级心跳（P0-01） ----------

def record_heartbeat(
    session: Session,
    runner_id: str,
    *,
    poll_interval_seconds: float | None = None,
    pid: int | None = None,
    hostname: str | None = None,
) -> None:
    """worker 每轮轮询（含空闲）upsert 一行进程心跳；/readyz 据此判定异步能力是否可用。"""
    from runtime.db.models import WorkerHeartbeat

    now = _dt.datetime.now(_dt.timezone.utc)
    row = session.get(WorkerHeartbeat, runner_id)
    if row is None:
        row = WorkerHeartbeat(runner_id=runner_id, heartbeat_at=now, started_at=now)
        session.add(row)
    row.heartbeat_at = now
    row.poll_interval_seconds = poll_interval_seconds
    row.pid = pid
    row.hostname = hostname
    row.updated_at = now
    session.commit()


def worker_status(session: Session, *, stale_seconds: float, now: _dt.datetime | None = None) -> dict:
    """最近一次 worker 心跳 → /readyz 检查项 dict（available / heartbeat_at / age_seconds / runner_id）。

    无任何心跳行 = worker 从未启动 → available=False（不以「API 起了」冒充异步链路可用）。
    """
    from runtime.db.models import WorkerHeartbeat

    now = now or _dt.datetime.now(_dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    row = session.scalar(
        select(WorkerHeartbeat).order_by(WorkerHeartbeat.heartbeat_at.desc()).limit(1)
    )
    if row is None:
        return {
            "available": False,
            "state": "unavailable",
            "error": "无 worker 心跳记录（worker 未启动或从未连接数据库）",
            "stale_after_seconds": stale_seconds,
        }
    hb = row.heartbeat_at
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=_dt.timezone.utc)
    age = (now - hb).total_seconds()
    available = age <= stale_seconds
    out = {
        "available": available,
        "state": "ok" if available else "stale",
        "runner_id": row.runner_id,
        "heartbeat_at": hb.isoformat(),
        "age_seconds": round(age, 1),
        "stale_after_seconds": stale_seconds,
    }
    if not available:
        out["error"] = f"worker 心跳超时 {round(age)}s（阈值 {stale_seconds}s），异步任务暂不可用"
    return out
