# F025 §2.4 / 方案 §3.4：权限与可见性过滤（纯逻辑，无第三方依赖）
# 规则：权限先于召回（fail-closed）；L1 可按公开权限跨项目检索；
# L2/L3 查询缺少 project_id/as_of 直接拒绝；L3 明细默认脱敏。
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from runtime.core import rbac
from runtime.rag.schemas import (
    L2_TENDER,
    L3_ENTERPRISE,
    KNOWLEDGE_LAYERS,
    PERMISSION_SCOPES,
)


class VisibilityError(Exception):
    """可见性校验失败（映射 403 / 422）。"""


@dataclass(frozen=True)
class Actor:
    """当前调用者（开发期契约：X-Role + X-Actor，R024 认证后替换）。"""

    role: str
    actor: str


@dataclass(frozen=True)
class Visibility:
    """可见性过滤条件（SQL 谓词输入，方案 §8.2）。"""

    layers: tuple[str, ...]
    scopes: tuple[str, ...]
    project_id: str | None = None
    lot_id: str | None = None
    as_of: str | None = None


def build_visibility_predicate(actor: Actor, request) -> Visibility:
    """按角色推导可见的 knowledge_layers 与 permission_scope。

    - 请求层列表与角色可见层求交集（越权层剔除，不为空才继续）；
    - 请求必须显式提供 knowledge_layers/permission_scope 上下文；
    - L2/L3 查询必须带 project_id + as_of（方案 §3.4）；
    - L3 明细（restricted/enterprise_read）仅 data_admin/business_head 可见；
      投标专员/法务请求 L3 直接 403（fail-closed）。
    """
    if not actor.role or actor.role not in rbac.ROLES:
        raise VisibilityError("未知角色，拒绝检索（fail-closed）")

    requested = set(request.knowledge_layers)
    # 角色可见层：公开数据全角色；企业证据层仅数据管理员/经营负责人
    if rbac.can_read_scope(actor.role, "enterprise_read"):
        visible_layers = set(KNOWLEDGE_LAYERS)
    else:
        visible_layers = {layer for layer in KNOWLEDGE_LAYERS if layer != L3_ENTERPRISE}
    layers = requested & visible_layers
    if not layers:
        raise VisibilityError(f"角色 {actor.role} 无权检索所请求的知识层（fail-closed）")

    # 角色可读的 permission_scope（F003 §6.2）
    scopes = set(rbac.filter_visible_scopes(actor.role, PERMISSION_SCOPES))

    requires_project = bool({L2_TENDER, L3_ENTERPRISE} & layers)
    if requires_project:
        if not request.project_id or not request.as_of:
            raise VisibilityError("L2/L3 检索必须提供 project_id 与 as_of（方案 §3.4）")

    return Visibility(
        layers=tuple(sorted(layers)),
        scopes=tuple(sorted(scopes)),
        project_id=request.project_id if requires_project else None,
        lot_id=request.lot_id if requires_project else None,
        as_of=request.as_of if requires_project else None,
    )


def scope_requires_masking(role: str) -> bool:
    """L3 明细脱敏：非数据管理员/经营负责人一律脱敏（方案 §8.2）。"""
    return not rbac.can_read_scope(role, "enterprise_read")


def mask_l3_text(text: str) -> str:
    """脱敏展示：仅保留结构骨架，明细替换为 [脱敏]（F025 §8.1）。"""
    return "[脱敏]（L3 明细仅数据管理员/经营负责人可见）"


def filter_by_permission(
    role: str,
    rows: Iterable[dict],
) -> list[dict]:
    """对候选行按角色过滤（防泄漏兜底；正常路径由 SQL 谓词先行过滤）。"""
    visible_scopes = rbac.filter_visible_scopes(role, (r["permission_scope"] for r in rows))
    return [r for r in rows if r["permission_scope"] in visible_scopes]