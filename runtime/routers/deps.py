# F020：API 路由公共依赖（request_id / RBAC / 数据库会话 / 认证）
# R024/F020 §5.1：身份来源 = Authorization: Bearer token（正式）；仅当
# AUTH_DEV_HEADERS=true（开发/测试）回退 X-Actor / X-Role 头。未认证一律
# anonymous（fail-closed）→ RBAC 拒绝 + 审计。
from __future__ import annotations

import logging
import uuid
from typing import Iterator

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from runtime.core import auth as auth_core
from runtime.core import rbac
from runtime.core.config import database_url
from runtime.core.errors import ApiError

logger = logging.getLogger("runtime.routers.deps")

_ROLE_HEADER = "x-role"


def request_id(request: Request) -> str:
    """请求级 request_id：优先透传 X-Request-Id，否则生成（F020 §2）。"""
    incoming = request.headers.get("x-request-id")
    if incoming:
        return incoming
    return f"r-{uuid.uuid4().hex[:12]}"


def db_session() -> Iterator[Session]:
    from sqlalchemy import create_engine

    engine = create_engine(database_url(), pool_pre_ping=True)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _bearer_or_headers(request: Request) -> dict[str, str] | None:
    """正式认证优先；开发回退（AUTH_DEV_HEADERS=true）读 X-Actor/X-Role 头。"""
    identity = auth_core.bearer_identity(request)
    if identity is not None:
        return identity
    if auth_core.dev_headers_enabled():
        role = request.headers.get("x-role")
        actor = request.headers.get("x-actor")
        try:
            role = rbac.normalize_role(role)
        except ValueError:
            role = None
        if role is None:
            return None
        return {"login": actor or role, "role": role, "name": actor or role}
    return None


def current_identity(request: Request) -> dict[str, str] | None:
    """当前请求身份 {login, role, name}；未认证返回 None（调用方按 anonymous 处理）。"""
    return _bearer_or_headers(request)


def current_actor(request: Request) -> str:
    identity = _bearer_or_headers(request)
    return identity["login"] if identity else auth_core.ANONYMOUS


def current_role(request: Request) -> str:
    """校验角色合法性；未知/未认证按 anonymous 处理（fail-closed，RBAC 拒绝）。"""
    identity = _bearer_or_headers(request)
    return identity["role"] if identity else auth_core.ANONYMOUS


def require_role(
    role: str,
    resource: str,
    action: str,
    *,
    session: Session | None = None,
    actor: str | None = None,
    object_ref: str | None = None,
) -> None:
    if not rbac.has_permission(role, resource, action):
        if session is not None:
            # Keep authorization failures auditable without exposing resource details.
            try:
                from runtime.db import api_service

                event = rbac.audit_denial(actor or role, resource, action)
                api_service.audit(
                    session,
                    actor=event["actor"],
                    action=event["action"],
                    basis=event["basis"],
                    outcome=event["outcome"],
                    object_ref=object_ref,
                )
                session.commit()
            except Exception:
                # Authorization must remain fail-closed even when the audit sink is unavailable.
                try:
                    session.rollback()
                except Exception:
                    pass
        raise ApiError("forbidden", f"角色 {role} 无 {resource}:{action} 权限")


# 供路由直接使用的依赖别名（保持 FastAPI Depends 语义清晰）
def get_request_id(request: Request) -> str:
    return request_id(request)


def get_db() -> Iterator[Session]:
    yield from db_session()


def get_role(request: Request) -> str:
    return current_role(request)


def get_actor(request: Request) -> str:
    return current_actor(request)
