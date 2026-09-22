"""ADR-004 Iteration 1 PostgreSQL integration verification.

Runs only against an explicit, disposable isolated DATABASE_URL.  It verifies the
0017 tables through the real FastAPI/router/RBAC path and never targets a shared
or production database.
"""
from __future__ import annotations

import os
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from runtime.core import rbac
from runtime.db import identity_service, workflow_service
from runtime.db.models import AdmissionResult, Project, RemediationTask

pytestmark = pytest.mark.integration
DATABASE_URL = os.environ.get("DATABASE_URL", "")
from runtime.tests._dbguard import integration_db_allowed
_DB_OK, _DB_WHY = integration_db_allowed()
needs_db = pytest.mark.skipif(not _DB_OK, reason=_DB_WHY)



@pytest.fixture()
def engine():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    # Only tables created/used by this thin slice are cleared.  The caller is
    # responsible for pointing DATABASE_URL at a disposable verification DB.
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE admission_data.remediation_tasks, admission_data.preparation_records, "
            "admission_data.quick_prescreen_runs, admission_data.admission_results, "
            "admission_data.match_runs, public_data.project_identities, public_data.projects, "
            "audit_data.audit_events RESTART IDENTITY CASCADE"
        ))
    yield engine
    engine.dispose()


@pytest.fixture()
def api_client(engine, monkeypatch):
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


def _project(session: Session, project_id: str = "I1-PG") -> None:
    session.add(Project(
        project_id=project_id, project_name="ADR-004 Iteration 1 隔离验收项目",
        admission_status="matching", bid_deadline=date(2026, 12, 31),
    ))
    session.commit()


@needs_db
def test_iteration1_postgresql_http_workflow_and_task_privacy(api_client, engine):
    """真实 PG：快速预核、立项、任务状态和两级角色证据可见性均经过 HTTP/RBAC（F026）。"""
    with Session(engine) as session:
        _project(session)
    root = "/api/v1/projects/I1-PG"
    bid = {"X-Role": "bid_specialist", "X-Actor": "pg-bid"}
    head = {"X-Role": "business_head", "X-Actor": "pg-head"}
    data = {"X-Role": "data_admin", "X-Actor": "pg-data"}

    # A warning blocks formal match but not the facts-only early workflow.
    with Session(engine) as session:
        result = identity_service.evaluate_project_identity(
            session, project_id="I1-PG", actor="pg-bid",
            expected={"project_name": "隔离验收项目", "bid_deadline": "2026-12-31"},
            actual={"project_name": "隔离验收项目", "bid_deadline": "2027-01-01"},
        )
        assert result["status"] == "identity_warning"
    denied = api_client.post(root + "/match", headers=bid)
    assert denied.status_code == 409, denied.text
    assert denied.json()["error"]["detail"]["reason"] == "identity_warning"

    prescreen = api_client.post(root + "/quick-prescreen", headers=bid)
    assert prescreen.status_code == 200, prescreen.text
    assert "不是投标建议" in prescreen.json()["disclaimer"]
    preparation = api_client.post(root + "/preparations", headers=head, json={"reason": "隔离环境准备核验"})
    assert preparation.status_code == 200, preparation.text
    decided = api_client.post(
        root + f"/preparations/{preparation.json()['preparation_id']}/approve",
        headers=head, json={"comment": "仅允许投入准备工作"},
    )
    assert decided.status_code == 200 and decided.json()["state"] == workflow_service.PREPARATION_APPROVED

    task = api_client.post(root + "/tasks", headers=data, json={
        "task_type": "evidence_supplement", "title": "补充隔离验证证据", "requirement_id": "I1-PG-REQ",
    })
    assert task.status_code == 200, task.text
    task_id = task.json()["task_id"]
    evidence = api_client.post(root + f"/tasks/{task_id}/evidence", headers=data, json={
        "evidence_refs": ["isolated-private-evidence-v1"], "comment": "已提交，待核验重算",
    })
    assert evidence.status_code == 200 and evidence.json()["state"] == workflow_service.TASK_EVIDENCE_SUBMITTED
    # F026 §5：投标专员承接原 data_admin 职责（含证据核验），任务证据引用可见；
    # data_admin 兼容别名与投标专员同权，审批层仍仅经营负责人。
    bid_view = api_client.get(root + "/tasks", headers=bid)
    assert bid_view.status_code == 200
    assert bid_view.json()["items"][0]["evidence_refs"] == ["isolated-private-evidence-v1"]
    assert bid_view.json()["items"][0]["evidence_count"] == 1

    with Session(engine) as session:
        assert session.get(Project, "I1-PG").admission_status == "matching"
        assert session.scalar(select(RemediationTask).where(RemediationTask.task_id == task_id)).state == workflow_service.TASK_EVIDENCE_SUBMITTED
