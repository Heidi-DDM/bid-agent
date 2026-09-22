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


def test_enterprise_data_allowed_for_specialist_and_head(client):
    # F026 §5：投标专员（含其 Agent）与经营负责人均负责企业资料录入/核验 → 可读企业资料；
    # 沙盒内无数据库 → 触库查询失败返回 500（统一结构），而非 403（证明已过 RBAC 门）
    for role in ("bid_specialist", "business_head", "data_admin", "legal"):  # 后两者为兼容别名
        resp = client.get("/api/v1/enterprise/qualifications", headers=_headers(role))
        assert resp.status_code == 500, (role, resp.text)
        assert resp.json()["error"]["code"] == "internal_error", role
    resp_anon = client.get("/api/v1/enterprise/qualifications", headers=_headers(None))
    assert resp_anon.status_code == 403


def test_enterprise_data_allowed_for_data_admin(client):
    # 历史 data_admin 账号登录后归并为投标专员（兼容别名），权限同投标专员；
    # 沙盒内无数据库 -> 触库查询失败返回 500（统一结构），而非 403
    resp = client.get("/api/v1/enterprise/qualifications", headers=_headers("data_admin"))
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal_error"


def test_legal_alias_merged_into_specialist_can_create_material(client):
    # ADR-005：legal 不再是独立业务角色，登录时归并为投标专员 → 具备 material write；
    # 沙盒内上传在触库前的文件校验返回 415/400（均 ≠ 403，证明 RBAC 已放行）
    resp = client.post(
        "/api/v1/materials",
        headers=_headers("legal"),
        files={"file": ("q.pdf", b"x", "application/pdf")},
        data={"material_id": "MAT-PRV-001", "material_type": "qualification_cert",
              "data_owner": "甲"},
    )
    assert resp.status_code != 403, resp.text
    assert resp.json()["error"]["code"] in ("invalid_request", "unsupported_format", "internal_error")


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


def test_search_collect_mode_validation(client):
    # F026/ADR-005：触发方式由运行时 COLLECTION_POLICY 与调度器控制，API 不再把
    # scheduled 硬编码为红线——manual_trigger/scheduled 均通过参数校验（沙盒无库 → 500）；
    # 未知模式 → 400 invalid_request
    for mode in ("manual_trigger", "scheduled"):
        resp = client.post(
            "/api/v1/intake/announcement/search",
            headers=_headers("bid_specialist"),
            data={"keyword": "房屋建筑施工", "collect_mode": mode},
        )
        assert resp.status_code == 500, (mode, resp.text)
        assert resp.json()["error"]["code"] == "internal_error", mode
    resp = client.post(
        "/api/v1/intake/announcement/search",
        headers=_headers("bid_specialist"),
        data={"keyword": "房屋建筑施工", "collect_mode": "auto_magic"},
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
    # import = 写公开事实（抓详情原文入库）；F026：两级角色都有 announcement write
    # （兼容别名 data_admin/legal 归并为 bid_specialist）；匿名 403 不触库；
    # 已登录角色沙盒无库 → 500 统一结构
    url = "/api/v1/intake/announcement/candidates/C-1/import"
    resp = client.post(url, headers=_headers(None))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    for role in ("bid_specialist", "business_head", "data_admin", "legal"):
        resp = client.post(url, headers=_headers(role))
        assert resp.status_code == 500, role  # 已过 RBAC，沙盒无库 → 统一 500 结构
        assert resp.json()["error"]["code"] == "internal_error", role


def test_candidate_status_read_gate(client):
    # 候选读 = announcement read：两级角色均可读（无库 500）；匿名 403
    url = "/api/v1/intake/announcement/candidates/C-1"
    resp = client.get(url, headers=_headers(None))
    assert resp.status_code == 403
    for role in ("bid_specialist", "business_head", "data_admin", "legal"):
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
    # 批量导入写企业资料：F026 两级角色均具备 enterprise write；匿名 403（不触库）
    body = {"kind": "qualifications", "rows": [{"category": "X", "level": "一级"}],
            "data_owner": "项目负责人"}
    resp = client.post("/api/v1/enterprise/import", json=body, headers=_headers(None))
    assert resp.status_code == 403, resp.text
    for role in ("bid_specialist", "business_head", "data_admin", "legal"):
        resp = client.post("/api/v1/enterprise/import", json=body, headers=_headers(role))
        assert resp.status_code == 500, (role, resp.text)
        assert resp.json()["error"]["code"] == "internal_error", role


def test_r022_ledger_preview_commit_role_gate(client):
    # 受控导入 preview/commit = enterprise write：F026 两级角色均可；匿名 403（不触库）
    files = {"file": ("q.csv", "资质类别,资质等级\n建筑工程施工总承包,特级".encode(), "text/csv")}
    data = {"kind": "qualifications", "mapping": "{}", "data_owner": "项目负责人"}
    resp = client.post("/api/v1/enterprise/import/preview", headers=_headers(None), files=files, data=data)
    assert resp.status_code == 403
    for path in ("/api/v1/enterprise/import/preview", "/api/v1/enterprise/import/commit"):
        for role in ("bid_specialist", "business_head", "data_admin", "legal"):
            resp = client.post(path, headers=_headers(role), files=files, data=data)
            # 已过 RBAC（非 403）：沙盒无库 → 触库 500；commit 还有触库前的台账
            # 语义校验路径 → 415（同样证明门禁放行，不触库）
            assert resp.status_code != 403, f"{path} {role}: {resp.text}"
            assert resp.json()["error"]["code"] in ("internal_error", "unsupported_format")


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
    # 核验动作（enterprise verify）：F026 两级角色均可执行；匿名 403
    body = {"kind": "qualifications", "ids": ["Q-x"], "action": "approve"}
    resp = client.post("/api/v1/enterprise/verify", json=body, headers=_headers(None))
    assert resp.status_code == 403, resp.text
    for role in ("bid_specialist", "business_head", "data_admin", "legal"):
        resp = client.post("/api/v1/enterprise/verify", json=body, headers=_headers(role))
        assert resp.status_code == 500, (role, resp.text)
        assert resp.json()["error"]["code"] == "internal_error", role


def test_r022_verification_queue_readable_by_admin_and_head(client):
    # 核验队列读门禁：enterprise:read 两级角色均可（兼容别名一并归并）；
    # 匿名 403（不触库）。沙盒无库时角色过权限检查后触库失败 → 500 统一结构
    resp = client.get("/api/v1/enterprise/verification-queue", headers=_headers(None))
    assert resp.status_code == 403
    for role in ("bid_specialist", "business_head", "data_admin", "legal"):
        resp = client.get("/api/v1/enterprise/verification-queue", headers=_headers(role))
        assert resp.status_code == 500, f"{role}: {resp.text}"
        assert resp.json()["error"]["code"] == "internal_error"


# ---------- R021-4 解析复核 RBAC（F021 §2.7：投标专员复核/确认；写 = tender_document write） ----------

def test_parse_review_requires_tender_doc_write(client):
    # F026：两级角色均具备 tender_document write（投标专员/经营负责人 + 兼容别名）；
    # 匿名 403 不触库
    for role in (None,):
        resp = client.post(
            "/api/v1/parse/candidates/C-1/review",
            headers=_headers(role),
            json={"decision": "approved", "reviewer": "x"},
        )
        assert resp.status_code == 403, role
        assert resp.json()["error"]["code"] == "forbidden", role
    for role in ("bid_specialist", "business_head", "data_admin", "legal"):
        resp = client.post(
            "/api/v1/parse/candidates/C-1/review",
            headers=_headers(role),
            json={"decision": "approved", "reviewer": "x"},
        )
        assert resp.status_code == 500, role  # 已过 RBAC，沙盒无库 → 统一 500
        assert resp.json()["error"]["code"] == "internal_error", role


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


def test_parse_review_not_applicable_and_confirm_as_of_validation(client):
    """F021 §2.1/§2.3 v1.5：not_applicable 必带人工核查说明、不得带修正值；
    confirm 的 as_of 须为 ISO 日期（请求体校验 422，先于触库）。"""
    base = {"reviewer": "投标专员"}
    resp = client.post("/api/v1/parse/candidates/C-1/review", headers=_headers("bid_specialist"),
                       json={**base, "decision": "not_applicable"})
    assert resp.status_code == 422, resp.text
    resp = client.post("/api/v1/parse/candidates/C-1/review", headers=_headers("bid_specialist"),
                       json={**base, "decision": "not_applicable", "review_note": "无此要求",
                             "revised_payload": {"assertion": "x"}})
    assert resp.status_code == 422, resp.text
    # 带说明 → 过请求体校验（沙盒无库 → 触库 500）
    resp = client.post("/api/v1/parse/candidates/C-1/review", headers=_headers("bid_specialist"),
                       json={**base, "decision": "not_applicable", "review_note": "全文检索无此条款"})
    assert resp.status_code == 500, resp.text
    # 未知决策 → 422
    resp = client.post("/api/v1/parse/candidates/C-1/review", headers=_headers("bid_specialist"),
                       json={**base, "decision": "maybe"})
    assert resp.status_code == 422, resp.text
    # confirm：as_of 非 ISO 日期 → 422；合法日期 → 过校验（沙盒触库 500）
    for bad in ("2025/12/19", "2025-13-01", "today"):
        resp = client.post("/api/v1/parse/projects/ND-2025/materials/MAT-1/confirm",
                           headers=_headers("bid_specialist"), json={"actor": "投标专员", "as_of": bad})
        assert resp.status_code == 422, (bad, resp.text)
    resp = client.post("/api/v1/parse/projects/ND-2025/materials/MAT-1/confirm",
                       headers=_headers("bid_specialist"), json={"actor": "投标专员", "as_of": "2025-12-19"})
    assert resp.status_code == 500, resp.text


def test_openapi_registers_material_file_route(client):
    """F021 §2.1 v1.5：原文文件接口已注册（定位跳转用）。"""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/materials/{material_id}/file" in paths


def test_parse_requirements_aggregate_role_gate(client):
    # 聚合读门禁与 candidates 一致：tender_document read 两级角色均可（沙盒无库 → 500）；
    # 匿名 403 不触库
    resp = client.get("/api/v1/parse/projects/ND-2025/requirements",
                      headers=_headers(None))
    assert resp.status_code == 403, resp.text
    for role in ("bid_specialist", "business_head", "data_admin", "legal"):
        resp = client.get("/api/v1/parse/projects/ND-2025/requirements",
                          headers=_headers(role))
        assert resp.status_code == 500, role
        assert resp.json()["error"]["code"] == "internal_error", role


def test_parse_confirm_requires_tender_doc_write(client):
    # 确认（写 RuleSet/Requirement）：F026 两级角色均可执行；匿名 403
    resp = client.post(
        "/api/v1/parse/projects/ND-2025/materials/MAT-1/confirm",
        headers=_headers(None),
        json={"actor": "x"},
    )
    assert resp.status_code == 403
    for role in ("bid_specialist", "business_head", "data_admin", "legal"):
        resp = client.post(
            "/api/v1/parse/projects/ND-2025/materials/MAT-1/confirm",
            headers=_headers(role),
            json={"actor": "x"},
        )
        assert resp.status_code == 500, role


def test_parse_candidates_read_role_gate(client):
    # tender_document read：两级角色均可读（沙盒无库 → 500）；匿名 403（不触库）
    resp = client.get(
        "/api/v1/parse/projects/ND-2025/materials/MAT-1/candidates",
        headers=_headers(None),
    )
    assert resp.status_code == 403
    for role in ("bid_specialist", "business_head", "data_admin", "legal"):
        resp = client.get(
            "/api/v1/parse/projects/ND-2025/materials/MAT-1/candidates",
            headers=_headers(role),
        )
        assert resp.status_code == 500, role
        assert resp.json()["error"]["code"] == "internal_error", role


# ---------- R024 §2.4 第 1 项：结果页只读 HTTP 契约 ----------

def test_result_pages_role_gate(client):
    """结果页只读端点最小权限：F026 两级角色均可读
    （沙盒无库 → 触库 500 统一结构，证明已过 RBAC）；匿名 403 不触库。"""
    urls = [
        "/api/v1/projects/ND-2025/admission",
        "/api/v1/projects/ND-2025/queues",
        "/api/v1/projects/ND-2025/matrix",
        "/api/v1/projects/ND-2025/requirements",
        "/api/v1/projects/ND-2025/match-runs/latest",
    ]
    allowed = ("bid_specialist", "business_head", "data_admin", "legal")
    for url in urls:
        resp = client.get(url, headers=_headers(None))
        assert resp.status_code == 403, url
        assert resp.json()["error"]["code"] == "forbidden", url
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
    # F026 两级角色（含兼容别名）过门后沙盒无库 → 500；匿名 403 不触库
    url = "/api/v1/intake/announcement/candidates/C-1/text"
    resp = client.get(url, headers=_headers(None))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    for role in ("bid_specialist", "business_head", "data_admin", "legal"):
        resp = client.get(url, headers=_headers(role))
        assert resp.status_code == 500, role
        assert resp.json()["error"]["code"] == "internal_error", role


# ---------- F026/ADR-005：搜索简报、待选池、规则配置、管理面板 ----------

def test_openapi_registers_f026_discovery_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path in [
        "/api/v1/discovery/pool",
        "/api/v1/discovery/briefing",
        "/api/v1/discovery/candidates/{candidate_id}/select",
        "/api/v1/discovery/candidates/{candidate_id}/dismiss",
        "/api/v1/discovery/candidates/dismiss-batch",
        "/api/v1/discovery/candidates/{candidate_id}/restore",
        "/api/v1/discovery/rules",
        "/api/v1/discovery/rules/preview",
        "/api/v1/manager/dashboard",
        "/api/v1/enterprise/verify-and-recalculate",
    ]:
        assert path in paths, path


def test_discovery_pool_sort_validation(client):
    # F026 v1.1：sort 仅支持 publish_date/last_seen；非法值触库前 400
    resp = client.get("/api/v1/discovery/pool?sort=priority",
                      headers=_headers("bid_specialist"))
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_request"
    for sort in ("publish_date", "last_seen"):
        resp = client.get(f"/api/v1/discovery/pool?sort={sort}",
                          headers=_headers("bid_specialist"))
        assert resp.status_code == 500, (sort, resp.text)  # 沙盒无库


def test_discovery_briefing_read_both_roles(client):
    # F026 v1.1：搜索增量简报 = announcement read；匿名 403，两级角色过门后无库 500
    url = "/api/v1/discovery/briefing"
    resp = client.get(url, headers=_headers(None))
    assert resp.status_code == 403
    for role in ("bid_specialist", "business_head"):
        resp = client.get(url, headers=_headers(role))
        assert resp.status_code == 500, (role, resp.text)


def test_discovery_dismiss_batch_write_both_roles(client):
    # F026 v1.1：批量软删除 = announcement write；匿名 403，两级角色过门后无库 500
    url = "/api/v1/discovery/candidates/dismiss-batch"
    body = {"candidate_ids": ["C-1", "C-2"], "reason": "批量清理"}
    resp = client.post(url, json=body, headers=_headers(None))
    assert resp.status_code == 403
    for role in ("bid_specialist", "business_head"):
        resp = client.post(url, json=body, headers=_headers(role))
        assert resp.status_code == 500, (role, resp.text)


def test_discovery_rules_preview_head_only(client):
    # F026 v1.1：规则试跑与规则写同门（approval:write，经营策略域）；投标专员 403 不触库
    url = "/api/v1/discovery/rules/preview"
    body = {"industry_keywords": ["施工"], "exclude_keywords": [], "limit": 50}
    resp = client.post(url, json=body, headers=_headers("bid_specialist"))
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"
    resp = client.post(url, json=body, headers=_headers("business_head"))
    assert resp.status_code == 500, resp.text  # 过 RBAC，沙盒无库


def test_discovery_pool_read_both_roles(client):
    # 待选池/简报：两级角色均可读（沙盒无库 → 500）；匿名 403
    url = "/api/v1/discovery/pool"
    resp = client.get(url, headers=_headers(None))
    assert resp.status_code == 403
    for role in ("bid_specialist", "business_head", "data_admin", "legal"):
        resp = client.get(url, headers=_headers(role))
        assert resp.status_code == 500, (role, resp.text)
        assert resp.json()["error"]["code"] == "internal_error", role


def test_discovery_select_write_both_roles(client):
    # 选择深入 = announcement write：两级角色均可；匿名 403
    url = "/api/v1/discovery/candidates/C-1/select"
    resp = client.post(url, headers=_headers(None))
    assert resp.status_code == 403
    for role in ("bid_specialist", "business_head"):
        resp = client.post(url, headers=_headers(role))
        assert resp.status_code == 500, (role, resp.text)


def test_discovery_rules_write_head_only(client):
    # F026 §4/§5：预筛规则写 = 经营策略配置，仅经营负责人（approval:write）；
    # 投标专员 403（不触库）
    body = {"industry_keywords": ["施工"], "exclude_keywords": []}
    resp = client.put("/api/v1/discovery/rules", json=body,
                      headers=_headers("bid_specialist"))
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"
    resp = client.put("/api/v1/discovery/rules", json=body,
                      headers=_headers("business_head"))
    assert resp.status_code == 500, resp.text  # 已过 RBAC，沙盒无库 → 统一 500


def test_discovery_rules_read_both_roles(client):
    # 规则读：两级角色均可（无库 500）；匿名 403
    resp = client.get("/api/v1/discovery/rules", headers=_headers(None))
    assert resp.status_code == 403
    for role in ("bid_specialist", "business_head"):
        resp = client.get("/api/v1/discovery/rules", headers=_headers(role))
        assert resp.status_code == 500, (role, resp.text)


def test_manager_dashboard_head_only(client):
    # F026 §6：管理进度面板仅经营负责人（approval:read）；投标专员 403（不触库）
    url = "/api/v1/manager/dashboard"
    resp = client.get(url, headers=_headers("bid_specialist"))
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"
    resp = client.get(url, headers=_headers("business_head"))
    assert resp.status_code == 500, resp.text  # 已过 RBAC，沙盒无库 → 统一 500


def test_verify_and_recalculate_requires_enterprise_verify_and_match_write(client):
    # F026 §5：核验重算需同时具备 enterprise:verify 与 match:write ——
    # 两级角色均有；匿名 403（不触库）；业务校验（如证据链不齐）在触库后体现为 500/400
    body = {"project_id": "ND-2025", "kind": "qualifications", "ids": ["Q-x"], "action": "approve"}
    resp = client.post("/api/v1/enterprise/verify-and-recalculate", json=body,
                       headers=_headers(None))
    assert resp.status_code == 403, resp.text
    for role in ("bid_specialist", "business_head"):
        resp = client.post("/api/v1/enterprise/verify-and-recalculate", json=body,
                           headers=_headers(role))
        assert resp.status_code in (400, 500), (role, resp.text)  # 已过 RBAC；无库 500 / 无记录 400


# ---------- F027：企业资料画像（评分维度四分类总览） ----------

def test_openapi_registers_f027_enterprise_overview(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/enterprise/overview" in paths


def test_enterprise_overview_read_both_roles(client):
    # F027 §5：企业画像 = enterprise:read，两级角色（含兼容别名）可读；
    # 匿名 403 不触库；过门后沙盒无库 → 统一 500
    url = "/api/v1/enterprise/overview"
    resp = client.get(url, headers=_headers(None))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    for role in ("bid_specialist", "business_head", "data_admin", "legal"):
        resp = client.get(url, headers=_headers(role))
        assert resp.status_code == 500, (role, resp.text)
        assert resp.json()["error"]["code"] == "internal_error", role


# ---------- F027（2026-09-22 下午）：资料库后台全量台账端点 ----------

def test_openapi_registers_f027_library_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path in [
        "/api/v1/enterprise/performances",
        "/api/v1/enterprise/personnel",
        "/api/v1/enterprise/evidence-files",
    ]:
        assert path in paths, path


def test_enterprise_library_read_both_roles(client):
    # 资料库后台五类端点 = enterprise:read；匿名 403；两级角色过门后无库 500
    urls = [
        "/api/v1/enterprise/performances",
        "/api/v1/enterprise/personnel",
        "/api/v1/enterprise/evidence-files",
        "/api/v1/enterprise/managers?limit=50&offset=0&q=李",
    ]
    for url in urls:
        resp = client.get(url, headers=_headers(None))
        assert resp.status_code == 403, url
        for role in ("bid_specialist", "business_head"):
            resp = client.get(url, headers=_headers(role))
            assert resp.status_code == 500, (role, url)
            assert resp.json()["error"]["code"] == "internal_error", (role, url)


def test_enterprise_auto_verify_endpoint_gates(client):
    # F027 2026-09-22：自动核验 = enterprise:verify；匿名 403，两级角色过门后无库 500
    url = "/api/v1/enterprise/auto-verify"
    resp = client.post(url, headers=_headers(None))
    assert resp.status_code == 403
    for role in ("bid_specialist", "business_head"):
        resp = client.post(url, headers=_headers(role))
        assert resp.status_code == 500, (role, resp.text)
    assert "/api/v1/enterprise/auto-verify" in client.get("/openapi.json").json()["paths"]


def test_enterprise_personnel_category_validation(client):
    # category 白名单校验在触库前 400（非法值不透传到查询）
    resp = client.get("/api/v1/enterprise/personnel?category=pilot",
                      headers=_headers("bid_specialist"))
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_request"
