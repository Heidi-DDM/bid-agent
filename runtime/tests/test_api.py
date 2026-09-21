# R018/F018：API /readyz 健康检查测试（FastAPI TestClient）
# 依赖：fastapi + httpx（requirements-dev.txt）。
# 测试环境不要求真实数据库：readyz 各检查项在依赖缺失时返回明确失败（F018 §8）。
import os

import pytest

from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://localhost:1/bid_agent_test")
    # python-multipart 缺失时跳过（见 test_api_contracts.py 说明）
    pytest.importorskip("python_multipart")
    from runtime.api import app

    with TestClient(app) as c:
        yield c


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"


def test_readyz_structure(client):
    resp = client.get("/readyz")
    assert resp.status_code in (200, 503)
    body = resp.json()
    assert "ready" in body
    # knowledge 检查为 F025 RAG 就绪项（pgvector/embedding/reranker/public-only，docs/07 §3.1）；
    # worker 为 ADR-004 §2.6 异步 worker 进程心跳检查（P0-01）
    assert set(body["checks"]) == {"database", "object_store", "ocr", "model", "knowledge", "worker"}
    for name, check in body["checks"].items():
        if name == "knowledge":
            # knowledge 为嵌套检查项（pgvector/embedding/reranker/deepseek_public_only，R025 §3.1）
            for sub, sub_check in check.items():
                assert "available" in sub_check, f"{name}.{sub}"
        else:
            assert "available" in check, name
    knowledge = body["checks"]["knowledge"]
    assert set(knowledge) == {"pgvector", "embedding", "reranker", "deepseek_public_only"}
    # 各不可用项的影响范围说明（前端全局状态提示同源，优化方案 §10.1）
    assert isinstance(body["impacts"], list)


def test_readyz_worker_unavailable_without_heartbeat(client):
    """ADR-004 §2.6：无 worker 心跳（沙盒无数据库/未启动 worker）→ worker 不可用且说明影响范围，
    不得以 API 起了冒充异步链路可用。"""
    resp = client.get("/readyz")
    body = resp.json()
    worker = body["checks"]["worker"]
    assert worker["available"] is False
    assert worker["state"] in ("unknown", "unavailable", "stale")
    assert "error" in worker
    assert any("worker" in text for text in body["impacts"])
    assert body["ready"] is False


def test_readyz_database_unavailable_when_no_db(client):
    # DATABASE_URL 指向不存在的端口 -> database 检查明确失败，整体 503
    resp = client.get("/readyz")
    body = resp.json()
    assert body["checks"]["database"]["available"] is False
    assert body["ready"] is False
    assert resp.status_code == 503