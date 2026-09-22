# R024 / F020 §5.1：正式认证（纯标准库实现，无第三方依赖）
#
# 职责：
# - 账号配置：AUTH_USERS 环境变量（.env，不入 Git）
#     格式：login:pbkdf2_sha256$iterations$salt_hex$hash_hex:role:display_name
#     多账号用 ';' 分隔（约束：display_name 不含 ':' 与 ';'）
# - 密码哈希：pbkdf2_hmac(sha256)，每次注册随机盐；verify 常数时间比较
# - Token：HMAC-SHA256 签名（AUTH_TOKEN_SECRET），载荷含 sub/role/name/iat/exp
#     token = b64url(payload_json) + '.' + b64url(hmac)
# - 身份解析：Bearer token -> identity；开发回退（AUTH_DEV_HEADERS=true）供 X-Role 头
# - 原则：fail-closed —— 未配置 AUTH_USERS/AUTH_TOKEN_SECRET 即无可用账号/无法验签；
#   验签失败、过期、未知账号一律按匿名处理，不泄露细节（F020 §5.1）。
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

from runtime.core import rbac

# 环境变量键
ENV_USERS = "AUTH_USERS"
ENV_SECRET = "AUTH_TOKEN_SECRET"
ENV_DEV_HEADERS = "AUTH_DEV_HEADERS"

TOKEN_TTL_SECONDS = 8 * 3600  # 默认 8 小时（F020 §5.1）
_PBKDF2_ITERATIONS = 200_000
_HASH_ALGO = "pbkdf2_sha256"

ANONYMOUS = "anonymous"


# ---------- 配置读取 ----------

def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value is not None else None


def token_secret() -> str | None:
    return _env(ENV_SECRET)


def dev_headers_enabled() -> bool:
    """开发期 X-Role/X-Actor 回退开关（仅测试/开发，正式环境必须移除）。"""
    return _env(ENV_DEV_HEADERS) == "true"


# ---------- 密码哈希 ----------

def hash_password(password: str, iterations: int = _PBKDF2_ITERATIONS) -> str:
    """pbkdf2_sha256$iterations$salt_hex$hash_hex（salt 随机 16 字节）。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_HASH_ALGO}${iterations}${salt.hex()}${digest.hex()}"


def _parse_hash(stored: str) -> tuple[str, int, bytes, bytes] | None:
    parts = (stored or "").split("$")
    if len(parts) != 4 or parts[0] != _HASH_ALGO:
        return None
    try:
        return parts[0], int(parts[1]), bytes.fromhex(parts[2]), bytes.fromhex(parts[3])
    except ValueError:
        return None


def verify_password(password: str, stored: str) -> bool:
    parsed = _parse_hash(stored)
    if parsed is None:
        return False
    _, iterations, salt, expected = parsed
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


# ---------- 账号 ----------

def load_users() -> dict[str, dict[str, str]]:
    """解析 AUTH_USERS -> {login: {password_hash, role, name}}；配置缺失/格式错返回空（fail-closed）。"""
    raw = _env(ENV_USERS)
    users: dict[str, dict[str, str]] = {}
    if not raw:
        return users
    for segment in raw.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        parts = segment.split(":")
        if len(parts) != 4:
            continue  # 单条格式错不影响其他账号，但不放行该条
        login, pwd_hash, role, name = (p.strip() for p in parts)
        try:
            role = rbac.normalize_role(role)
        except ValueError:
            continue
        if login and pwd_hash and name and _parse_hash(pwd_hash) is not None:
            users[login] = {"password_hash": pwd_hash, "role": role, "name": name}
    return users


def authenticate(username: str, password: str) -> dict[str, str] | None:
    """校验用户名密码 -> 身份 {login, role, name}；失败返回 None（不区分原因）。"""
    users = load_users()
    record = users.get(username or "")
    if record is None:
        return None
    if not verify_password(password or "", record["password_hash"]):
        return None
    return {"login": username, "role": record["role"], "name": record["name"]}


# ---------- Token ----------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(text: str) -> bytes | None:
    try:
        padded = text + "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode(padded)
    except Exception:
        return None


def issue_token(identity: dict[str, str], *, secret: str | None = None,
                ttl: int = TOKEN_TTL_SECONDS) -> str | None:
    """签发 HMAC token；secret 未配置返回 None（签发不可用，login 端点应拒绝）。"""
    secret = secret if secret is not None else token_secret()
    if not secret:
        return None
    now = int(time.time())
    payload = {
        "ver": 1,
        "sub": identity["login"],
        "role": identity["role"],
        "name": identity["name"],
        "iat": now,
        "exp": now + ttl,
    }
    body = _b64url(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    sig = _b64url(hmac.new(secret.encode("utf-8"), body.encode("ascii"),
                           hashlib.sha256).digest())
    return f"{body}.{sig}"


def parse_token(token: str | None, *, secret: str | None = None) -> dict[str, Any] | None:
    """验签+过期校验 -> 身份 {login, role, name}；任何失败返回 None（匿名处理）。"""
    secret = secret if secret is not None else token_secret()
    if not secret or not token:
        return None
    try:
        body, sig = token.split(".", 1)
        expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"),
                            hashlib.sha256).digest()
        actual = _unb64url(sig)
        if actual is None or not hmac.compare_digest(actual, expected):
            return None
        payload = json.loads(_unb64url(body) or b"{}")
        now = int(time.time())
        if payload.get("ver") != 1 or payload.get("sub") is None:
            return None
        if not isinstance(payload.get("exp"), int) or payload["exp"] <= now:
            return None
        role = payload.get("role")
        if role not in rbac.ROLES:
            return None
        return {"login": payload["sub"], "role": role,
                "name": str(payload.get("name") or payload["sub"])}
    except Exception:
        return None


# ---------- 身份解析（路由依赖使用） ----------

def bearer_identity(request: Any) -> dict[str, str] | None:
    """从 Authorization: Bearer <token> 解析身份；失败 None（调用方按匿名处理）。"""
    auth = (request.headers.get("authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        return None
    return parse_token(auth[len("bearer "):].strip())
