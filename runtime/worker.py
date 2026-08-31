# R018/F018 §4.2：单 worker 任务循环
# 轮询 analysis_jobs，领取任务 -> 心跳 -> 执行 -> 完成/失败重试。
# 进程异常时任务保持 running 超时后可恢复为 retryable（F018 §4.4），
# 不直接把项目推进到下一业务状态。
#
# 用法：
#   python -m runtime.worker            # 默认配置
#   WORKER_POLL_INTERVAL_SECONDS=2 python -m runtime.worker
from __future__ import annotations

import datetime as _dt
import logging
import logging.config
import os
import signal
import sys
import time

from runtime.core import jobs as job_logic
from runtime.core.config import job_running_timeout_seconds, logging_config, worker_poll_interval_seconds
from runtime.db.models import AnalysisJob
from runtime.db.worker_service import claim_job, finish_job, heartbeat_job

logger = logging.getLogger("runtime.worker")

RUNNER_ID = os.environ.get("RUNNER_ID", f"worker-{os.getpid()}")


def process_one(session, runner_id: str, stale_seconds: int) -> bool:
    """领取并执行一个任务。返回是否处理了任务。"""
    job = claim_job(session, runner_id, stale_seconds=stale_seconds)
    if job is None:
        return False
    job_id = job.job_id
    kind = job.kind
    logger.info("领取任务 job_id=%s kind=%s attempts=%s", job_id, kind, job.attempts)
    try:
        # 心跳保持（单次任务执行期间周期性刷新；演示执行器即刻返回）
        heartbeat_job(session, job_id)
        # F021+ 在此接入具体执行器（解析/匹配/准入），当前为可验证的占位执行
        _execute(kind, job.input_ref)
        finish_job(session, job_id, outcome="completed")
        logger.info("完成任务 job_id=%s kind=%s", job_id, kind)
    except Exception as exc:
        logger.exception("任务执行异常 job_id=%s kind=%s error=%s", job_id, kind, type(exc).__name__)
        from runtime.db.worker_service import fail_then_retryable

        fail_then_retryable(session, job_id, error_code=type(exc).__name__, error_message=str(exc)[:500])
    return True


def _execute(kind: str, input_ref: str | None) -> None:
    """任务执行器占位：记录输入引用，后续 R021-R024 接入真实能力。

    规则：缺失/未核实字段不得推断（AGENTS.md 红线）。
    """
    if not input_ref:
        raise ValueError("任务缺少 input_ref")
    # 演示：模拟耗时工作，便于验证心跳与超时恢复
    time.sleep(0.2)


def run_forever() -> None:
    stale_seconds = int(job_running_timeout_seconds())
    poll_interval = worker_poll_interval_seconds()

    stop = False

    def _on_signal(signum, frame):
        nonlocal stop
        logger.info("收到信号 %s，优雅退出", signum)
        stop = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from runtime.core.config import database_url

    engine = create_engine(database_url())
    SessionLocal = sessionmaker(bind=engine)

    logger.info("worker 启动 runner_id=%s poll=%ss stale=%ss", RUNNER_ID, poll_interval, stale_seconds)
    while not stop:
        session = SessionLocal()
        try:
            processed = process_one(session, RUNNER_ID, stale_seconds=stale_seconds)
        except Exception:
            logger.exception("worker 循环异常")
            processed = False
        finally:
            session.close()
        if not processed:
            time.sleep(poll_interval)


def main() -> None:
    logging.config.dictConfig(logging_config())
    run_forever()


if __name__ == "__main__":
    main()