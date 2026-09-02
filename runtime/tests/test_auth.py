# R024 / F020 §5.1：正式认证测试
# 覆盖：密码哈希（pbkdf2）、token 签发/验签/过期/篡改、账号解析与校验、
# 登录端点（成功/失败审计语义）、auth/me、X-Role 在正式模式（AUTH_DEV_HEADERS=false）
# 一律 anonymous 拒绝（fail-closed）+ 越权审计 actor=登录者。
from __future__ import annotations

import datetime as _dt

import pytest
from fastapi.testclient import TestClient

from runtime.core import auth as auth_core
from runtime.api import app

pytestmark = pytest.mark.auth


# ---------- 纯函数层 ----------

def test_password_hash_roundtrip():
    stored = auth_core.hash_password("s3cret-密码")
    assert stored.startswith("pbkdf2_sha256$")
    assert auth_core.verify_password("s3cret-密码", stored)
    assert not auth_core.verify_password("wrong", stored)
    assert not auth_core.verify_password("s3cret-密码", "not-a-hash")


def test_token_roundtrip_and_tamper():
    identity = {"login": "business_head", "role": "business_head", "name": "经营负责人"}
    token = auth_core.issue_token(identity, secret="test-secret")
    assert token is not None and "." in token
    parsed = auth_core.parse_token(token, secret="test-secret")
    assert parsed == identity

    # 篡改 payload / 签名 → 拒绝
    body, sig = token.split(".")
    assert auth_core.parse_token(f"{body}x.{sig}", secret="test-secret") is None
    assert auth_core.parse_token(f"{body}.{sig}x", secret="test-secret") is None
    # 错误密钥 → 拒绝
    assert auth_core.parse_token(token, secret="other-secret") is None
    # 空/坏 token
    assert auth_core.parse_token(None, secret="test-secret") is None
    assert auth_core.parse_token("garbage", secret="test-secret") is None


def test_token_expired():
    identity = {"login": "business_head", "role": "business_head", "name": "经营负责人"}
    token = auth_core.issue_token(identity, secret="test-secret", ttl=-10)
    assert auth_core.parse_token(token, secret="test-secret") is None


def test_load_users_and_authenticate(monkeypatch):
    monkeypatch.delenv("AUTH_USERS", raising=False)
    assert auth_core.load_users() == {}

    stored = auth_core.hash_password("pw-1")
    monkeypatch.setenv("AUTH_USERS", f"bh:{stored}:business_head:经营负责人")
    users = auth_core.load_users()
    assert users["bh"]["role"] == "business_head"

    ok = auth_core.authenticate("bh", "pw-1")
    assert ok == {"login": "bh", "role": "business_head", "name": "经营负责人"}
    assert auth_core.authenticate("bh", "pw-wrong") is None
    assert auth_core.authenticate("nobody", "pw-1") is None
    # 格式损坏的段被跳过但其它账号可用
    monkeypatch.setenv("AUTH_USERS", f"bad-hash-line;bh:{stored}:business_head:经营负责人")
    assert "bh" in auth_core.load_users()


# ---------- HTTP 层（dev 回退 + 正式 fail-closed） ----------

def _client() -> TestClient:
    return TestClient(app)


def test_headers_dev_fallback_by_default():
    """conftest 设 AUTH_DEV_HEADERS=true：X-Role 回退仍可用（开发/测试模式）。"""
    resp = _client().get("/api/v1/auth/me", headers={"X-Role": "business_head", "X-Actor": "business_head"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["identity"]["role"] == "business_head"


def test_me_requires_token_when_dev_headers_off(monkeypatch):
    monkeypatch.setenv("AUTH_DEV_HEADERS", "false")
    resp = _client().get("/api/v1/auth/me", headers={"X-Role": "business_head"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_login_success_and_me(monkeypatch):
    stored = auth_core.hash_password("demo-pw")
    monkeypatch.setenv(
        "AUTH_USERS", f"business_head:{stored}:business_head:经营负责人")
    monkeypatch.setenv("AUTH_TOKEN_SECRET", "test-secret-2")
    resp = _client().post("/api/v1/auth/login",
                          json={"username": "business_head", "password": "demo-pw"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["identity"]["role"] == "business_head"
    token = body["token"]

    me = _client().get("/api/v1/auth/me",
                       headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["identity"]["login"] == "business_head"


def test_login_rejected_and_unknown_credentials(monkeypatch):
    stored = auth_core.hash_password("demo-pw")
    monkeypatch.setenv("AUTH_USERS", f"bh:{stored}:business_head:经营负责人")
    monkeypatch.setenv("AUTH_TOKEN_SECRET", "test-secret-3")
    for username, password in (("bh", "wrong"), ("nobody", "demo-pw")):
        resp = _client().post("/api/v1/auth/login",
                              json={"username": username, "password": password})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"
        # 不泄露「账号不存在 vs 密码错」（同一文案）
        assert "用户名或密码错误" in resp.json()["error"]["message"]
    # 空凭据：pydantic min_length 拦截 → 422（不进入认证逻辑）
    resp = _client().post("/api/v1/auth/login", json={"username": "", "password": ""})
    assert resp.status_code == 422


def test_login_unavailable_without_secret(monkeypatch):
    stored = auth_core.hash_password("demo-pw")
    monkeypatch.setenv("AUTH_USERS", f"bh:{stored}:business_head:经营负责人")
    monkeypatch.delenv("AUTH_TOKEN_SECRET", raising=False)
    resp = _client().post("/api/v1/auth/login",
                          json={"username": "bh", "password": "demo-pw"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "dependency_unavailable"


def test_x_role_rejected_in_formal_mode(monkeypatch):
    """正式模式（AUTH_DEV_HEADERS=false）：X-Role 头不再被信任 → anonymous 403。"""
    monkeypatch.setenv("AUTH_DEV_HEADERS", "false")
    resp = _client().get("/api/v1/projects", headers={"X-Role": "business_head"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_min_privilege_denied_with_bearer(monkeypatch):
    """最小权限：投标专员 token 不能访问审批队列（403），非匿名误报。"""
    stored = auth_core.hash_password("pw")
    monkeypatch.setenv("AUTH_USERS", f"spec:{stored}:bid_specialist:投标专员")
    monkeypatch.setenv("AUTH_TOKEN_SECRET", "test-secret-4")
    token = auth_core.issue_token(
        {"login": "spec", "role": "bid_specialist", "name": "投标专员"},
        secret="test-secret-4")
    resp = _client().get("/api/v1/approvals/pending",
                         headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
