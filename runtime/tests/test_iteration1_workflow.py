"""ADR-004 Iteration 1: factual prescreen, preparation, and remediation tasks.

These tests intentionally use the same SQLite schema-prefix stripping as the P0
suite. They verify business safety rules at both service and HTTP boundaries;
they do not claim remote deployment or human acceptance.
"""
from __future__ import annotations

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from runtime.core import rbac
from runtime.core.errors import ApiError
from runtime.db import identity_service, lifecycle_service, workflow_service
from runtime.db.models import (
    AdmissionResult,
    AuditEvent,
    Base,
    MatchRun,
    PreparationRecord,
    Project,
    ProjectIdentity,
    QuickPrescreenRun,
    RemediationTask,
)

_SCHEMA_PREFIX = re.compile(r"\b(?:public_data|enterprise_data|admission_data|audit_data|knowledge_data)\.")
FUTURE = dt.date(2026, 12, 31)
PAST = dt.date(2025, 10, 30)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _strip_schema(conn, cursor, statement, parameters, context, executemany):
        return _SCHEMA_PREFIX.sub("", statement), parameters

    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _project(session: Session, project_id: str = "I1-P", *, deadline=FUTURE, status="matching") -> Project:
    row = Project(project_id=project_id, project_name="Iteration 1 脱敏项目", admission_status=status,
                  bid_deadline=deadline)
    session.add(row)
    session.commit()
    return row


def _admission(session: Session, project_id: str = "I1-P", *, run_id="MR-I1", blocked=None, pending=None, review=None):
    run = MatchRun(run_id=run_id, project_id=project_id, rule_set_id="RS-I1", as_of="2026-09-17",
                   mode="gate", coverage={"complete": True}, status="completed")
    row = AdmissionResult(
        result_id=f"AR-{run_id}", run_id=run_id, project_id=project_id, rule_set_id="RS-I1",
        qualification_result={}, scoring_result={}, operational_readiness={},
        internal_admission_result={"status": "blocked_missing_data", "eligible": False},
        internal_admission_eligible=False, blocked_items=blocked or [], pending_items=pending or [],
        review_items=review or [], result_freshness="current", state="final",
    )
    session.add_all([run, row])
    session.commit()
    return row


def _audits(session: Session, action: str):
    return session.scalars(select(AuditEvent).where(AuditEvent.action == action)).all()


def test_prescreen_is_factual_and_does_not_mutate_formal_flow(session):
    _project(session, deadline=None, status="matching")
    before_runs = session.scalars(select(MatchRun)).all()
    result = workflow_service.create_prescreen(session, project_id="I1-P", actor="toubiao")

    assert "不是投标建议" in result["disclaimer"]
    assert any(x["code"] == "bid_deadline_missing" for x in result["missing"])
    assert session.get(Project, "I1-P").admission_status == "matching"
    assert session.scalars(select(MatchRun)).all() == before_runs
    assert session.scalar(select(QuickPrescreenRun)) is not None
    assert _audits(session, "quick_prescreen.created")


def test_preparation_is_separate_and_overdue_is_blocked(session):
    p = _project(session)
    created = workflow_service.create_preparation(
        session, project_id=p.project_id, actor="jingying", role=rbac.BUSINESS_HEAD, reason="安排文件编制资源"
    )
    assert created["state"] == workflow_service.PREPARATION_PENDING
    assert "不代表正式同意投标" in created["meaning"]
    with pytest.raises(ApiError) as duplicate:
        workflow_service.create_preparation(
            session, project_id=p.project_id, actor="jingying", role=rbac.BUSINESS_HEAD, reason="重复"
        )
    assert duplicate.value.code == "invalid_state_transition"

    approved = workflow_service.decide_preparation(
        session, project_id=p.project_id, preparation_id=created["preparation_id"], actor="jingying",
        role=rbac.BUSINESS_HEAD, decision=workflow_service.PREPARATION_APPROVED, comment="允许投入准备工作"
    )
    assert approved["state"] == workflow_service.PREPARATION_APPROVED
    assert session.get(Project, p.project_id).admission_status == "matching"
    with pytest.raises(ApiError):
        workflow_service.decide_preparation(
            session, project_id=p.project_id, preparation_id=created["preparation_id"], actor="jingying",
            role=rbac.BUSINESS_HEAD, decision=workflow_service.PREPARATION_DECLINED, comment="重复决定"
        )

    expired = _project(session, "I1-EXPIRED", deadline=PAST)
    with pytest.raises(ApiError) as exc:
        workflow_service.create_preparation(
            session, project_id=expired.project_id, actor="jingying", role=rbac.BUSINESS_HEAD, reason="过期项目"
        )
    assert exc.value.status_code == 409 and exc.value.detail["reason"] == "overdue"


def test_identity_warning_blocks_formal_match_and_recalculate_until_confirmed(session):
    p = _project(session)
    identity_service.evaluate_project_identity(
        session, project_id=p.project_id, actor="toubiao",
        expected={"project_name": "脱敏项目", "bid_deadline": "2026-12-31"},
        actual={"project_name": "脱敏项目", "bid_deadline": "2027-01-01"},
    )
    for action in ("match", "recalculate"):
        gate = lifecycle_service.gate_reason(session, p, action=action, actor="toubiao")
        assert gate and gate[0] == "identity_warning"
    identity_service.confirm_project_identity(
        session, project_id=p.project_id, actor="toubiao", note="已核对延期/版本说明，确属同一项目"
    )
    assert lifecycle_service.gate_reason(session, p, action="match", actor="toubiao") is None


def test_task_sync_is_idempotent_and_only_new_match_can_close(session):
    _project(session)
    first = _admission(
        session, blocked=[{"req": "R-B", "match_result": "not_satisfied", "text": "资格明确不满足"}],
        pending=[{"req": "R-P", "text": "缺少业绩证明", "recommended_material_types": ["performance"]}],
        review=[{"req": "R-R", "text": "条款待人工复核"}],
    )
    metrics = workflow_service.sync_tasks_from_admission(session, project_id="I1-P", match_run_id="MR-I1", admission=first)
    assert metrics == {"touched": 3, "created": 3, "reopened": 0, "closed": 0}
    assert session.scalars(select(RemediationTask)).all().__len__() == 3
    second_metrics = workflow_service.sync_tasks_from_admission(session, project_id="I1-P", match_run_id="MR-I1", admission=first)
    assert second_metrics == {"touched": 3, "created": 0, "reopened": 0, "closed": 0}

    pending = session.scalar(select(RemediationTask).where(RemediationTask.requirement_id == "R-P"))
    blocked = session.scalar(select(RemediationTask).where(RemediationTask.requirement_id == "R-B"))
    submitted = workflow_service.submit_evidence(
        session, project_id="I1-P", task_id=pending.task_id, role=rbac.DATA_ADMIN, actor="ziliao",
        evidence_refs=["EV-脱敏-001"], comment="已提交待核验证据"
    )
    assert submitted["state"] == workflow_service.TASK_EVIDENCE_SUBMITTED
    assert submitted["evidence_refs"] == ["EV-脱敏-001"]
    with pytest.raises(ApiError):
        workflow_service.resolve_task(session, project_id="I1-P", task_id=pending.task_id,
                                      role=rbac.DATA_ADMIN, actor="ziliao", resolution="自行宣布满足")
    with pytest.raises(ApiError):
        workflow_service.submit_evidence(session, project_id="I1-P", task_id=blocked.task_id,
                                         role=rbac.BID_SPECIALIST, actor="toubiao", evidence_refs=["EV-x"])

    newer = _admission(session, run_id="MR-I1-2", blocked=[], pending=[], review=[])
    metrics = workflow_service.sync_tasks_from_admission(session, project_id="I1-P", match_run_id="MR-I1-2", admission=newer)
    assert metrics["closed"] == 3
    assert {t.state for t in session.scalars(select(RemediationTask)).all()} == {workflow_service.TASK_CLOSED}


def test_task_evidence_reference_visible_to_responsible_and_head(session):
    # F026/ADR-005：企业证据引用向责任角色（投标专员=含兼容别名）与经营负责人显示；
    # 两级角色之外（匿名）在路由层即 403，不触达任务明细
    _project(session)
    row = workflow_service.create_task(
        session, project_id="I1-P", role=rbac.BID_SPECIALIST, actor="toubiao", task_type="evidence_supplement",
        title="补充脱敏证据", description=None, assignee_role=rbac.BID_SPECIALIST, requirement_id="R-P",
        due_at=None, evidence_required=[]
    )
    workflow_service.submit_evidence(session, project_id="I1-P", task_id=row["task_id"],
                                     role=rbac.BID_SPECIALIST, actor="toubiao",
                                     evidence_refs=["private-material-version-42"])
    owner_view = workflow_service.list_tasks(session, project_id="I1-P", role=rbac.BID_SPECIALIST, actor="toubiao")[0]
    alias_view = workflow_service.list_tasks(session, project_id="I1-P", role=rbac.DATA_ADMIN, actor="ziliao")[0]
    head_view = workflow_service.list_tasks(session, project_id="I1-P", role=rbac.BUSINESS_HEAD, actor="jingying")[0]
    assert owner_view["evidence_refs"] == ["private-material-version-42"]
    assert alias_view["evidence_refs"] == ["private-material-version-42"]
    assert head_view["evidence_refs"] == ["private-material-version-42"]


@pytest.fixture()
def api_client(monkeypatch):
    from runtime.api import app
    from runtime.routers.deps import get_db

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _strip_schema(conn, cursor, statement, parameters, context, executemany):
        return _SCHEMA_PREFIX.sub("", statement), parameters

    Base.metadata.create_all(engine)

    def _db_override():
        with Session(engine) as s:
            yield s

    monkeypatch.setenv("AUTH_DEV_HEADERS", "true")
    app.dependency_overrides[get_db] = _db_override
    with TestClient(app, raise_server_exceptions=False) as client:
        client.engine = engine
        yield client
    app.dependency_overrides.clear()


def test_workflow_http_role_boundary_and_state_transitions(api_client):
    with Session(api_client.engine) as s:
        _project(s, "I1-API")
    p = "/api/v1/projects/I1-API"
    bid = {"X-Role": "bid_specialist", "X-Actor": "toubiao"}
    head = {"X-Role": "business_head", "X-Actor": "jingying"}
    data = {"X-Role": "data_admin", "X-Actor": "ziliao"}

    prescreen = api_client.post(p + "/quick-prescreen", headers=bid)
    assert prescreen.status_code == 200 and "不是投标建议" in prescreen.json()["disclaimer"]
    # F026：data_admin 兼容别名归并为投标专员 → 快速预核放行（职责收编）
    assert api_client.post(p + "/quick-prescreen", headers=data).status_code == 200
    assert api_client.post(p + "/quick-prescreen").status_code == 403

    create = api_client.post(p + "/preparations", headers=head, json={"reason": "准备技术标资源"})
    assert create.status_code == 200
    assert api_client.post(p + "/preparations", headers=bid, json={"reason": "越权"}).status_code == 403
    approved = api_client.post(p + f"/preparations/{create.json()['preparation_id']}/approve", headers=head,
                               json={"comment": "仅允许投入准备工作"})
    assert approved.status_code == 200 and approved.json()["state"] == "preparation_approved"

    task = api_client.post(p + "/tasks", headers=data, json={
        "task_type": "evidence_supplement", "title": "补充证据", "requirement_id": "R-HTTP"
    })
    assert task.status_code == 200
    task_id = task.json()["task_id"]
    # F026：data_admin 由 create_task 内部归一为 bid_specialist 责任角色 → 同角色提交放行；
    # 匿名 403（不触库，fail-closed）
    assert api_client.post(p + f"/tasks/{task_id}/evidence",
                           json={"evidence_refs": ["private-ref"]}).status_code == 403
    evidence = api_client.post(p + f"/tasks/{task_id}/evidence", headers=bid,
                               json={"evidence_refs": ["private-ref"]})
    assert evidence.status_code == 200 and evidence.json()["state"] == "evidence_submitted"
    bid_list = api_client.get(p + "/tasks", headers=bid)
    assert bid_list.status_code == 200
    # 责任角色（投标专员）可见证据引用；两级角色之外不触达（路由层 403）
    assert "evidence_refs" in bid_list.json()["items"][0]
    assert bid_list.json()["items"][0]["evidence_refs"] == ["private-ref"]
