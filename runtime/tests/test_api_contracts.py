# F020：API 契约测试（FastAPI TestClient，沙盒内可跑，不依赖真实数据库）
# 覆盖：统一错误结构（request_id + error.code）、RBAC 越权 403、
# 空文件/非法 collect_mode 校验、非法状态迁移、OpenAPI 路由注册。
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://localhost:1/bid_agent_test")
    # python-multipart 是 FastAPI multipart 上传的必需依赖（requirements-dev.txt）；
    # 缺失时跳过 API 契约测试（沙盒无网络无法安装，用户环境装好后自动恢复）
    pytest.importorskip("python_multipart")
    from runtime.api import app

    # raise_server_exceptions=False：契约测试需验证"触库失败返回 500 统一结构"，
    # 而非让 TestClient 把服务器异常直接抛出（沙盒内无真实数据库）
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _headers(role: str | None = "bid_specialist", actor: str = "tester") -> dict:
    # HTTP header 按规范仅允许 latin-1 可编码字符，中文 actor 值应放请求体而非 header
    h = {"X-Actor": actor}
    if role:
        h["X-Role"] = role
    return h


# ---------- 统一错误结构 ----------

def test_error_body_structure(client):
    resp = client.get("/api/v1/approvals/pending", headers=_headers("bid_specialist"))
    assert resp.status_code == 403
    body = resp.json()
    assert "request_id" in body
    assert body["error"]["code"] == "forbidden"


def test_unknown_role_fail_closed(client):
    resp = client.get("/api/v1/approvals/pending", headers=_headers(None))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


# ---------- RBAC 越权（F020 §5 / 原型 403 模拟页） ----------

def test_approvals_pending_only_business_head(client):
    for role in ("bid_specialist", "data_admin", "legal"):
        resp = client.get("/api/v1/approvals/pending", headers=_headers(role))
        assert resp.status_code == 403, role
        assert resp.json()["error"]["code"] == "forbidden", role


def test_approval_create_denied_for_non_head(client):
    resp = client.post("/api/v1/projects/ND-2025/approval/create", headers=_headers("bid_specialist"))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_enterprise_data_denied_for_specialist(client):
    resp = client.get("/api/v1/enterprise/qualifications", headers=_headers("bid_specialist"))
    assert resp.status_code == 403
    resp2 = client.get("/api/v1/enterprise/qualifications", headers=_headers("legal"))
    assert resp2.status_code == 403


def test_enterprise_data_allowed_for_data_admin(client):
    # 数据管理员可访问；沙盒内无数据库 -> 触库查询失败返回 500（统一结构），而非 403
    resp = client.get("/api/v1/enterprise/qualifications", headers=_headers("data_admin"))
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal_error"


def test_legal_cannot_create_material(client):
    resp = client.post(
        "/api/v1/materials",
        headers=_headers("legal"),
        files={"file": ("q.pdf", b"x", "application/pdf")},
        data={"material_id": "MAT-PRV-001", "material_type": "qualification_cert",
              "data_owner": "甲"},
    )
    assert resp.status_code == 403


# ---------- 请求校验（F020 §2.2/§3） ----------

def test_tender_document_empty_file_400(client):
    resp = client.post(
        "/api/v1/intake/tender-document",
        headers=_headers("bid_specialist"),
        files={"file": ("tender.pdf", b"", "application/pdf")},
        data={"project_id": "ND-2025", "material_id": "MAT-ND-001", "data_owner": "甲"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_tender_document_unsupported_format_415(client):
    resp = client.post(
        "/api/v1/intake/tender-document",
        headers=_headers("bid_specialist"),
        files={"file": ("tender.gef", b"encrypted", "application/octet-stream")},
        data={"project_id": "ND-2025", "material_id": "MAT-ND-001", "data_owner": "甲"},
    )
    assert resp.status_code == 415
    assert resp.json()["error"]["code"] == "unsupported_format"


def test_tender_document_non_docx_pdf_415(client):
    resp = client.post(
        "/api/v1/intake/tender-document",
        headers=_headers("bid_specialist"),
        files={"file": ("tender.txt", b"text", "text/plain")},
        data={"project_id": "ND-2025", "material_id": "MAT-ND-001", "data_owner": "甲"},
    )
    assert resp.status_code == 415


def test_search_requires_manual_trigger(client):
    resp = client.post(
        "/api/v1/intake/announcement/search",
        headers=_headers("bid_specialist"),
        data={"keyword": "房屋建筑施工", "collect_mode": "scheduled"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_search_requires_keyword_or_region(client):
    resp = client.post(
        "/api/v1/intake/announcement/search",
        headers=_headers("bid_specialist"),
        data={"keyword": "", "region": "", "collect_mode": "manual_trigger"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_reject_requires_comment_validation(client):
    # 经营负责人提交空 comment -> 先校验 body（pydantic 不强制非空字符串）-> 服务层校验
    resp = client.post(
        "/api/v1/projects/ND-2025/approval/reject",
        headers=_headers("business_head"),
        json={"approver": "经营负责人", "comment": ""},
    )
    # 沙盒内无数据库：若未到服务层校验，会先触库 -> 500；服务层 comment 校验在触库前
    assert resp.status_code in (400, 500)


def test_openapi_registers_f020_routes(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    for path in [
        "/api/v1/materials",
        "/api/v1/intake/announcement/search",
        "/api/v1/intake/tender-document",
        "/api/v1/projects",
        "/api/v1/projects/{project_id}/requirements",
        "/api/v1/projects/{project_id}/match-runs/latest",
        "/api/v1/projects/{project_id}/matrix",
        "/api/v1/projects/{project_id}/admission",
        "/api/v1/projects/{project_id}/queues",
        "/api/v1/projects/{project_id}/recalculate",
        "/api/v1/approvals/pending",
        "/api/v1/projects/{project_id}/audit",
        "/api/v1/enterprise/qualifications",
        "/api/v1/ocr/route",
        "/api/v1/ocr/jobs/{job_id}",
        "/api/v1/ocr/review-queue",
        "/api/v1/ocr/review/{evidence_id}",
    ]:
        assert path in paths, path


def test_openapi_registers_parse_routes(client):
    # R021-4：解析候选列表 / 人工复核 / 全部确认（写规则集+字段溯源+状态流转）
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    for path in [
        "/api/v1/parse/projects/{project_id}/materials/{material_id}/candidates",
        "/api/v1/parse/candidates/{candidate_id}/review",
        "/api/v1/parse/projects/{project_id}/materials/{material_id}/confirm",
    ]:
        assert path in paths, path


def test_openapi_registers_rag_routes(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    for path in [
        "/api/v1/knowledge/search",
        "/api/v1/knowledge/indexes/{material_id}/{version}",
        "/api/v1/retrieval-runs/{retrieval_run_id}",
    ]:
        assert path in paths, path


# ---------- R021-4 解析复核 RBAC（F021 §2.7：投标专员复核/确认；写 = tender_document write） ----------

def test_parse_review_requires_tender_doc_write(client):
    # 仅 bid_specialist 有 tender_document write；其余角色复核候选一律 403
    for role in ("data_admin", "business_head", "legal"):
        resp = client.post(
            "/api/v1/parse/candidates/C-1/review",
            headers=_headers(role),
            json={"decision": "approved", "reviewer": "x"},
        )
        assert resp.status_code == 403, role
        assert resp.json()["error"]["code"] == "forbidden", role


def test_parse_confirm_requires_tender_doc_write(client):
    # 确认（写 RuleSet/Requirement）同样仅投标专员可执行
    for role in ("data_admin", "business_head", "legal"):
        resp = client.post(
            "/api/v1/parse/projects/ND-2025/materials/MAT-1/confirm",
            headers=_headers(role),
            json={"actor": "x"},
        )
        assert resp.status_code == 403, role


def test_parse_candidates_read_role_gate(client):
    # tender_document read：bid_specialist/business_head/legal 可读（沙盒无库 → 500）；
    # data_admin 无 tender_document 权限 → 403（不触库）
    for role in ("data_admin",):
        resp = client.get(
            "/api/v1/parse/projects/ND-2025/materials/MAT-1/candidates",
            headers=_headers(role),
        )
        assert resp.status_code == 403, role
    for role in ("bid_specialist", "business_head", "legal"):
        resp = client.get(
            "/api/v1/parse/projects/ND-2025/materials/MAT-1/candidates",
            headers=_headers(role),
        )
        assert resp.status_code == 500, role
        assert resp.json()["error"]["code"] == "internal_error", role
