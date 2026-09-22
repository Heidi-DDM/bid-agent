# F020 §5 + F026 §5 / ADR-005：RBAC 权限矩阵纯逻辑测试（无第三方依赖）
# 覆盖：两级角色（投标专员=含其 Agent / 经营负责人）允许/拒绝操作、历史角色别名归并、
# 数据权限（permission_scope）、fail-closed 未知角色、越权审计事件结构。
from __future__ import annotations

import pytest

from runtime.core import rbac


def test_all_roles_valid():
    assert rbac.ROLES == {"bid_specialist", "business_head"}
    # 历史 data_admin/legal 只是兼容别名，不再是可配置业务角色
    assert rbac.LEGACY_ROLE_ALIASES == {"data_admin": "bid_specialist", "legal": "bid_specialist"}


def test_bid_specialist_allowed_actions():
    # F026 §5：投标专员（含其 Agent）负责搜索/简报/待选池、材料维护、解析复核、匹配与处置
    assert rbac.has_permission("bid_specialist", "announcement", "read")
    assert rbac.has_permission("bid_specialist", "announcement", "write")
    assert rbac.has_permission("bid_specialist", "tender_document", "read")
    assert rbac.has_permission("bid_specialist", "tender_document", "write")
    assert rbac.has_permission("bid_specialist", "material", "write")
    assert rbac.has_permission("bid_specialist", "enterprise", "write")
    assert rbac.has_permission("bid_specialist", "enterprise", "verify")
    assert rbac.has_permission("bid_specialist", "ocr", "review")
    assert rbac.has_permission("bid_specialist", "match", "write")
    assert rbac.has_permission("bid_specialist", "result", "read")
    assert not rbac.has_permission("bid_specialist", "approval", "read")
    assert not rbac.has_permission("bid_specialist", "approval", "approve")
    assert not rbac.has_permission("bid_specialist", "preparation", "approve")


def test_business_head_allowed_actions():
    # F026 §5：经营负责人拥有全部查看 + 规则管理 + 管理面板 + 最终审批/豁免
    assert rbac.has_permission("business_head", "approval", "approve")
    assert rbac.has_permission("business_head", "approval", "read")
    assert rbac.has_permission("business_head", "match", "write")
    assert rbac.has_permission("business_head", "enterprise", "write")
    assert rbac.has_permission("business_head", "enterprise", "verify")
    assert rbac.has_permission("business_head", "tender_document", "write")
    assert rbac.has_permission("business_head", "material", "write")
    assert rbac.has_permission("business_head", "preparation", "approve")
    assert not rbac.has_permission("business_head", "approval", "no_such_action")


def test_legacy_roles_normalize_to_bid_specialist():
    # 历史 data_admin / legal 登录角色归并为投标专员（兼容别名）
    assert rbac.normalize_role("data_admin") == rbac.BID_SPECIALIST
    assert rbac.normalize_role("legal") == rbac.BID_SPECIALIST
    assert rbac.normalize_role("bid_specialist") == rbac.BID_SPECIALIST
    # 别名的权限与投标专员完全一致
    assert rbac.has_permission("data_admin", "enterprise", "verify")
    assert rbac.has_permission("legal", "material", "write")
    assert not rbac.has_permission("data_admin", "approval", "approve")


def test_unknown_role_fail_closed():
    assert not rbac.has_permission("hacker", "announcement", "read")
    assert not rbac.has_permission(None, "announcement", "read")
    with pytest.raises(ValueError):
        rbac.normalize_role("hacker")
    with pytest.raises(ValueError):
        rbac.normalize_role(None)


def test_permission_scope_matrix():
    # 公开原文/事实卡：两级角色均可读（F003 §6.2）
    for role in rbac.ROLES:
        assert rbac.can_read_scope(role, "public_read"), role
    # 企业资质/业绩：投标专员（含其 Agent）与经营负责人（F026 §5 收编数据管理员职责）
    assert rbac.can_read_scope("bid_specialist", "enterprise_read")
    assert rbac.can_read_scope("business_head", "enterprise_read")
    # 审批/豁免/审计：仅经营负责人
    assert rbac.can_read_scope("business_head", "approver_only")
    assert not rbac.can_read_scope("bid_specialist", "approver_only")


def test_filter_visible_scopes():
    scopes = ["public_read", "enterprise_read", "approver_only"]
    assert rbac.filter_visible_scopes("bid_specialist", scopes) == {"public_read", "enterprise_read"}
    assert rbac.filter_visible_scopes("business_head", scopes) == set(scopes)


def test_assert_permission_raises():
    rbac.assert_permission("business_head", "approval", "approve")  # 不抛
    with pytest.raises(PermissionError):
        rbac.assert_permission("bid_specialist", "approval", "approve")


def test_audit_denial_structure():
    event = rbac.audit_denial("bid_specialist", "approval", "read")
    assert event["action"] == "rbac.deny.approval.read"
    assert event["outcome"] == "denied"
    assert event["actor"] == "bid_specialist"
    assert event["basis"]