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


def test_candidate_card_keeps_source_scope_separate_from_project_region():
    from runtime.db.models import AnnouncementCandidate
    from runtime.routers.intake import _candidate_card

    candidate = AnnouncementCandidate(
        candidate_id="C-REGION-001",
        source_id="hebtig",
        source_name="惠招标（河北交投）工程类",
        title="某地施工招标公告",
        url="https://ebidding.hebtig.com/jyxx/example.html",
        category="招标专区-工程类",
        region=None,
        publish_date=None,
        import_status="pending",
    )
    card = _candidate_card(candidate)
    assert card["region"] is None
    assert card["source_region_scope"] == "河北省"
    assert card["region_inferred"] is False
    assert card["region_provenance"] == "source_scope_only"
    assert "region" in card["missing_fields"]


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


def test_search_idempotency_key_is_windowed(client, monkeypatch):
    """搜索在同一 5 分钟窗口内幂等，窗口切换后允许合规重试。"""
    import runtime.routers.intake as intake
    seen = []

    def fake_create_job(session, **kwargs):
        seen.append(kwargs["idempotency_key"])
        class Job:
            job_id = "JOB-WINDOW"
        return Job(), len(seen) == 1

    monkeypatch.setattr(intake.worker_service, "create_job", fake_create_job)
    monkeypatch.setattr(intake.time, "time", lambda: 1500.0)
    for _ in range(2):
        resp = client.post("/api/v1/intake/announcement/search",
                           headers=_headers("bid_specialist"),
                           json={"keyword": "施工", "region": "河北省"})
        assert resp.status_code == 200
    monkeypatch.setattr(intake.time, "time", lambda: 1800.0)
    resp = client.post("/api/v1/intake/announcement/search",
                       headers=_headers("bid_specialist"),
                       json={"keyword": "施工", "region": "河北省"})
    assert resp.status_code == 200
    assert seen[0] == seen[1]
    assert seen[2] != seen[1]


def test_search_ignores_legacy_category_scale(client):
    # v1.7（09-优化方案 §3.1）：种类/规模不再是搜索请求的采集过滤条件——服务端
    # 忽略且不校验其取值（含未登记种类、超长规模），照常创建任务（沙盒无库 → 500
    # 统一结构，说明已通过参数校验、未被拦截）。
    for payload in (
        {"keyword": "施工", "region": "河北省", "category": "航天工程",
         "collect_mode": "manual_trigger"},
        {"keyword": "施工", "region": "河北省", "scale": "超" * 30,
         "collect_mode": "manual_trigger"},
        {"keyword": "施工", "region": "石家庄市", "category": "市政公用",
         "scale": "中型（400万–1亿）", "collect_mode": "manual_trigger"},
    ):
        resp = client.post(
            "/api/v1/intake/announcement/search",
            headers=_headers("bid_specialist"),
            data=payload,
        )
        assert resp.status_code == 500, (payload, resp.text)  # 仅沙盒无库
        assert resp.json()["error"]["code"] == "internal_error", payload


# ---------- R004：候选公告确认入库 RBAC（announcement write：投标专员 + 经营负责人） ----------

def test_candidate_import_requires_announcement_write(client):
    # import = 写公开事实（抓详情原文入库）；2026-09-11 用户决策：business_head 增
    # announcement:write（与 bid_specialist 同过 RBAC 门，沙盒无库 → 500）；
    # 其余角色 403 不触库
    url = "/api/v1/intake/announcement/candidates/C-1/import"
    for role in ("data_admin", "legal"):
        resp = client.post(url, headers=_headers(role))
        assert resp.status_code == 403, role
        assert resp.json()["error"]["code"] == "forbidden", role
    for role in ("bid_specialist", "business_head"):
        resp = client.post(url, headers=_headers(role))
        assert resp.status_code == 500, role  # 已过 RBAC，沙盒无库 → 统一 500 结构
        assert resp.json()["error"]["code"] == "internal_error", role


def test_candidate_status_read_gate(client):
    # 候选读 = announcement read：bid_specialist/business_head/legal 可读（无库 500）；
    # data_admin 403
    url = "/api/v1/intake/announcement/candidates/C-1"
    resp = client.get(url, headers=_headers("data_admin"))
    assert resp.status_code == 403
    for role in ("bid_specialist", "business_head", "legal"):
        resp = client.get(url, headers=_headers(role))
        assert resp.status_code == 500, role
        assert resp.json()["error"]["code"] == "internal_error", role


def test_search_rejects_unknown_source(client):
    resp = client.post(
        "/api/v1/intake/announcement/search",
        headers=_headers("bid_specialist"),
        json={"keyword": "房屋建筑施工", "region": "河北省",
              "sources": ["not_registered_platform"], "collect_mode": "manual_trigger"},
    )
    # 未知源校验在触库前 → 400（沙盒无库也稳定）
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
        "/api/v1/intake/announcement/search/{job_id}",
        "/api/v1/intake/announcement/candidates/{candidate_id}/import",
        "/api/v1/intake/announcement/candidates/{candidate_id}",
        "/api/v1/intake/announcement/candidates/{candidate_id}/text",
        "/api/v1/intake/tender-document",
        "/api/v1/projects",
        "/api/v1/projects/{project_id}/requirements",
        "/api/v1/projects/{project_id}/match-runs/latest",
        "/api/v1/projects/{project_id}/match-runs/compare",
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
        "/api/v1/parse/projects/{project_id}/requirements",  # F021 §2.1 v1.3 聚合
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


def test_openapi_registers_r022_enterprise_routes(client):
    # R022：批量导入 / 核验队列 / 批量核验 / 重导入 / 过期扫描
    # F022 §5.1 v1.3：受控导入 preview/commit（Excel/CSV 台账）
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    for path in [
        "/api/v1/enterprise/import",
        "/api/v1/enterprise/import/preview",
        "/api/v1/enterprise/import/commit",
        "/api/v1/enterprise/verification-queue",
        "/api/v1/enterprise/verify",
        "/api/v1/enterprise/reimport",
        "/api/v1/enterprise/expire-overdue",
    ]:
        assert path in paths, path


def test_r022_import_requires_enterprise_write(client):
    # 批量导入写企业资料：data_admin 允许；bid_specialist/legal 403（不触库）
    body = {"kind": "qualifications", "rows": [{"category": "X", "level": "一级"}],
            "data_owner": "项目负责人"}
    resp = client.post("/api/v1/enterprise/import", json=body,
                       headers=_headers("bid_specialist"))
    assert resp.status_code == 403, resp.text
    resp2 = client.post("/api/v1/enterprise/import", json=body,
                        headers=_headers("legal"))
    assert resp2.status_code == 403, resp2.text


def test_r022_ledger_preview_commit_role_gate(client):
    # 受控导入 preview/commit = enterprise write：仅 data_admin；其余 403（不触库）
    files = {"file": ("q.csv", "资质类别,资质等级\n建筑工程施工总承包,特级".encode(), "text/csv")}
    data = {"kind": "qualifications", "mapping": "{}", "data_owner": "项目负责人"}
    for path in ("/api/v1/enterprise/import/preview", "/api/v1/enterprise/import/commit"):
        for role in ("bid_specialist", "legal", "business_head"):
            resp = client.post(path, headers=_headers(role), files=files, data=data)
            assert resp.status_code == 403, f"{path} {role}: {resp.text}"


def test_r022_ledger_validation_before_db(client):
    # 触库前校验（data_admin 过 RBAC 后、不依赖数据库）：
    # preview 未知 kind / commit 未知 kind、mapping 非法字段键、personnel 缺 category → 400
    csv_bytes = "资质类别,资质等级\n建筑工程施工总承包,特级".encode()
    files = {"file": ("q.csv", csv_bytes, "text/csv")}
    resp = client.post("/api/v1/enterprise/import/preview",
                       headers=_headers("data_admin"), files=files,
                       data={"kind": "unknown"})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_request"

    resp = client.post("/api/v1/enterprise/import/commit",
                       headers=_headers("data_admin"), files=files,
                       data={"kind": "qualifications", "mapping": "not-json",
                             "data_owner": "项目负责人"})
    assert resp.status_code == 400, resp.text
    assert "JSON 对象" in resp.json()["error"]["message"]

    resp = client.post("/api/v1/enterprise/import/commit",
                       headers=_headers("data_admin"), files=files,
                       data={"kind": "qualifications",
                             "mapping": '{"资质类别": "not_a_field"}',
                             "data_owner": "项目负责人"})
    assert resp.status_code == 400, resp.text
    assert "不允许的字段键" in resp.json()["error"]["message"]

    resp = client.post("/api/v1/enterprise/import/commit",
                       headers=_headers("data_admin"), files=files,
                       data={"kind": "personnel", "mapping": "{}",
                             "data_owner": "项目负责人"})
    assert resp.status_code == 400, resp.text
    assert "category" in resp.json()["error"]["message"]


def test_r022_verify_requires_enterprise_verify(client):
    # 核验动作：仅 data_admin（enterprise verify）；投标专员 403
    body = {"kind": "qualifications", "ids": ["Q-x"], "action": "approve"}
    resp = client.post("/api/v1/enterprise/verify", json=body,
                       headers=_headers("bid_specialist"))
    assert resp.status_code == 403, resp.text


def test_r022_verification_queue_readable_by_admin_and_head(client):
    # 核验队列读门禁：enterprise:read 仅 data_admin（RBAC F020 §5）；
    # business_head（审批人）/legal 均 403（不触库）。沙盒无库时 data_admin 过权限检查
    # 后触库失败 → 500 统一错误结构（与既有 test_enterprise_data_allowed_for_data_admin 约定一致）
    resp = client.get("/api/v1/enterprise/verification-queue",
                      headers=_headers("data_admin"))
    assert resp.status_code == 500, resp.text
    assert resp.json()["error"]["code"] == "internal_error"
    for role in ("business_head", "legal"):
        resp = client.get("/api/v1/enterprise/verification-queue",
                          headers=_headers(role))
        assert resp.status_code == 403, f"{role}: {resp.text}"


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


def test_parse_review_semantics_validation(client):
    """F021 §2.1 v1.3 复核语义：rejected 必须带结构化原因；revised 必须带修正值；
    approved 不得带 revised_payload（请求体校验 422，先于触库）。"""
    base = {"reviewer": "投标专员"}
    # rejected 无原因 → 422（不触库，沙盒同样成立）
    resp = client.post(
        "/api/v1/parse/candidates/C-1/review",
        headers=_headers("bid_specialist"),
        json={**base, "decision": "rejected", "review_note": "字迹不清"},
    )
    assert resp.status_code == 422, resp.text
    # rejected 带枚举原因 → 通过请求体校验（沙盒无库 → 触库 500，证明语义校验已放行）
    resp = client.post(
        "/api/v1/parse/candidates/C-1/review",
        headers=_headers("bid_specialist"),
        json={**base, "decision": "rejected", "review_note": "扫描不清"},
    )
    assert resp.status_code == 500, resp.text
    assert resp.json()["error"]["code"] == "internal_error"
    # rejected 不得携带修正值
    resp = client.post(
        "/api/v1/parse/candidates/C-1/review",
        headers=_headers("bid_specialist"),
        json={**base, "decision": "rejected", "review_note": "条款冲突",
              "revised_payload": {"assertion": "x"}},
    )
    assert resp.status_code == 422, resp.text
    # revised 无修正值 → 422；带修正值 → 过校验（沙盒触库 500）
    resp = client.post(
        "/api/v1/parse/candidates/C-1/review",
        headers=_headers("bid_specialist"),
        json={**base, "decision": "revised"},
    )
    assert resp.status_code == 422, resp.text
    resp = client.post(
        "/api/v1/parse/candidates/C-1/review",
        headers=_headers("bid_specialist"),
        json={**base, "decision": "revised",
              "revised_payload": {"assertion": "修正原文", "value": "一级"}},
    )
    assert resp.status_code == 500, resp.text
    # approved 不得带修正值
    resp = client.post(
        "/api/v1/parse/candidates/C-1/review",
        headers=_headers("bid_specialist"),
        json={**base, "decision": "approved", "revised_payload": {"assertion": "x"}},
    )
    assert resp.status_code == 422, resp.text


def test_parse_requirements_aggregate_role_gate(client):
    # 聚合读门禁与 candidates 一致：tender_document read（bid_specialist/business_head/legal 可读，
    # 沙盒无库 → 500）；data_admin 403 不触库
    resp = client.get("/api/v1/parse/projects/ND-2025/requirements",
                      headers=_headers("data_admin"))
    assert resp.status_code == 403, resp.text
    for role in ("bid_specialist", "business_head", "legal"):
        resp = client.get("/api/v1/parse/projects/ND-2025/requirements",
                          headers=_headers(role))
        assert resp.status_code == 500, role
        assert resp.json()["error"]["code"] == "internal_error", role


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


# ---------- R024 §2.4 第 1 项：结果页只读 HTTP 契约 ----------

def test_result_pages_role_gate(client):
    """结果页只读端点最小权限：result/match read 角色（bid_specialist/business_head）
    可读（沙盒无库 → 触库 500 统一结构，证明已过 RBAC）；无权限角色 403 不触库。"""
    urls = [
        "/api/v1/projects/ND-2025/admission",
        "/api/v1/projects/ND-2025/queues",
        "/api/v1/projects/ND-2025/matrix",
        "/api/v1/projects/ND-2025/requirements",
        "/api/v1/projects/ND-2025/match-runs/latest",
    ]
    allowed = ("bid_specialist", "business_head")
    denied = ("data_admin", "legal")
    for url in urls:
        for role in denied:
            resp = client.get(url, headers=_headers(role))
            assert resp.status_code == 403, (url, role)
            assert resp.json()["error"]["code"] == "forbidden", (url, role)
        for role in allowed:
            resp = client.get(url, headers=_headers(role))
            assert resp.status_code == 500, (url, role)  # 已过 RBAC，触库失败统一结构
            assert resp.json()["error"]["code"] == "internal_error", (url, role)


def test_result_pages_empty_state_via_http(client):
    """结果页空态：项目不存在/无运行时不隐式创建任务，返回业务空结构（404/空）而非崩。"""
    # match-runs/latest：空态约定 {run_id: null}（F020 §2.2.3）；触库前不校验项目存在
    resp = client.get("/api/v1/projects/NOPE-1/match-runs/latest", headers=_headers("bid_specialist"))
    assert resp.status_code in (200, 500)  # 沙盒无库：通过 RBAC 后触库 500；真库返回 {run_id: null}
    if resp.status_code == 200:
        assert resp.json().get("run_id") is None
    # 列表页空态：无项目 → 200 items:[]（真库行为；沙盒无库 500 结构）
    resp = client.get("/api/v1/projects", headers=_headers("bid_specialist"))
    assert resp.status_code in (200, 500)


def test_approvals_pending_empty_state(client):
    """审批队列空态：经营负责人可读，无待审批返回 items:[]（真库行为；沙盒无库 500）。"""
    resp = client.get("/api/v1/approvals/pending", headers=_headers("business_head"))
    assert resp.status_code in (200, 500)
    if resp.status_code == 200:
        assert resp.json()["items"] == []


def test_candidate_text_read_gate(client):
    # P1（2026-09-11）：公告固化原文读取（溯源定位）= announcement read；
    # data_admin 403 不触库，bid_specialist/business_head/legal 过门后沙盒无库 → 500
    url = "/api/v1/intake/announcement/candidates/C-1/text"
    resp = client.get(url, headers=_headers("data_admin"))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    for role in ("bid_specialist", "business_head", "legal"):
        resp = client.get(url, headers=_headers(role))
        assert resp.status_code == 500, role
        assert resp.json()["error"]["code"] == "internal_error", role
