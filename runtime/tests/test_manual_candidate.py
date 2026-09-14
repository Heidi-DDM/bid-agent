# 09-优化方案 §3.1.9：Agent 线索转人工登记候选测试
# - 路由全链路（sqlite 内存库 + override get_db）：
#   登记成功（候选 pending + 容器 job completed，worker 不领取）→ 幂等重放 →
#   非法 URL 400 → RBAC 403 → 最近任务列表/候选列表可打开 → 健康检查匿名可读契约
from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from runtime.db.models import AnnouncementCandidate, AnalysisJob, AuditEvent, Base
from runtime.routers.deps import get_db

os.environ.setdefault("AUTH_DEV_HEADERS", "true")
os.environ.setdefault("APP_ENV", "test")

_SCHEMA_PREFIX = re.compile(
    r"\b(?:public_data|enterprise_data|admission_data|audit_data|knowledge_data)\."
)

URL = "https://example.com/notice/2026/0001.html"


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _strip_schema(conn, cursor, statement, parameters, context, executemany):
        return _SCHEMA_PREFIX.sub("", statement), parameters

    Base.metadata.create_all(engine)

    def _db_override():
        with Session(engine) as s:
            yield s

    from runtime.api import app

    app.dependency_overrides[get_db] = _db_override
    with TestClient(app, raise_server_exceptions=False) as c:
        c.engine = engine
        yield c
    app.dependency_overrides.clear()


def _headers(role: str = "bid_specialist", actor: str = "tester") -> dict:
    return {"X-Role": role, "X-Actor": actor}


class TestManualCandidateRegister:
    def test_register_ok(self, client):
        resp = client.post(
            "/api/v1/intake/announcement/candidates/manual",
            headers=_headers(),
            json={"title": "某市人民医院迁建项目施工招标", "url": URL,
                  "note": "Agent 会话 2026-09-04 线索"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["created"] is True
        job = client.engine.begin() and None
        with Session(client.engine) as s:
            job = s.get(AnalysisJob, body["job_id"])
            cand = s.get(AnnouncementCandidate, body["candidate_id"])
        assert job is not None
        assert job.kind == "announcement.manual_entry"
        assert job.status == "completed"          # 容器终态：worker 不领取
        assert cand is not None
        assert cand.import_status == "pending"
        assert cand.source_id == "agent_manual"
        assert cand.region is None                # 人工转录不推断地区

    def test_idempotent_same_url(self, client):
        h = _headers()
        r1 = client.post("/api/v1/intake/announcement/candidates/manual",
                         headers=h, json={"title": "标题甲", "url": URL})
        r2 = client.post("/api/v1/intake/announcement/candidates/manual",
                         headers=h, json={"title": "标题乙（重复提交）", "url": URL})
        assert r1.status_code == r2.status_code == 200
        b1, b2 = r1.json(), r2.json()
        assert b1["created"] is True and b2["created"] is False
        assert b1["job_id"] == b2["job_id"] and b1["candidate_id"] == b2["candidate_id"]

    def test_invalid_url_400(self, client):
        resp = client.post("/api/v1/intake/announcement/candidates/manual",
                           headers=_headers(),
                           json={"title": "标题", "url": "ftp://bad/notice"})
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "invalid_request"

    def test_empty_title_422(self, client):
        resp = client.post("/api/v1/intake/announcement/candidates/manual",
                           headers=_headers(),
                           json={"title": "", "url": URL})
        assert resp.status_code == 422

    def test_rbac_403_data_admin(self, client):
        # data_admin 无 announcement:write → 403（fail-closed，先于触库）
        resp = client.post("/api/v1/intake/announcement/candidates/manual",
                           headers=_headers("data_admin"),
                           json={"title": "标题", "url": URL})
        assert resp.status_code == 403

    def test_no_auth_fail_closed_403(self, client):
        resp = client.post("/api/v1/intake/announcement/candidates/manual",
                           json={"title": "标题", "url": URL})
        assert resp.status_code == 403

    def test_worker_does_not_claim_container_job(self, client):
        """容器 job=completed：claim_job 只取 pending/running 回收，登记不被执行。"""
        resp = client.post("/api/v1/intake/announcement/candidates/manual",
                           headers=_headers(), json={"title": "标题", "url": URL})
        job_id = resp.json()["job_id"]
        from runtime.db.worker_service import claim_job

        with Session(client.engine) as s:
            claimed = claim_job(s, runner_id="probe")
            s.rollback()
        assert claimed is None or claimed.job_id != job_id


class TestManualCandidateDiscovery:
    def _register(self, client, title="某大学实验楼施工招标"):
        return client.post("/api/v1/intake/announcement/candidates/manual",
                           headers=_headers(),
                           json={"title": title, "url": URL})

    def test_searches_list_includes_manual(self, client):
        self._register(client)
        resp = client.get("/api/v1/intake/announcement/searches?limit=10",
                          headers=_headers())
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert any(it["manual"] is True and it["keyword"] == "某大学实验楼施工招标"
                   and it["candidates_count"] == 1 for it in items)

    def test_candidates_openable_by_job(self, client):
        body = self._register(client).json()
        resp = client.get(
            f"/api/v1/intake/announcement/search/{body['job_id']}/candidates",
            headers=_headers())
        assert resp.status_code == 200, resp.text
        cands = resp.json()["items"]
        assert len(cands) == 1
        assert cands[0]["candidate_id"] == body["candidate_id"]
        assert cands[0]["title"] == "某大学实验楼施工招标"
        assert cands[0]["source_name"] == "Agent 线索（人工转录）"

    def test_empty_search_is_hidden_from_searches(self, client):
        from runtime.db.worker_service import create_job

        with Session(client.engine) as s:
            create_job(s, kind="announcement.search", input_ref='{"keyword":"无命中"}',
                       project_id=None, idempotency_key="empty-search")
            job = s.scalar(select(AnalysisJob).where(AnalysisJob.idempotency_key == "empty-search"))
            job.status = "completed"
            s.commit()
        resp = client.get("/api/v1/intake/announcement/searches", headers=_headers())
        assert resp.status_code == 200
        assert all(it["job_id"] != job.job_id for it in resp.json()["items"])

    def test_delete_search_soft_deletes_and_audits(self, client):
        body = self._register(client).json()
        resp = client.delete(f"/api/v1/intake/announcement/search/{body['job_id']}", headers=_headers())
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted"] is True
        again = client.delete(f"/api/v1/intake/announcement/search/{body['job_id']}", headers=_headers())
        assert again.status_code == 200 and again.json()["deleted"] is True
        listing = client.get("/api/v1/intake/announcement/searches", headers=_headers())
        assert body["job_id"] not in {it["job_id"] for it in listing.json()["items"]}
        assert client.get(f"/api/v1/intake/announcement/search/{body['job_id']}", headers=_headers()).status_code == 404
        with Session(client.engine) as s:
            event = s.scalar(select(AuditEvent).where(AuditEvent.object_ref == body["job_id"]))
            assert event is not None and event.action == "announcement.search.delete"

    def test_delete_search_requires_write_permission(self, client):
        body = self._register(client).json()
        # announcement:write 现含 business_head（2026-09-11 用户决策）→ 拒绝名单只剩 legal/data_admin
        for role in ("legal", "data_admin"):
            resp = client.delete(f"/api/v1/intake/announcement/search/{body['job_id']}",
                                 headers=_headers(role))
            assert resp.status_code == 403, role


class TestAnnouncementHealthContract:
    """F020 v1.7：announcement/health 匿名可读（与 /readyz 同级诊断，无 RBAC 门禁）。"""

    def test_anonymous_200(self, client):
        resp = client.get("/api/v1/intake/announcement/health")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["api"] == "ok"
        assert set(body["sources"][0]) >= {"source_id", "name", "enabled", "region_scope"}
        # v1.9：省域平台下辖行政区随健康检查返回，前端地区预检与服务端同口径
        src = next(s for s in body["sources"] if s["source_id"] == "hebtig")
        assert "石家庄市" in src["admin_subregions"]
        assert "next_allowed_at" in src

    def test_healthz_anonymous_200(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestRegionRecallMarker:
    """v1.9：超范围放宽二次召回的候选地区待核实标记（_candidate_region_recall 纯函数）。"""

    def _candidate(self, source_id="hebtig"):
        from runtime.db.models import AnnouncementCandidate

        return AnnouncementCandidate(
            candidate_id="c-x", search_job_id="j-x", source_id=source_id,
            source_name="x", title="t", url="https://ebidding.hebtig.com/jyxx/a.html",
            import_status="pending")

    def test_out_of_scope_recalled(self):
        from runtime.routers.intake import _candidate_region_recall

        assert _candidate_region_recall(self._candidate(), "北京市") is True

    def test_scope_and_admin_subregion_not_recalled(self):
        from runtime.routers.intake import _candidate_region_recall

        assert _candidate_region_recall(self._candidate(), "河北省") is False
        # C1 新口径（2026-09-10）：地市检索命中省级平台（admin_subregions 覆盖该市）
        # → 覆盖采集但候选地区待核实（region_recall=True，region_note 标省级来源）；
        # 同市地市源才是精确覆盖（不标待核实）。
        assert _candidate_region_recall(self._candidate(source_id="hebtig"), "石家庄市") is True
        assert _candidate_region_recall(self._candidate(source_id="sjzsggzy"), "石家庄市") is False

    def test_manual_source_never_recalled(self):
        from runtime.routers.intake import _candidate_region_recall

        assert _candidate_region_recall(self._candidate(source_id="agent_manual"), "北京市") is False

    def test_no_region_not_recalled(self):
        from runtime.routers.intake import _candidate_region_recall

        assert _candidate_region_recall(self._candidate(), None) is False

    def test_job_region_extracted_from_input_ref(self):
        import json

        from runtime.routers.intake import _job_region
        from runtime.db.models import AnalysisJob

        job = AnalysisJob(job_id="j", kind="announcement.search", status="pending",
                          idempotency_key="k",
                          input_ref=json.dumps({"keyword": "施工", "region": "石家庄市"},
                                               ensure_ascii=False))
        assert _job_region(job) == "石家庄市"
        assert _job_region(None) is None
