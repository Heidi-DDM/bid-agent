# F018 §8：进程重启不丢任务演练辅助脚本
# 用法（在项目根目录，已激活 .venv 且 DATABASE_URL 已导出）：
#   python scripts/verify_restart_drill.py submit   # 投递演练任务（幂等键去重），打印 created/job_id/status
#   python scripts/verify_restart_drill.py status   # 查询演练任务当前状态
#
# 演练流程见 runtime/README.md §8：start -> submit -> kill worker -> submit（预期 created=False）
# -> start -> status（任务仍在库中，不丢失）。
import sys
from pathlib import Path

# 脚本位于 scripts/，项目根为其上级目录：直接 python scripts/xxx.py 运行时
# sys.path[0] 是 scripts/，需显式插入项目根才能 import runtime 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from runtime.core.config import database_url
from runtime.db.models import AnalysisJob
from runtime.db.worker_service import create_job

KIND = "parse_tender"
INPUT_REF = "restart-drill-1"
PROJECT_ID = "PRJ-DRILL"


def main() -> None:
    engine = create_engine(database_url())
    with Session(engine) as s:
        cmd = sys.argv[1] if len(sys.argv) > 1 else "submit"
        if cmd == "submit":
            job, created = create_job(s, kind=KIND, input_ref=INPUT_REF, project_id=PROJECT_ID)
            print(f"created={created} job_id={job.job_id} status={job.status}")
        elif cmd == "status":
            jobs = s.scalars(select(AnalysisJob).where(AnalysisJob.project_id == PROJECT_ID)).all()
            if not jobs:
                print("未找到演练任务（任务丢失）")
                sys.exit(1)
            for j in jobs:
                print(f"job_id={j.job_id} kind={j.kind} status={j.status} attempts={j.attempts}")
        else:
            print(f"未知命令: {cmd}（支持 submit/status）")
            sys.exit(2)


if __name__ == "__main__":
    main()