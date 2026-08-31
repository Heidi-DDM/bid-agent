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
    assert set(body["checks"]) == {"database", "object_store", "ocr", "model"}
    for name, check in body["checks"].items():
        assert "available" in check, name


def test_readyz_database_unavailable_when_no_db(client):
    # DATABASE_URL 指向不存在的端口 -> database 检查明确失败，整体 503
    resp = client.get("/readyz")
    body = resp.json()
    assert body["checks"]["database"]["available"] is False
    assert body["ready"] is False
    assert resp.status_code == 503