# 集成测试防再发护栏（2026-09-22 事故后建立）
# 背景：真库集成测试的 fixture 会 TRUNCATE 业务表（projects/materials/match_runs/
# admission_results/approvals/audit_events 等）。2026-09-22 曾把 DATABASE_URL 指向
# 开发库 bid_agent 运行集成套件，清空了用户已建档的 PJ-* 项目与其材料/匹配/审批数据。
# 规则：只有库名含 "test"（隔离测试库）才默认放行；确需在开发库执行时必须显式设置
# ACK_DEV_DB_WIPE="我确认清空该库"（跳过护栏的责任由执行人自负）。
from __future__ import annotations

import os
from urllib.parse import urlparse

ACK_ENV = "ACK_DEV_DB_WIPE"
ACK_PHRASE = "我确认清空该库"


def integration_db_allowed() -> tuple[bool, str]:
    """(是否放行, 原因)。放行时原因为空串。"""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return False, "集成测试需要真实 PostgreSQL：请设置 DATABASE_URL 后执行"
    name = (urlparse(url).path or "").lstrip("/").strip().lower()
    if not name:
        return False, "DATABASE_URL 缺少数据库名，拒绝执行"
    if "test" in name:
        return True, ""
    if os.environ.get(ACK_ENV) == ACK_PHRASE:
        return True, ""
    return False, (
        f"集成测试 fixture 会 TRUNCATE 业务表，拒绝在疑似开发/生产库 {name!r} 上执行"
        f"（2026-09-22 曾因此清空开发库用户数据）。请改用隔离测试库"
        f"（如 bid_agent_test，库名含 test 自动放行），或设置 {ACK_ENV}='{ACK_PHRASE}' 显式确认。"
    )
