# F020 §5：四角色 RBAC（角色 -> 操作 -> 数据权限）
# 纯逻辑模块（无第三方依赖）：权限矩阵契约 F003 §6.2 + F020 §5。
# 规则：
# - 角色：投标专员 / 数据管理员 / 经营负责人 / 法务（F010 ROLES 对齐）；
# - 操作按资源/动作两级；每次拒绝访问必须写审计（越权审计，F020 §5）；
# - 数据权限：enterprise_data 明细不对投标专员/法务开放；审批层数据仅审批人可见。
from __future__ import annotations

from typing import Iterable

# 角色（F010 ROLES / F020 §5）
BID_SPECIALIST = "bid_specialist"   # 投标专员
DATA_ADMIN = "data_admin"           # 数据管理员
BUSINESS_HEAD = "business_head"     # 经营负责人
LEGAL = "legal"                     # 法务
ROLES = frozenset({BID_SPECIALIST, DATA_ADMIN, BUSINESS_HEAD, LEGAL})

# 操作资源命名空间（F020 分组）
RES_ANNOUNCEMENT = "announcement"    # 搜索/推送
RES_TENDER_DOC = "tender_document"   # 招标文件上传/解析
RES_MATERIAL = "material"            # 材料
RES_OCR = "ocr"                      # 解析/OCR
RES_ENTERPRISE = "enterprise"        # 企业资料
RES_MATCH = "match"                  # 匹配/准入
RES_RESULT = "result"                # 结果/队列
RES_APPROVAL = "approval"            # 审批/豁免/审计
RES_PREPARATION = "preparation"      # 投标准备立项（独立于正式审批）
RES_TASK = "task"                    # 分类型处置任务

# 动作：read / write / verify / approve / review
READ = "read"
WRITE = "write"
VERIFY = "verify"
APPROVE = "approve"
REVIEW = "review"

# 角色 -> 允许的 (资源, 动作)（F020 §5 角色表）
_ROLE_PERMISSIONS: dict[str, set[tuple[str, str]]] = {
    BID_SPECIALIST: {
        (RES_ANNOUNCEMENT, READ), (RES_ANNOUNCEMENT, WRITE),   # 搜索/查看公开事实
        (RES_TENDER_DOC, WRITE), (RES_TENDER_DOC, READ),       # 上传完整招标文件、查看解析
        (RES_MATERIAL, READ),                                  # 查看公开材料
        (RES_MATCH, READ), (RES_MATCH, WRITE), (RES_RESULT, READ),  # 复核后可发起受门禁重算
        (RES_TASK, READ), (RES_TASK, WRITE),                    # 规则/文件/身份等任务办理
    },
    DATA_ADMIN: {
        (RES_ENTERPRISE, READ), (RES_ENTERPRISE, WRITE),       # 导入/核验
        (RES_ENTERPRISE, VERIFY),                              # 核验
        (RES_OCR, READ), (RES_OCR, WRITE), (RES_OCR, REVIEW),  # OCR 复核
        (RES_MATERIAL, READ), (RES_MATERIAL, WRITE),
        (RES_TASK, READ), (RES_TASK, WRITE),                    # 资料补证任务办理
    },
    BUSINESS_HEAD: {
        (RES_ANNOUNCEMENT, READ), (RES_ANNOUNCEMENT, WRITE),   # 2026-09-11 用户决策：经营负责人可搜索/登记公告（原只读）
        (RES_TENDER_DOC, READ),
        (RES_MATERIAL, READ),
        (RES_TASK, READ), (RES_TASK, WRITE),                    # 任务协调/资源确认
        (RES_MATCH, READ), (RES_MATCH, WRITE),                 # 查看完整匹配结果/准入
        (RES_RESULT, READ),
        (RES_APPROVAL, READ), (RES_APPROVAL, WRITE), (RES_APPROVAL, APPROVE),  # 审批/驳回/豁免/审计
        (RES_PREPARATION, READ), (RES_PREPARATION, WRITE), (RES_PREPARATION, APPROVE),
    },
    LEGAL: {
        (RES_ANNOUNCEMENT, READ), (RES_TENDER_DOC, READ),      # 数据源与合规记录只读/审核
        (RES_MATERIAL, READ),
        (RES_TASK, READ), (RES_TASK, WRITE),                    # 法律/合规类人工任务
    },
}

# 数据权限：enterprise_data 明细仅数据管理员/经营负责人（匹配结果）可见（F003 §6.2）
_ENTERPRISE_DATA_READERS = frozenset({DATA_ADMIN, BUSINESS_HEAD})
# 审批层数据仅经营负责人可见
_APPROVAL_DATA_READERS = frozenset({BUSINESS_HEAD})

# 角色可读的 permission_scope（F003 §6.2 权限矩阵）
_SCOPE_READERS: dict[str, frozenset[str]] = {
    "public_read": frozenset(ROLES),
    "enterprise_read": _ENTERPRISE_DATA_READERS,
    "restricted": _ENTERPRISE_DATA_READERS,
    "approver_only": _APPROVAL_DATA_READERS,
}


def normalize_role(role: str | None) -> str:
    if not role or role not in ROLES:
        raise ValueError(f"未知角色: {role!r}，允许 {sorted(ROLES)}")
    return role


def has_permission(role: str, resource: str, action: str) -> bool:
    """角色是否允许 (资源, 动作)。未知角色一律拒绝（fail-closed）。"""
    if role not in ROLES:
        return False
    return (resource, action) in _ROLE_PERMISSIONS[role]


def can_read_scope(role: str, permission_scope: str) -> bool:
    """按 F003 §6.2 权限矩阵判断角色能否读取某 permission_scope 的数据。"""
    if role not in ROLES:
        return False
    readers = _SCOPE_READERS.get(permission_scope)
    if readers is None:
        return False
    return role in readers


def filter_visible_scopes(role: str, scopes: Iterable[str]) -> set[str]:
    """角色可见的 permission_scope 子集（列表过滤，防止企业明细泄露）。"""
    return {s for s in scopes if can_read_scope(role, s)}


def assert_permission(role: str, resource: str, action: str) -> None:
    """断言权限；不满足时抛 PermissionError（由路由层转 403 + 审计）。"""
    if not has_permission(role, resource, action):
        raise PermissionError(f"角色 {role} 无 {resource}:{action} 权限")


def audit_denial(actor: str, resource: str, action: str, reason: str = "") -> dict:
    """构造越权审计事件（路由层写库）：谁、何时、依据、结论。"""
    return {
        "actor": actor,
        "action": f"rbac.deny.{resource}.{action}",
        "basis": f"role 无权限",
        "outcome": f"denied:{reason}" if reason else "denied",
    }