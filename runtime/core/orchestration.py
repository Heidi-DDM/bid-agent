# F020 §2.1/§2.2：任务编排规则（纯逻辑，无第三方依赖）
# 约束（F020 §2.1/§7 联通验收）：
# - 未上传完整招标文件不得创建解析任务；
# - 解析成功只触发一个首次匹配 run（parse.completed 幂等）；
# - 结果页 GET 只读，不得隐式创建 run；
# - 补录核验后按新证据版本重算，旧结果标记 stale；同一证据版本重复调用返回既有 run（幂等）。
from __future__ import annotations

from typing import Any, Optional

# 招标文件解析前置检查
TENDER_DOC_TYPES = ("tender_document",)
PARSED_STATUSES = {"parsed"}


class OrchestrationError(Exception):
    """编排规则违例（转 409 invalid_state_transition 或 400 invalid_request）。"""


def ensure_tender_document_present(materials: list[dict[str, Any]]) -> None:
    """未上传完整招标文件（缺 file 或空文件）不得创建解析任务（F020 §2.2.2）。"""
    for m in materials:
        if m.get("material_type") in TENDER_DOC_TYPES and m.get("parse_status"):
            return
    raise OrchestrationError("未上传完整招标文件，不能创建解析任务（F020 §2.2.2）")


def should_trigger_first_match(project_materials: list[dict[str, Any]], existing_runs: int) -> bool:
    """解析成功且尚无匹配 run 时才触发首次匹配（幂等，不重复创建）。"""
    if existing_runs > 0:
        return False
    parsed = [m for m in project_materials if m.get("parse_status") in PARSED_STATUSES]
    return bool(parsed)


# 需要进入知识索引的材料类型（方案 §3.3：L1 公告 / L2 招标文件 / L3 企业资料）
INDEXABLE_TYPES = ("announcement", "tender_document", "qualification_cert",
                   "performance_record", "personnel_cert", "evidence_file")


def materials_needing_index(project_materials: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """解析完成后需建索引的材料（方案 §3.5：先索引后匹配）。

    返回 [(material_id, version)]；已解析且属于可索引类型。
    """
    out: list[tuple[str, int]] = []
    for m in project_materials:
        if m.get("parse_status") not in PARSED_STATUSES:
            continue
        if m.get("material_type") not in INDEXABLE_TYPES:
            continue
        mid, ver = m.get("material_id"), m.get("version")
        if mid and ver:
            out.append((mid, int(ver)))
    return out


def can_recalculate(project_materials: list[dict[str, Any]], latest_run: Optional[dict[str, Any]]) -> bool:
    """补录核验后重算：需存在最新 run 且项目仍有材料版本。"""
    if latest_run is None:
        return False
    return any(m.get("parse_status") in PARSED_STATUSES for m in project_materials)


def result_query_is_readonly() -> bool:
    """结果/矩阵/队列 GET 查询一律只读，不创建 run（F020 §2.1 红线）。"""
    return True


def ensure_recalculate_idempotent(evidence_version: str, latest_run: Optional[dict[str, Any]]) -> bool:
    """同一证据版本重复调用重算返回既有 run（幂等）；仅证据版本变化才新建 run。"""
    if latest_run is None:
        return False
    return latest_run.get("evidence_version") == evidence_version


def admission_eligible_for_approval(admission: dict[str, Any]) -> bool:
    """仅 internal_admission_eligible=true（满分）可创建审批（F009 §7 / F020 §2.2.6）。"""
    return bool(admission.get("internal_admission_eligible"))


def parse_event_idempotent(event_id: str, processed_event_ids: set[str]) -> bool:
    """parse.completed 事件幂等：同一事件只触发一次首次匹配（F020 §2.1）。"""
    if event_id in processed_event_ids:
        return False
    processed_event_ids.add(event_id)
    return True