# F020 §5：RBAC 权限矩阵纯逻辑测试（无第三方依赖）
# 覆盖：四角色允许/拒绝操作、数据权限（permission_scope）、fail-closed 未知角色、越权审计事件结构。
from __future__ import annotations

import pytest

from runtime.core import rbac


def test_all_roles_valid():
    assert rbac.ROLES == {"bid_specialist", "data_admin", "business_head", "legal"}


def test_bid_specialist_allowed_actions():
    assert rbac.has_permission("bid_specialist", "announcement", "write")
    assert rbac.has_permission("bid_specialist", "tender_document", "write")
    assert rbac.has_permission("bid_specialist", "result", "read")
    assert not rbac.has_permission("bid_specialist", "enterprise", "write")
    assert not rbac.has_permission("bid_specialist", "approval", "approve")
    assert not rbac.has_permission("bid_specialist", "approval", "read")


def test_data_admin_allowed_actions():
    assert rbac.has_permission("data_admin", "enterprise", "write")
    assert rbac.has_permission("data_admin", "enterprise", "verify")
    assert rbac.has_permission("data_admin", "ocr", "review")
    assert not rbac.has_permission("data_admin", "approval", "approve")
    assert not rbac.has_permission("data_admin", "announcement", "write")


def test_business_head_allowed_actions():
    assert rbac.has_permission("business_head", "approval", "approve")
    assert rbac.has_permission("business_head", "approval", "write")
    assert rbac.has_permission("business_head", "match", "read")
    assert not rbac.has_permission("business_head", "enterprise", "write")
    assert not rbac.has_permission("business_head", "tender_document", "write")


def test_legal_readonly():
    assert rbac.has_permission("legal", "announcement", "read")
    assert rbac.has_permission("legal", "material", "read")
    assert not rbac.has_permission("legal", "approval", "read")
    assert not rbac.has_permission("legal", "enterprise", "read")
    assert not rbac.has_permission("legal", "material", "write")


def test_unknown_role_fail_closed():
    assert not rbac.has_permission("hacker", "announcement", "read")
    assert not rbac.has_permission(None, "announcement", "read")
    with pytest.raises(ValueError):
        rbac.normalize_role("hacker")
    with pytest.raises(ValueError):
        rbac.normalize_role(None)


def test_permission_scope_matrix():
    # 公开原文/事实卡：全部角色可读（F003 §6.2）
    for role in rbac.ROLES:
        assert rbac.can_read_scope(role, "public_read"), role
    # 企业资质/业绩：仅数据管理员/经营负责人
    assert rbac.can_read_scope("data_admin", "enterprise_read")
    assert rbac.can_read_scope("business_head", "enterprise_read")
    assert not rbac.can_read_scope("bid_specialist", "enterprise_read")
    assert not rbac.can_read_scope("legal", "enterprise_read")
    # 审批/豁免/审计：仅经营负责人
    assert rbac.can_read_scope("business_head", "approver_only")
    assert not rbac.can_read_scope("data_admin", "approver_only")
    assert not rbac.can_read_scope("legal", "approver_only")


def test_filter_visible_scopes():
    scopes = ["public_read", "enterprise_read", "approver_only"]
    assert rbac.filter_visible_scopes("bid_specialist", scopes) == {"public_read"}
    assert rbac.filter_visible_scopes("business_head", scopes) == set(scopes)
    assert rbac.filter_visible_scopes("legal", scopes) == {"public_read"}


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