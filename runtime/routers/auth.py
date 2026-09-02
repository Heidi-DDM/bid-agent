# R024 / F020 §5.1：认证 API
# POST /api/v1/auth/login   —— 登录签发 Bearer token（成功/失败均留审计）
# GET  /api/v1/auth/me      —— 当前身份自检（前端初始化用；未认证 401）
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from runtime.core import auth as auth_core
from runtime.core.errors import ApiError
from runtime.db import api_service
from runtime.routers.deps import current_identity, db_session, request_id

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1, description="登录名（AUTH_USERS 配置）")
    password: str = Field(..., min_length=1, description="密码")


def _audit_login(session: Session, actor: str, outcome: str, basis: str | None = None) -> None:
    """登录审计（不可静默失败：审计写入异常不得放行/阻断登录本身——记录为日志）。"""
    try:
        api_service.audit(session, actor=actor, action="auth.login",
                          basis=basis or "", outcome=outcome)
        session.commit()
    except Exception:
        session.rollback()


@router.post("/login")
def login(body: LoginBody, request: Request) -> dict:
    """登录：校验 AUTH_USERS 账号，签发 Bearer token（F020 §5.1）。"""
    rid = request_id(request)
    identity = auth_core.authenticate(body.username, body.password)
    if identity is None:
        # 不区分「账号不存在/密码错」，避免用户枚举；仍留审计
        with _login_session() as session:
            _audit_login(session, body.username or "unknown",
                         "denied:bad_credentials")
        raise ApiError("unauthorized", "登录失败：用户名或密码错误")

    secret = auth_core.token_secret()
    if not secret:
        raise ApiError("dependency_unavailable", "认证服务未配置（AUTH_TOKEN_SECRET 缺失）")
    token = auth_core.issue_token(identity, secret=secret)
    if token is None:
        raise ApiError("dependency_unavailable", "认证服务不可用（token 签发失败）")

    with _login_session() as session:
        _audit_login(session, identity["login"], "ok",
                     basis=f"role={identity['role']}")
    return {
        "request_id": rid,
        "token": token,
        "token_type": "bearer",
        "expires_in": auth_core.TOKEN_TTL_SECONDS,
        "identity": identity,
    }


@router.get("/me")
def me(request: Request) -> dict:
    """当前身份自检：Bearer token（或 dev 回退头）有效则返回身份，否则 401（F020 §5.1）。"""
    rid = request_id(request)
    identity = current_identity(request)
    if identity is None:
        raise ApiError("unauthorized", "未认证或 token 无效/过期")
    return {"request_id": rid, "identity": identity}


def _login_session():
    """登录审计用独立会话（登录不依赖业务库状态；审计失败不阻断登录）。"""
    from contextlib import contextmanager
    from runtime.core.config import database_url
    from sqlalchemy import create_engine

    @contextmanager
    def _ctx():
        engine = create_engine(database_url(), pool_pre_ping=True)
        with Session(engine) as session:
            yield session
        engine.dispose()

    return _ctx()
