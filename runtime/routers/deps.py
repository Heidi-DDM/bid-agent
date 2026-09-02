# F020：API 路由公共依赖（request_id / RBAC / 数据库会话）
from __future__ import annotations

import logging
import uuid
from typing import Iterator

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from runtime.core import rbac
from runtime.core.config import database_url
from runtime.core.errors import ApiError

logger = logging.getLogger("runtime.routers.deps")

# 简易身份：X-Actor / X-Role 头（开发期契约；正式鉴权接入 R024 认证时替换）
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


def current_actor(x_actor: str | None = Header(default=None)) -> str:
    return x_actor or "anonymous"


def current_role(x_role: str | None = Header(default=None)) -> str:
    """校验角色合法性；未知角色按 anonymous 处理（fail-closed，后续 RBAC 拒绝）。"""
    try:
        return rbac.normalize_role(x_role)
    except ValueError:
        return "anonymous"


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


def get_role(x_role: str | None = Header(default=None)) -> str:
    return current_role(x_role)


def get_actor(x_actor: str | None = Header(default=None)) -> str:
    return current_actor(x_actor)
