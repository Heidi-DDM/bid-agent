# F020 §2.2.3 v1.22：矩阵页透传最近匹配任务状态（matrix_view.match_job）
# 背景：match.run 被 ADR-004 门禁终态拒绝时不会产生 MatchRun，此前矩阵页在
# "尚无运行"分支只能提示"排队中稍后刷新"，用户无从得知任务已终态失败及原因
# （2026-09-23 PJ-b4e65720e2 gate_overdue 实测）。本文件锁定契约：
# matrix_view.match_job 只读、取项目最近一条 match.run/match.recalculate、空态 None。
from __future__ import annotations

import re as _re

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from runtime.db import api_service
from runtime.db.models import AnalysisJob, Base, Project
from runtime.db.worker_service import create_job

_SCHEMA_PREFIX = _re.compile(
    r"\b(?:public_data|enterprise_data|admission_data|audit_data|knowledge_data)\."
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _strip_schema(conn, cursor, statement, parameters, context, executemany):
        return _SCHEMA_PREFIX.sub("", statement), parameters

    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _project(session, project_id="ND-2025"):
    session.add(Project(project_id=project_id, project_name="测试项目"))
    session.commit()


def _fail(session, job_id: str, *, code: str = "gate_overdue", msg: str = "项目已过投标截止"):
    job = session.get(AnalysisJob, job_id)
    job.status = "failed"
    job.error_code = code
    job.error_message = msg
    session.commit()


def test_matrix_view_match_job_failed(session):
    """门禁终态失败的 match.run：match_job 携带 error_code/error_message，页面据此展示失败原因。"""
    _project(session)
    job, _ = create_job(session, kind="match.run", input_ref="ND-2025", project_id="ND-2025")
    _fail(session, job.job_id)

    view = api_service.matrix_view(session, "ND-2025")

    assert view["run"]["run_id"] is None  # 无运行（失败不产生 MatchRun）
    mj = view["match_job"]
    assert mj["job_id"] == job.job_id
    assert mj["status"] == "failed"
    assert mj["error_code"] == "gate_overdue"
    assert "投标截止" in mj["error_message"]


def test_matrix_view_match_job_none_without_jobs(session):
    """空态：项目无任何匹配任务时 match_job 为 None（前端回落到"尚未触发"文案）。"""
    _project(session)
    assert api_service.matrix_view(session, "ND-2025")["match_job"] is None


def test_latest_match_job_picks_most_recent(session):
    """多任务取最近一条：match.recalculate 失败晚于 match.run 完成 → 返回失败者。"""
    _project(session)
    first, _ = create_job(session, kind="match.run", input_ref="ND-2025", project_id="ND-2025")
    first.status = "completed"
    second, _ = create_job(session, kind="match.recalculate", input_ref="ev-2", project_id="ND-2025")
    _fail(session, second.job_id, code="gate_identity_conflict", msg="疑似串档")

    mj = api_service.latest_match_job(session, "ND-2025")
    assert mj["job_id"] == second.job_id
    assert mj["kind"] == "match.recalculate"
    assert mj["error_code"] == "gate_identity_conflict"


def test_matrix_view_is_readonly_on_jobs(session):
    """只读红线（F020 §2.1）：matrix_view 反复调用不创建/不改动任务。"""
    _project(session)
    job, _ = create_job(session, kind="match.run", input_ref="ND-2025", project_id="ND-2025")
    _fail(session, job.job_id)
    for _ in range(3):
        api_service.matrix_view(session, "ND-2025")
    jobs = session.scalars(select(AnalysisJob)).all()
    assert len(jobs) == 1
    assert jobs[0].status == "failed"
