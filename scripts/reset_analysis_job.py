# -*- coding: utf-8 -*-
"""运维一次性脚本：人工重置卡死的 analysis_job（心跳耗尽重试后永久滞留 retryable）。

背景：MAT-TEST-001 的 parse.tender_document job（9de626f423614438）因 OBJECT_STORE_ROOT
模板占位符 + worker 进程异常，3 次领取全部 heartbeat_timeout，attempts=3=max_attempts，
状态机无自动恢复路径（claim_job 只在 attempts<max 时 retryable->pending）。

本脚本按 job_id 执行人工重置：retryable/running -> pending, attempts=0，
并写 audit 留痕（F009：audit_events 仅追加，人工运维动作也须可追溯）。

用法：
    .venv/bin/python scripts/reset_analysis_job.py <job_id>
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from runtime.core.config import database_url  # noqa: E402
from runtime.db import api_service  # noqa: E402
from runtime.db.models import AnalysisJob  # noqa: E402

RESETTABLE = {"retryable", "running"}


def main() -> int:
    if len(sys.argv) != 2:
        print("用法: .venv/bin/python scripts/reset_analysis_job.py <job_id>")
        return 2
    job_id = sys.argv[1].strip()

    eng = create_engine(database_url())
    with Session(eng) as s:
        job = s.get(AnalysisJob, job_id)
        if job is None:
            print(f"!! 任务不存在: {job_id}")
            return 1
        before = f"status={job.status} attempts={job.attempts}/{job.max_attempts} " \
                 f"error_code={job.error_code} input_ref={job.input_ref} project_id={job.project_id}"
        if job.status not in RESETTABLE:
            print(f"!! 任务当前状态 {job.status} 不可重置（仅 {sorted(RESETTABLE)}）")
            print(f"   当前: {before}")
            return 1
        print(f"重置前: {before}")

        job.status = "pending"
        job.attempts = 0
        job.error_code = None
        job.error_message = None
        job.heartbeat_at = None
        job.runner_id = None
        api_service.audit(
            s, actor="manual:ops", action="analysis_job.reset",
            basis=f"job_id={job_id} kind={job.kind} input_ref={job.input_ref} "
                  f"project_id={job.project_id}",
            outcome="pending attempts=0（heartbeat_timeout 耗尽重试后人工恢复）",
            object_ref=job.project_id or job.input_ref,
        )
        s.commit()
        print(f"重置后: status={job.status} attempts={job.attempts}/{job.max_attempts} "
              f"error_code={job.error_code}")
        print("audit 已留痕 action=analysis_job.reset")
    eng.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())