"""ADR-004 P0 的真实 PostgreSQL 薄切片验收。

这不是 SQLite 逻辑单测的替代：本模块针对已迁移的隔离 PostgreSQL 验证
API → 项目身份/截止门禁 → 作业表 → worker 执行器/心跳这条写入链路。
绝不针对共享演示库运行；必须显式提供专用 ``DATABASE_URL``。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from runtime.db import identity_service, worker_service
from runtime.db.models import AnalysisJob, AuditEvent, Project
from runtime.worker import process_one

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("DATABASE_URL", "")
from runtime.tests._dbguard import integration_db_allowed
_DB_OK, _DB_WHY = integration_db_allowed()
needs_db = pytest.mark.skipif(not _DB_OK, reason=_DB_WHY)



@pytest.fixture()
def engine():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    # 本模块只应在专用的隔离库执行。清理范围限定为本 P0 用例会写入的对象；CASCADE
    # 确保 project_identities 等从属数据不残留。不要改成指向共享演示或生产数据库。
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE analysis_jobs, public_data.project_identities, public_data.projects, "
            "audit_data.audit_events RESTART IDENTITY CASCADE"
        ))
    yield engine
    engine.dispose()


@pytest.fixture()
def api_client(engine, monkeypatch):
    """以真实 PostgreSQL session 覆盖 FastAPI 依赖，验证 HTTP 契约而非 mock。"""
    from runtime.api import app
    from runtime.routers.deps import get_db

    def _db_override():
        with Session(engine) as session:
            yield session

    monkeypatch.setenv("AUTH_DEV_HEADERS", "true")
    app.dependency_overrides[get_db] = _db_override
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _project(session: Session, project_id: str, *, deadline_at: datetime | None = None) -> Project:
    project = Project(
        project_id=project_id,
        project_name=f"ADR-004 隔离验收项目 {project_id}",
        admission_status="matching",
        bid_deadline=deadline_at.astimezone(timezone(timedelta(hours=8))).date() if deadline_at else None,
        bid_deadline_at=deadline_at,
    )
    session.add(project)
    session.commit()
    return project


@needs_db
def test_postgresql_api_worker_and_p0_gates(api_client, engine):
    """真实库：身份冲突同步拒绝、精确截止落库、worker 终态阻断与进程心跳。"""
    headers = {"X-Role": "business_head", "X-Actor": "adr004-verifier"}

    # 1) 项目编号不一致是硬冲突：API 在创建 match.run 之前同步返回 409，不能把失败
    # 推给异步 worker 才发现。
    with Session(engine) as session:
        conflict_project = _project(session, "P0-PG-CONFLICT")
        result = identity_service.evaluate_project_identity(
            session,
            project_id=conflict_project.project_id,
            actor="adr004-verifier",
            expected={"tender_no": "HB-EXPECTED-001"},
            actual={"tender_no": "HB-ACTUAL-999"},
        )
        assert result["status"] == "identity_conflict"

    response = api_client.post("/api/v1/projects/P0-PG-CONFLICT/match", headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["detail"]["reason"] == "identity_conflict"
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AnalysisJob)) == 0
        actions = set(session.scalars(select(AuditEvent.action).where(
            AuditEvent.object_ref == "P0-PG-CONFLICT"
        )))
        assert {"project.identity_checked", "match.trigger_denied"} <= actions

    # 2) 人工登记精确截止时间必须有偏移并真实写为 timestamptz；随后把项目改为已经
    # 截止，worker 领取既有任务时仍会二次门禁并以不可重试终态失败，防 API 漏网。
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    with Session(engine) as session:
        _project(session, "P0-PG-DEADLINE")

    deadline_url = "/api/v1/projects/P0-PG-DEADLINE/bid-deadline"
    response = api_client.post(deadline_url, headers=headers, json={
        "bid_deadline_at": future.isoformat(),
        "source": "ADR-004 隔离验收：招标文件第 1 页",
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deadline_precision"] == "datetime"
    assert payload["bid_deadline"] == future.astimezone(timezone(timedelta(hours=8))).date().isoformat()

    with Session(engine) as session:
        project = session.get(Project, "P0-PG-DEADLINE")
        assert project is not None and project.bid_deadline_at is not None
        assert project.bid_deadline_at.tzinfo is not None
        assert project.bid_deadline_at.astimezone(timezone.utc).replace(microsecond=0) == future.replace(microsecond=0)
        # 为避免等待实际时间，直接写入一个已明确过去的事实；worker 会以该事实执行门禁。
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        project.bid_deadline = past.astimezone(timezone(timedelta(hours=8))).date()
        project.bid_deadline_at = past
        session.commit()
        job, created = worker_service.create_job(
            session,
            kind="match.run",
            input_ref="P0-PG-DEADLINE",
            project_id="P0-PG-DEADLINE",
        )
        assert created is True
        worker_service.record_heartbeat(session, "adr004-pg-worker", poll_interval_seconds=1.0, pid=1,
                                        hostname="integration-test")
        assert worker_service.worker_status(session, stale_seconds=60)["available"] is True
        assert process_one(session, "adr004-pg-worker", stale_seconds=600) is True
        session.refresh(job)
        assert job.status == "failed"
        assert job.error_code == "gate_overdue"
        actions = set(session.scalars(select(AuditEvent.action).where(
            AuditEvent.object_ref == "P0-PG-DEADLINE"
        )))
        assert {"project.bid_deadline_set", "project.overdue", "match.gate_blocked"} <= actions
