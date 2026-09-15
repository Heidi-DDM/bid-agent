# 2026-09-15「继续项目」链路修复回归：
# - worker 重解析幂等：既有材料全部候选重复 → 正常完成（此前 RuntimeError 必失败）
# - create_job(retry_failed)：failed/cancelled 终态任务复位 pending（占住幂等键的失败任务可复活）；
#   succeeded/running/pending 保持 created=False
# - schedule_post_parse（confirm 后编排）复活失败的 match.run
import re as _re

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from runtime.db import api_service, worker_service
from runtime.db.models import Base, Material, ParseCandidate

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


def _job(session, *, kind="match.run", input_ref="PJ-T", status="failed", key=None):
    job, created = worker_service.create_job(
        session, kind=kind, input_ref=input_ref, project_id="PJ-T",
        idempotency_key=key,  # None → 默认键（与调度方 build_idempotency_key 同源）
    )
    assert created
    job.status = status
    job.error_code = "X" if status == "failed" else None
    job.error_message = "boom" if status == "failed" else None
    session.add(job)
    session.commit()
    return job


def test_create_job_retry_failed_resets_terminal_failures(session):
    failed = _job(session, status="failed", key="match.run:PJ-T:test")
    job, created = worker_service.create_job(
        session, kind="match.run", input_ref="PJ-T", project_id="PJ-T",
        idempotency_key="match.run:PJ-T:test", retry_failed=True,
    )
    assert created is True and job.job_id == failed.job_id
    assert job.status == "pending" and job.attempts == 0
    assert job.error_code is None and job.error_message is None

    cancelled = _job(session, kind="knowledge_index", input_ref="M:1", status="cancelled",
                     key="idx:1")
    job2, created2 = worker_service.create_job(
        session, kind="knowledge_index", input_ref="M:1", project_id="PJ-T",
        idempotency_key="idx:1", retry_failed=True,
    )
    assert created2 is True and job2.status == "pending"


def test_create_job_without_retry_keeps_idempotency(session):
    done = _job(session, status="succeeded", key="match.run:PJ-T:test")
    job, created = worker_service.create_job(
        session, kind="match.run", input_ref="PJ-T", project_id="PJ-T",
        idempotency_key="match.run:PJ-T:test",
    )
    assert created is False and job.job_id == done.job_id and job.status == "succeeded"
    # retry_failed 对成功任务同样不复活（幂等：成功只执行一次）
    job_b, created_b = worker_service.create_job(
        session, kind="match.run", input_ref="PJ-T", project_id="PJ-T",
        idempotency_key="match.run:PJ-T:test", retry_failed=True,
    )
    assert created_b is False and job_b.status == "succeeded"


def test_schedule_post_parse_revives_failed_match_job(session):
    from runtime.db.models import RuleSet

    session.add(Material(
        material_id="MAT-T", version=1, material_type="tender_document", source_type="uploaded",
        owner_type="public", classification="public", permission_scope="public_read",
        content_hash="9c42" + "0" * 60, parse_status="parsed", status="active",
        data_owner="x", project_id="PJ-T",
    ))
    # 2026-09-15 契约：首次匹配触发依据 = 已确认规则集（材料 parsed 不再充分；
    # 存在驳回项的材料停在 manual_review，但规则集已写入即应匹配）
    session.add(RuleSet(rule_set_id="RS-T", project_id="PJ-T", version="v1",
                        created_by="t", snapshot={}))
    failed = _job(session, status="failed")
    session.commit()
    scheduled = api_service.schedule_post_parse(session, project_id="PJ-T")
    assert scheduled["match_job"] == failed.job_id
    refreshed = session.get(type(failed), failed.job_id)
    assert refreshed.status == "pending" and refreshed.error_message is None


def test_reparse_all_duplicate_candidates_completes(session, monkeypatch):
    """worker 重解析幂等：既有候选全部重复时不再 RuntimeError（2026-09-15 前必失败）。"""
    session.add(Material(
        material_id="MAT-R", version=1, material_type="tender_document", source_type="uploaded",
        owner_type="public", classification="public", permission_scope="public_read",
        content_hash="9c42" + "0" * 60, parse_status="manual_review", status="active",
        data_owner="x", project_id="PJ-T",
    ))
    session.add(ParseCandidate(
        candidate_id="MAT-R-H-001-draft", material_id="MAT-R", version=1, project_id="PJ-T",
        kind="rule_candidate",
        payload={"requirement_id": "MAT-R-H-001-draft", "req_type": "hard_requirement",
                 "category": "资质", "assertion": "原文", "rule": {"anchor_key": "qualification_grade"}},
        status="pending",
    ))
    session.commit()

    from runtime.db import parse_service
    from runtime.worker import _execute_parse_tender_document

    class _Page:
        page_no = 1
        paragraphs = ["3.2 具备…资质"]

    class _Route:  # 最小桩：路由成功 + 抽取器产出与既有候选同 ID（全部重复）
        kind = "text_pdf"
        pages = [_Page()]
        error = None

    monkeypatch.setattr("runtime.parsing.router.route_document", lambda path: _Route())
    monkeypatch.setattr(
        "runtime.parsing.extractor.extract_rule_candidates",
        lambda pages, **kw: [_RuleCand()],
    )
    monkeypatch.setattr("runtime.parsing.extractor.extract_main_card", lambda pages, **kw: [])
    monkeypatch.setattr("runtime.parsing.extractor.extract_term_candidates", lambda pages, **kw: [])
    import tempfile as _tf, os as _os
    root = _tf.mkdtemp(prefix="bid_reparse_test_")
    with open(_os.path.join(root, "m1.pdf"), "wb") as fh:
        fh.write(b"%PDF-1.4 test")
    monkeypatch.setattr("runtime.core.config.object_store_root", lambda: root)
    from runtime.db.models import MaterialVersion
    session.add(MaterialVersion(material_id="MAT-R", version=1,
                                object_uri="m1.pdf", content_hash="9c42" + "0" * 60))
    session.commit()

    # 不抛异常 = 幂等重放完成（此前 RuntimeError("未产出任何候选")）
    _execute_parse_tender_document(session, "MAT-R", project_id="PJ-T")


class _RuleCand:
    requirement_id = "MAT-R-H-001-draft"
    req_type = "hard_requirement"
    category = "资质"
    clause_ref = "招标公告 §3.2"
    assertion = "原文"
    page_no = 3
    rule = {"anchor_key": "qualification_grade"}
    evidence_required = []
    confidence = "high"
    missing_marker = False
    note = None

    def to_dict(self):
        return self.__dict__


def test_schedule_post_parse_requires_rule_set(session):
    """材料 parsed 但规则集未写入（复核未确认）→ 不触发匹配（编排只发生在确认后）。"""
    session.add(Material(
        material_id="MAT-N", version=1, material_type="tender_document", source_type="uploaded",
        owner_type="public", classification="public", permission_scope="public_read",
        content_hash="9c42" + "0" * 60, parse_status="parsed", status="active",
        data_owner="x", project_id="PJ-N",
    ))
    session.commit()
    scheduled = api_service.schedule_post_parse(session, project_id="PJ-N")
    assert scheduled["match_job"] is None
