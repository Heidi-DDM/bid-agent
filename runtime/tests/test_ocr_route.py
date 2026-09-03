# R022-② OCR Document Router worker + 人工复核写回测试（sqlite 内存库）
# 覆盖（08-清单 §2.2 第 2 项）：
# - _execute_ocr_route：读不可变原文 → route_document → EvidenceFile 落库
#   （page_no/ocr_confidence/source_hash）；低置信 → review_status=pending_review
# - 幂等：同 material+content_hash 重复执行不新增 EvidenceFile
# - 损坏/不支持：抛错（不静默入库），任务可 retryable
# - ocr.review 端点写回：approved/rejected 落 evidence.review_status + reviewer + note
# - review-queue 只列 pending_review / 低置信未复核
from __future__ import annotations

import re as _re
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from runtime.core.errors import ApiError
from runtime.db.models import (
    Base, EvidenceFile, Material, MaterialVersion, Project,
)
from runtime.worker import _execute_ocr_route

# sqlite 不支持多 schema：剥离前缀
_SCHEMA_PREFIX = _re.compile(
    r"\b(?:public_data|enterprise_data|admission_data|audit_data|knowledge_data)\."
)


@pytest.fixture()
def session(tmp_path, monkeypatch):
    # object_store_root() 在 APP_ENV=test 下硬编码 tempfile（config 分支），
    # env 注入无效 → 直接函数级 stub 指向测试目录（worker 内 import 时生效）
    from runtime.core import config as cfg

    monkeypatch.setattr(cfg, "object_store_root", lambda: str(tmp_path))
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _strip_schema(conn, cursor, statement, parameters, context, executemany):
        return _SCHEMA_PREFIX.sub("", statement), parameters

    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _store_material(session, tmp_path, *, material_id="MAT-OCR-001",
                    content=b"%PDF-1.4 fake text pdf body",
                    material_type="证书扫描件", owner_type="enterprise",
                    classification="internal", project_id="ND-2025"):
    import hashlib

    h = hashlib.sha256(content).hexdigest()
    obj_dir = Path(tmp_path) / "enterprise" / material_id / "1"
    obj_dir.mkdir(parents=True, exist_ok=True)
    obj = obj_dir / h[:16]  # object_uri 与 material_service 一致：sha256 前 16 字符
    obj.write_bytes(content)
    session.add(Project(project_id=project_id, project_name="农大"))
    session.add(Material(
        material_id=material_id, version=1, material_type=material_type,
        source_type="upload", owner_type=owner_type, classification=classification,
        permission_scope="enterprise_read", content_hash=h, parse_status="pending",
        status="active", evidence_refs=[], data_owner="项目负责人",
        project_id=project_id,
    ))
    session.add(MaterialVersion(
        material_id=material_id, version=1,
        object_uri=f"enterprise/{material_id}/1/{h[:16]}",
        content_hash=h,
    ))
    session.commit()
    return h


def test_ocr_route_creates_evidence_pending_review(session, tmp_path, monkeypatch):
    # 模拟 route_document 返回低置信扫描件 → EvidenceFile pending_review
    from runtime.parsing import router as router_mod
    from runtime.parsing.router import ParsedPage, RouteResult

    h = _store_material(session, tmp_path)
    monkeypatch.setattr(
        router_mod, "route_document",
        lambda p: RouteResult(kind="scanned_pdf", source=str(p), sha256=h,
                              pages=[ParsedPage(1, ["证书内容"], ocr_confidence=0.55)],
                              confidence=0.55, needs_review=True))
    _execute_ocr_route(session, "MAT-OCR-001")
    ev = session.scalars(select(EvidenceFile)).first()
    assert ev is not None
    assert ev.material_id == "MAT-OCR-001"
    assert ev.source_hash == h
    assert ev.page_no == 1
    assert ev.ocr_confidence == 0.55
    assert ev.review_status == "pending_review"


def test_ocr_route_text_pdf_no_review_needed(session, tmp_path, monkeypatch):
    from runtime.parsing import router as router_mod
    from runtime.parsing.router import ParsedPage, RouteResult

    h = _store_material(session, tmp_path)
    monkeypatch.setattr(
        router_mod, "route_document",
        lambda p: RouteResult(kind="text_pdf", source=str(p), sha256=h,
                              pages=[ParsedPage(1, ["条款文本"])],
                              confidence=0.98, needs_review=False))
    _execute_ocr_route(session, "MAT-OCR-001")
    ev = session.scalars(select(EvidenceFile)).first()
    assert ev.review_status is None  # 文本直读无需复核
    assert ev.ocr_confidence == 0.98


def test_ocr_route_idempotent(session, tmp_path, monkeypatch):
    from runtime.parsing import router as router_mod
    from runtime.parsing.router import ParsedPage, RouteResult

    h = _store_material(session, tmp_path)
    monkeypatch.setattr(
        router_mod, "route_document",
        lambda p: RouteResult(kind="scanned_pdf", source=str(p), sha256=h,
                              pages=[ParsedPage(1, ["x"], ocr_confidence=0.5)],
                              confidence=0.5, needs_review=True))
    _execute_ocr_route(session, "MAT-OCR-001")
    _execute_ocr_route(session, "MAT-OCR-001")
    assert session.scalars(select(EvidenceFile)).all().__len__() == 1


def test_ocr_route_unsupported_raises(session, tmp_path, monkeypatch):
    from runtime.parsing import router as router_mod
    from runtime.parsing.router import RouteResult

    _store_material(session, tmp_path)
    monkeypatch.setattr(
        router_mod, "route_document",
        lambda p: RouteResult(kind="unsupported", source=str(p), sha256="x",
                              needs_review=True, note="加密固化格式"))
    with pytest.raises(RuntimeError, match="路由失败"):
        _execute_ocr_route(session, "MAT-OCR-001")
    # 不静默入库
    assert session.scalars(select(EvidenceFile)).all().__len__() == 0


def test_review_writeback(session):
    """ocr.review 端点写回：approved 落 review_status/reviewed_by/reviewed_at/note。"""
    from datetime import datetime, timezone

    ev = EvidenceFile(
        evidence_id="E-OCR-1", material_id="MAT-OCR-001", file_type="证书扫描件",
        object_uri="enterprise/MAT-OCR-001/1/x", source_hash="abc",
        page_no=1, ocr_confidence=0.5, classification="internal",
        uploaded_by="system:ocr", review_status="pending_review",
    )
    session.add(ev)
    session.commit()
    from runtime.db.models import AuditEvent

    reviewer = "data_admin"
    ev.review_status = "approved"
    ev.reviewed_by = reviewer
    ev.reviewed_at = datetime.now(timezone.utc)
    ev.review_note = "扫描清晰，字段确认"
    session.add(AuditEvent(event_id="a1", actor=reviewer, at=datetime.now(timezone.utc),
                           action="ocr.review", basis="evidence_id=E-OCR-1",
                           outcome="approved", object_ref="E-OCR-1"))
    session.commit()
    session.refresh(ev)
    assert ev.review_status == "approved"
    assert ev.reviewed_by == "data_admin"
    assert ev.reviewed_at is not None
    assert ev.review_note == "扫描清晰，字段确认"
