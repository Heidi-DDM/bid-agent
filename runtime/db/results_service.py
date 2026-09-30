# ADR-008 / docs/12 §4.2：结果域投影与写操作服务层
#
# 职责：
# - 读：result-overview / qualification-matrix / scoring-analysis / operation-plan /
#   parse-exceptions / candidate-plans / remediation-tasks 的服务端投影；
# - 写：解析例外结构化处置（代替浏览器 prompt）、候选班子选定、执行任务登记、
#   历史样本测试上下文登记；
# - 一致性：所有读取接受可选 run_id，默认仅取最新 current 运行并回显同一 run_context；
#   写操作引用非当前版本 → 409 stale_input（docs/12 §4.1）。
#
# 红线：本模块不推断满足、不生成报价、不自动投标；历史样本上下文来自服务端
# projects.test_context，不可由前端参数伪造或移除（docs/12 §7）。
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from runtime.core import rbac
from runtime.core.domain_dict import (
    ACTION_STATUSES,
    BLOCK_PARSE_EXCEPTIONS,
    HISTORICAL_SAMPLE_BANNER,
    HISTORICAL_SAMPLE_NOTE,
    PARSE_EXC_DEEP_REVIEW,
    PARSE_EXC_RESOLVED,
    PARSE_EXCEPTION_RISK_LABEL,
    REASON_CODE_LABEL,
    RESULT_LABEL,
    ROLE_CODE_LABEL,
    SCORE_CLASSIFICATION_LABEL,
    STAGES,
    STAGE_LABEL,
    SUMMARY_ORDER,
    TASK_KIND_LABEL,
    project_result_state,
    next_action_for,
)
from runtime.core.errors import ApiError
from runtime.db.models import (
    AdmissionResult,
    CandidatePlan,
    CandidatePlanMember,
    CandidatePlanRequirementLink,
    MatchItem,
    MatchRun,
    OperationTask,
    ParseException,
    Project,
    RemediationTask,
    Requirement,
)

_READABLE = {rbac.BID_SPECIALIST, rbac.BUSINESS_HEAD}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(session: Session, *, actor: str, action: str, outcome: str | None = None,
           basis: str | None = None, object_ref: str | None = None) -> None:
    from runtime.db.api_service import audit
    audit(session, actor=actor, action=action, outcome=outcome, basis=basis, object_ref=object_ref)


def _project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise ApiError("not_found", f"项目不存在: {project_id}")
    return project


def _require_readable(role: str) -> None:
    if role not in _READABLE:
        raise ApiError("forbidden", "当前角色无权查看项目结果")


# ---------------------------------------------------------------------------
# 运行选择与 run_context（docs/12 §3.5：所有投影接口回显同一 run_context）
# ---------------------------------------------------------------------------

def _tender_source_material(session: Session, project_id: str):
    """返回项目当前版本的招标文件材料；原文始终只读，不在结果层改写。"""
    from runtime.db.models import Material

    return session.scalar(
        select(Material).where(Material.project_id == project_id,
                               Material.material_type == "tender_document")
        .order_by(Material.version.desc()).limit(1))


def _tender_source_link(session: Session, project_id: str) -> tuple[str | None, int | None]:
    """项目当前招标材料的原文链接（F021 §2.1 v1.5 契约；前端 Bearer 拉 blob + #page=N 定位）。

    取项目最新版本的 tender_document 材料；无材料时返回 (None, None)，前端不渲染链接。
    """
    material = _tender_source_material(session, project_id)
    if material is None:
        return None, None
    return f"/api/v1/materials/{material.material_id}/file?version={material.version}", material.version


def _verified_submission_checklist(session: Session, project_id: str,
                                    source_link: str | None) -> dict[str, Any] | None:
    """返回人工复核的投标文件/附件清单，严格绑定当前原始材料版本。

    这是“招标文件明确材料”的只读展示层，故意不写入 Requirement / MatchRun：它不改变
    企业资格匹配、缺证队列或内部准入。这样既能展示历史解析遗漏的材料，也不会为了
    显示完整而篡改历史 RuleSet 或以递交前材料误判企业资格。
    """
    material = _tender_source_material(session, project_id)
    if material is None:
        return None
    from runtime.config.verified_tender_checklists import verified_submission_checklist

    checklist = verified_submission_checklist(project_id, material.material_id, material.version)
    if checklist is None:
        return None
    for row in checklist["items"]:
        row["source_link"] = source_link
    checklist["total"] = len(checklist["items"])
    checklist["material_id"] = material.material_id
    checklist["material_version"] = material.version
    return checklist


def resolve_run(session: Session, project_id: str, run_id: str | None = None) -> MatchRun | None:
    """默认取最新 current 运行；显式 run_id 可读历史运行（is_current=false 如实回显）。"""
    if run_id:
        run = session.get(MatchRun, run_id)
        if run is None or run.project_id != project_id:
            raise ApiError("not_found", f"匹配运行不存在: {run_id}")
        return run
    return session.scalar(
        select(MatchRun).where(MatchRun.project_id == project_id, MatchRun.is_current.is_(True))
        .order_by(MatchRun.created_at.desc()).limit(1)
    )


def _run_context_payload(session: Session, run: MatchRun | None, *, project: Project) -> dict[str, Any]:
    """run_context + 新鲜度/历史样本标识（docs/12 §2.2-1 顶部固定显示内容）。"""
    test_ctx = project.test_context or {}
    payload: dict[str, Any] = {
        "run_context": (run.run_context if run is not None else None) or {},
        "is_current": bool(run.is_current) if run is not None else None,
        "as_of": run.as_of if run is not None else None,
        "generated_at": run.created_at.isoformat() if run is not None and run.created_at else None,
        "result_freshness": None,
        "historical_sample": bool(test_ctx.get("historical_sample")),
        "historical_sample_banner": HISTORICAL_SAMPLE_BANNER if test_ctx.get("historical_sample") else None,
        "historical_sample_note": HISTORICAL_SAMPLE_NOTE if test_ctx.get("historical_sample") else None,
    }
    if run is not None:
        admission = _admission_of_run(session, run)
        payload["result_freshness"] = admission.result_freshness if admission else None
    return payload


def _admission_of_run(session: Session, run: MatchRun | None) -> AdmissionResult | None:
    if run is None:
        return None
    return session.scalar(select(AdmissionResult).where(AdmissionResult.run_id == run.run_id).limit(1))


def _context_and_admission(session: Session, project_id: str, run_id: str | None = None):
    project = _project(session, project_id)
    run = resolve_run(session, project_id, run_id)
    if run_id and run is None:
        raise ApiError("not_found", f"匹配运行不存在: {run_id}")
    admission = _admission_of_run(session, run)
    return project, run, admission, _run_context_payload(session, run, project=project)


# ---------------------------------------------------------------------------
# 结果首屏（docs/12 §2.2）
# ---------------------------------------------------------------------------

def _projected(item: MatchItem | None, req: Requirement | None) -> dict[str, Any]:
    req_type = req.req_type if req is not None else None
    match_result = item.match_result if item is not None else None
    reason_code = item.reason_code if item is not None else None
    if match_result is None:
        projected = "not_evaluated"
    else:
        projected = project_result_state(match_result, reason_code, req_type)
    return projected



def _is_submission_package_req(req: Requirement) -> bool:
    """识别附件中的人员资料包，而不是把其中出现的岗位词误当新增资格岗。

    历史解析有时截掉了“项目管理机构”标题，只保留“技术负责人、合同商务负责人、
    专职安全生产管理人员……应附注册资格证书、身份证、职称证、养老保险……”。
    因此必须按附件资料包的组合措辞识别，不能只依赖标题是否被完整抽取。
    """
    text = req.assertion or ""
    has_attachment_markers = "应附" in text and ("扫描件" in text or "原件" in text)
    has_person_package_markers = sum(token in text for token in ("身份证", "职称证", "养老保险", "注册资格证书")) >= 2
    has_package_context = "项目管理机构" in text or any(token in text for token in ("技术负责人", "合同商务负责人", "岗位人员"))
    return has_attachment_markers and has_person_package_markers and has_package_context


def _submission_metadata(req: Requirement) -> dict[str, Any]:
    """把解析出的“是否随标提交”与企业内部证据严格分开。

    这里的材料口径只来自本项目招标文件已解析的文字；无法从原文确认的，明确标为
    “未定位/待核对”，绝不把企业台账字段自动升级成投标文件材料。
    """
    text = req.assertion or ""
    evidence = list(req.evidence_required or [])
    rule = req.rule if isinstance(req.rule, dict) else {}
    materials: list[str] = []
    required = False
    stage = "资格匹配/准备阶段"
    note = "企业证据字段，不等同于本阶段必须随标提交的材料。"
    if _is_submission_package_req(req):
        required = True
        stage = "投标文件递交时（附件项目管理机构）"
        materials = ["项目管理机构人员资料包：注册资格证书、身份证、职称证、养老保险缴纳证明等原件扫描件，并按文件要求加盖电子印章"]
        note = "这是随投标文件提交的附件资料包清单，不单独产生技术负责人、合同商务负责人或安全员的新增资格结论。"
    elif "保证金" in text or "保函" in text or "bid_bond" in evidence:
        required = True
        stage = "投标文件递交前"
        materials = ["投标保证金缴纳证明资料（按招标文件允许形式之一：银行转账记录、银行保函、投标保险保单、保证金联保证明、投标承诺书或其他）"]
        note = "判定是否准备投标时不要求已有到账凭证；实际递交前按招标文件完成并留存回执。"
    elif "失信被执行人" in text or "信用中国" in text or "信用" in text and "查询" in text:
        required = False
        stage = "评审/资格审查时查询"
        materials = []
        note = "招标文件写明由评委/平台网上查询；未定位到要求投标人另行提交信用查询证明或截图。"
    elif "项目业主同意更换" in text:
        required = True
        stage = "投标文件递交时（仅项目经理发生在建更换时）"
        materials = ["项目业主同意项目经理更换的证明"]
        note = "条件性随标材料：仅在拟派项目经理属于在建项目办理更换的情形提供。"
    elif "企业主要负责人" in text and "安全生产考核合格证书" in text:
        required = True
        stage = "资格审查/投标文件递交时"
        materials = ["法定代表人、企业经理、企业分管安全生产副经理的安全生产考核合格证书（A证）扫描件"]
        note = "这是招标文件实质性响应资料；是否具备仍以已核验企业资料为准。"
    elif "安全生产考核合格证书" in text and "专职安全生产" not in text and ("项目经理" in text or "manager_profile" in evidence):
        required = True
        stage = "资格审查/投标文件递交时"
        materials = ["拟派项目经理安全生产考核合格证书（B证）扫描件"]
        note = "与项目经理注册资格共同用于同一人综合核验，不能跨人拼接。"
    elif ("市政公用工程施工总承包" in text or "建设行政主管部门核发" in text) and "资质" not in text:
        required = True
        stage = "资格审查/投标文件递交时"
        materials = ["建设行政主管部门核发的企业资质证书扫描件"]
        note = "资格事实与实质性响应材料分别记录。"
    elif "授权委托" in text or "法定代表人" in text and "授权" in text:
        required = True
        stage = "投标文件递交时"
        materials = ["法定代表人身份证明/授权委托书（按招标文件格式）"]
        note = "仅在招标文件该条款明确要求时列入递交材料。"
    elif "营业执照" in text:
        required = True
        stage = "资格审查/投标文件递交时"
        materials = ["营业执照扫描件/复印件（按文件要求加盖电子印章）"]
        note = "资格事实与随标呈现材料分别记录。"
    elif "安全生产许可证" in text and "具备" in text:
        required = True
        stage = "资格审查/投标文件递交时"
        materials = ["企业安全生产许可证扫描件（按文件要求加盖电子印章）"]
        note = "资格事实与随标呈现材料分别记录。"
    elif "专职安全生产管理人员" in text and ("证" in text or "配备" in text):
        required = True
        stage = "资格审查/投标文件递交时"
        materials = ["专职安全生产管理人员有效安全生产考核合格证书（C证）及人员信息"]
        note = "台账没有记录只能说明企业资料待补/待核实，不能推断企业没有人员。"
    elif "项目经理" in text and ("注册" in text or "安全生产考核" in text):
        required = True
        stage = "资格审查/投标文件递交时"
        materials = ["拟派项目经理注册建造师证书及安全生产考核合格证书（B证）扫描件"]
        note = "人员资格按同一人综合方案核验，不能跨人拼接。"
    elif req.req_type == "action_requirement":
        required = False
        stage = rule.get("stage") or req.required_by_stage or "投标执行阶段"
        note = "这是执行节点，不是企业资质缺口；完成后按责任人回执登记。"
    elif evidence:
        note = "招标文件当前条款未明确列出需随标提交的具体材料；仅作为企业证据核验项。"
    return {"required": required, "stage": stage, "materials": materials, "note": note,
            "evidence_keys": evidence, "basis": "招标文件原文/附件解析；未明确项不推断"}


def _requirement_overview_row(req: Requirement, item: MatchItem | None,
                             source_link: str | None) -> dict[str, Any]:
    rule = req.rule if isinstance(req.rule, dict) else {}
    if _is_submission_package_req(req):
        state = "not_applicable"
    elif req.req_type == "action_requirement":
        raw = item.match_result if item else (req.action_status or "not_started")
        state = raw if raw in ACTION_STATUSES else "not_started"
    else:
        state = _projected(item, req)
        state = {"parse_exception": "manual_review", "not_calculable": "manual_review",
                 "legacy_ambiguous": "manual_review", "not_evaluated": "manual_review"}.get(state, state)
    meta = _submission_metadata(req)
    return {
        "requirement_id": req.requirement_id, "req_type": req.req_type,
        "domain": req.domain or ({"hard_requirement": "qualification", "scored_requirement": "scoring",
                                   "action_requirement": "operation"}.get(req.req_type)),
        "category": req.category, "clause_ref": req.clause_ref, "page_no": rule.get("page_no"),
        "assertion": req.assertion, "lot_id": req.lot_id,
        "state": state, "state_label": RESULT_LABEL.get(state, state),
        "decision_scope": ("information_only" if _is_submission_package_req(req)
                           else (req.decision_scope or ("admission" if req.req_type != "action_requirement" else "information_only"))),
        "evidence_required": req.evidence_required or [],
        "submission": meta, "source_link": source_link,
        "reason_code": item.reason_code if item else None,
        "reason_code_label": REASON_CODE_LABEL.get(item.reason_code or "", None) if item else None,
        "reason": (item.match_reason or {}).get("text") if item and item.match_reason else None,
        "required_value": item.required_value if item else None,
        "observed_value": item.observed_value if item else None,
        "evidence_state": item.evidence_state if item else None,
        "evidence_refs": item.evidence_refs or [] if item else [],
        "next_action": (item.next_action if item else None) or next_action_for(item.reason_code if item else None),
        "gate_executed": bool((item.match_reason or {}).get("gate_executed", True)) if item and item.match_reason else None,
    }


def result_overview(session: Session, *, project_id: str, run_id: str | None = None,
                    role: str, actor: str) -> dict[str, Any]:
    """结果首屏的单一读取源（docs/12 §4.2）：run_context、资格摘要、解析质量摘要、优先事项。"""
    _require_readable(role)
    project, run, admission, context = _context_and_admission(session, project_id, run_id)
    source_link, _src_ver = _tender_source_link(session, project_id)
    submission_checklist = _verified_submission_checklist(session, project_id, source_link)

    reqs = {r.requirement_id: r for r in session.scalars(
        select(Requirement).where(Requirement.rule_set_id == (run.rule_set_id if run else "")))} if run else {}
    items = {i.requirement_id: i for i in session.scalars(
        select(MatchItem).where(MatchItem.run_id == run.run_id))} if run else {}

    hard_items = []
    for rid, req in reqs.items():
        if req.req_type != "hard_requirement" or _is_submission_package_req(req):
            continue
        scope = req.decision_scope or "admission"
        if scope != "admission":  # information_only 不进入内部准入（docs/12 §3.1）
            continue
        item = items.get(rid)
        hard_items.append((rid, req, item))

    counts = {state: 0 for state in SUMMARY_ORDER}
    priority: list[dict[str, Any]] = []
    for rid, req, item in hard_items:
        projected = _projected(item, req)
        state = {"satisfied": "satisfied", "not_satisfied": "not_satisfied",
                 "blocked_missing_data": "blocked_missing_data",
                 "manual_review": "manual_review", "parse_exception": "manual_review",
                 "not_calculable": "manual_review", "legacy_ambiguous": "manual_review",
                 "not_evaluated": "manual_review"}.get(projected, "manual_review")
        if state in counts:
            counts[state] += 1
        priority.append({
            "requirement_id": rid,
            "clause_ref": req.clause_ref,
            "assertion": req.assertion,
            "state": state,
            "state_label": RESULT_LABEL.get(state, state),
            "reason_code": item.reason_code if item else None,
            "reason_code_label": REASON_CODE_LABEL.get(item.reason_code or "", None) if item else None,
            "reason": (item.match_reason or {}).get("text") if item and item.match_reason else None,
            "next_action": (item.next_action if item else None) or next_action_for(item.reason_code if item else None),
            "observed_value": item.observed_value if item else None,
            "required_value": item.required_value if item else None,
            "person_id": item.person_id if item else None,
            "candidate_plan_id": item.candidate_plan_id if item else None,
            "page_no": (reqs[rid].rule or {}).get("page_no") if reqs.get(rid) else None,
            "source_link": source_link,
        })
    order_index = {s: i for i, s in enumerate(SUMMARY_ORDER)}
    priority.sort(key=lambda p: order_index.get(p["state"], 99))
    top3, rest = priority[:3], priority[3:]
    # 已满足清单（2026-09-29 用户裁定：满足项必须逐条可见、带证据与原文回链，
    # 不得"满足了就消失"被误认为漏项）
    satisfied_items = [p for p in priority if p["state"] == "satisfied"]

    # 解析质量摘要（docs/12 §2.2-3：次级提示，不抢占首屏；阻断原因独立）
    pe_snapshot = _current_parse_exception_snapshot(session, project_id)
    open_exceptions = [p for p in pe_snapshot if p.status in ("open", "deep_review")]
    blocking_exceptions = [p for p in open_exceptions if p.exception_type in ("source_conflict", "anchor_not_located")]

    operation_summary = (admission.operation_summary if admission else {}) or {}
    scoring_summary = (admission.scoring_summary if admission else {}) or {}
    # 全量清单：资格、评分、执行三域统一展示；满足项也保留，避免“满足即消失”。
    all_requirements = [_requirement_overview_row(req, items.get(rid), source_link)
                        for rid, req in reqs.items()]
    domain_order = {"qualification": 0, "scoring": 1, "operation": 2}
    all_requirements.sort(key=lambda x: (domain_order.get(x.get("domain"), 9),
                                         x.get("page_no") is None, x.get("page_no") or 0,
                                         x.get("requirement_id") or ""))
    full_counts = {"qualification": sum(1 for x in all_requirements if x["domain"] == "qualification"),
                   "scoring": sum(1 for x in all_requirements if x["domain"] == "scoring"),
                   "operation": sum(1 for x in all_requirements if x["domain"] == "operation")}

    return {
        "project_id": project_id,
        **context,
        "qualification_summary": {
            **counts,
            "total": len(hard_items),
            "actionable_order": list(SUMMARY_ORDER),
        },
        "priority_items": top3,
        "satisfied_items": satisfied_items,
        "more_count": len(rest),
        "parse_quality_summary": {
            "pending_count": len(open_exceptions),
            "blocking_count": len(blocking_exceptions),
            "note": ("满分准入暂不可用：存在影响准入的硬性解析例外（解析质量问题，不是企业缺证）"
                     if blocking_exceptions else
                     ("解析质量：n 项待处置（处置完毕后自动重算）" if open_exceptions else "解析质量无待处置项")),
            "admission_blocked_by": BLOCK_PARSE_EXCEPTIONS if blocking_exceptions else None,
        },
        "scoring_summary": scoring_summary,
        "operation_summary": operation_summary,
        "requirements_overview": all_requirements,
        "requirements_overview_summary": {"total": len(all_requirements), "by_domain": full_counts,
                                          "note": "每条均回链招标文件原文；“随标提交”只在原文明确时标记。"},
        # 与 RuleSet/匹配结果分开：这是人工通读后补充的投标文件组成、格式和附件材料清单。
        "document_submission_checklist": submission_checklist,
        "admission": {
            "eligible": admission.internal_admission_eligible if admission else None,
            "state": admission.internal_admission_result.get("status") if admission else None,
            "decision": admission.internal_admission_result.get("decision") if admission else None,
            "blocking_reasons": admission.blocking_reasons or [] if admission else [],
        },
    }


# ---------------------------------------------------------------------------
# 资格矩阵（docs/12 §2.2-4：结论标签、原因码、事实值、证据状态、人员方案、下一步）
# ---------------------------------------------------------------------------

def qualification_matrix(session: Session, *, project_id: str, run_id: str | None = None,
                         lot_id: str | None = None, state: str | None = None,
                         role: str, actor: str) -> dict[str, Any]:
    _require_readable(role)
    project, run, admission, context = _context_and_admission(session, project_id, run_id)
    if run is None:
        return {"project_id": project_id, **context, "items": [], "total": 0}
    source_link, _src_ver = _tender_source_link(session, project_id)
    reqs = session.scalars(select(Requirement).where(Requirement.rule_set_id == run.rule_set_id)).all()
    items = {i.requirement_id: i for i in session.scalars(
        select(MatchItem).where(MatchItem.run_id == run.run_id))}
    out = []
    for req in reqs:
        if req.req_type != "hard_requirement" or _is_submission_package_req(req):
            continue
        if lot_id and req.lot_id not in (None, lot_id):
            continue
        item = items.get(req.requirement_id)
        projected = _projected(item, req)
        state_key = {"parse_exception": "manual_review", "not_calculable": "manual_review",
                     "legacy_ambiguous": "manual_review", "not_evaluated": "manual_review"}.get(projected, projected)
        if state and state_key != state:
            continue
        rule = req.rule or {}
        out.append({
            "requirement_id": req.requirement_id,
            "clause_ref": req.clause_ref,
            "assertion": req.assertion,
            "category": req.category,
            "lot_id": req.lot_id,
            "page_no": rule.get("page_no"),
            "source_link": source_link,  # 原文追溯：点击直达原文对应页（docs/12 用户验收）
            "domain": "qualification",
            "decision_scope": req.decision_scope or "admission",
            "match_result": item.match_result if item else None,
            "projected_result": projected,
            "state": state_key,
            "state_label": RESULT_LABEL.get(state_key, state_key),
            "reason_code": item.reason_code if item else None,
            "reason_code_label": REASON_CODE_LABEL.get(item.reason_code or "", None) if item else None,
            "reason": (item.match_reason or {}).get("text") if item and item.match_reason else None,
            "observed_value": item.observed_value if item else None,
            "required_value": item.required_value if item else None,
            "evidence_state": item.evidence_state if item else None,
            "evidence_refs": (item.evidence_refs or []) if item else [],
            "submission": _submission_metadata(req),
            "evidence_required": req.evidence_required or [],
            "person_id": item.person_id if item else None,
            "candidate_plan_id": item.candidate_plan_id if item else None,
            "next_action": (item.next_action if item else None) or next_action_for(item.reason_code if item else None),
            "gate_executed": bool((item.match_reason or {}).get("gate_executed", True)) if item and item.match_reason else True,
        })
    order_index = {s: i for i, s in enumerate(SUMMARY_ORDER)}
    out.sort(key=lambda x: (order_index.get(x["state"], 99), x["requirement_id"] or ""))
    return {"project_id": project_id, **context, "items": out, "total": len(out)}


# ---------------------------------------------------------------------------
# 评分分析（docs/12 §2.3：五类分组；无可算项不输出 0/0）
# ---------------------------------------------------------------------------

def scoring_analysis(session: Session, *, project_id: str, run_id: str | None = None,
                     role: str, actor: str) -> dict[str, Any]:
    _require_readable(role)
    project, run, admission, context = _context_and_admission(session, project_id, run_id)
    if run is None:
        return {"project_id": project_id, **context, "groups": [],
                "summary": {"calculable": False, "objective_count": 0, "subjective_count": 0,
                            "unstructured_count": 0}}
    source_link, _src_ver = _tender_source_link(session, project_id)
    reqs = session.scalars(select(Requirement).where(Requirement.rule_set_id == run.rule_set_id)).all()
    items = {i.requirement_id: i for i in session.scalars(
        select(MatchItem).where(MatchItem.run_id == run.run_id))}
    groups: dict[str, list[dict[str, Any]]] = {}
    for req in reqs:
        if req.req_type != "scored_requirement":
            continue
        item = items.get(req.requirement_id)
        classification = (item.score_classification if item and item.score_classification else None) \
            or req.score_classification or "unstructured"
        projected = _projected(item, req)
        groups.setdefault(classification, []).append({
            "requirement_id": req.requirement_id,
            "clause_ref": req.clause_ref,
            "assertion": req.assertion,
            "score": item.score if item else None,
            "max_score": (item.max_score if item and item.max_score is not None else req.max_score),
            "match_result": item.match_result if item else None,
            "projected_result": projected,
            "state_label": RESULT_LABEL.get(projected, projected),
            "reason_code": item.reason_code if item else None,
            "reason": (item.match_reason or {}).get("text") if item and item.match_reason else None,
            "formula": req.score_formula,
            "page_no": (req.rule or {}).get("page_no"),
            "source_link": source_link,
        })
    summary = {
        "objective_count": len(groups.get("objective_calculable", [])),
        "subjective_count": len(groups.get("subjective_review", [])),
        "price_formula_count": len(groups.get("price_formula", [])),
        "methodology_count": len(groups.get("methodology_only", [])),
        "unstructured_count": len(groups.get("unstructured", [])),
        "calculable": bool(groups.get("objective_calculable")),
        "objective_score": (admission.scoring_result or {}).get("objective_score") if admission else None,
        "objective_max": (admission.scoring_result or {}).get("objective_max") if admission else None,
        "internal_full_score_ready": (admission.scoring_result or {}).get("internal_full_score_ready") if admission else None,
    }
    if not summary["calculable"]:
        summary["note"] = ("当前无可自动计算评分项，不能自动判断是否达到内部满分"
                           "（docs/12 §2.3：不显示 0/0 或「未满分」）")
    return {
        "project_id": project_id,
        **context,
        "summary": summary,
        "groups": [
            {"classification": key, "label": SCORE_CLASSIFICATION_LABEL.get(key, key), "items": groups[key]}
            for key in ("objective_calculable", "subjective_review", "price_formula",
                        "methodology_only", "unstructured")
            if key in groups
        ],
    }


# ---------------------------------------------------------------------------
# 投标执行计划（docs/12 §2.4：阶段时间线、责任人、截止、回执）
# ---------------------------------------------------------------------------

def operation_plan(session: Session, *, project_id: str, run_id: str | None = None,
                   stage: str | None = None, role: str, actor: str) -> dict[str, Any]:
    _require_readable(role)
    project, run, admission, context = _context_and_admission(session, project_id, run_id)
    tasks = session.scalars(select(OperationTask).where(OperationTask.project_id == project_id)
                            .order_by(OperationTask.required_by_stage, OperationTask.deadline_at)).all()
    stages_payload: list[dict[str, Any]] = []
    for stage_key in STAGES:
        if stage and stage_key != stage:
            continue
        rows = [t for t in tasks if (t.required_by_stage or "approval_ready") == stage_key]
        if not rows:
            continue
        stages_payload.append({
            "stage": stage_key,
            "stage_label": STAGE_LABEL.get(stage_key, stage_key),
            "gates_approval": stage_key == "approval_ready",
            "tasks": [_operation_task_payload(t) for t in rows],
            "ready": all(t.status in ("ready", "completed") for t in rows),
        })
    return {
        "project_id": project_id,
        **context,
        "stages": stages_payload,
        "approval_actions_ready": all(
            s["ready"] for s in stages_payload if s["gates_approval"]) if stages_payload else True,
    }


def _operation_task_payload(t: OperationTask) -> dict[str, Any]:
    return {
        "operation_task_id": t.operation_task_id,
        "action_requirement_id": t.action_requirement_id,
        "task_kind": t.task_kind,
        "task_kind_label": TASK_KIND_LABEL.get(t.task_kind, t.task_kind),
        "title": t.title,
        "clause_ref": t.clause_ref,
        "deadline_at": t.deadline_at.isoformat() if t.deadline_at else None,
        "owner_role": t.owner_role,
        "owner": t.owner,
        "status": t.status,
        "status_label": RESULT_LABEL.get(t.status, t.status),
        "receipt_evidence_refs": t.receipt_evidence_refs or [],
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "completed_by": t.completed_by,
        "version": t.version,
        "run_id": t.run_id,
    }


def update_operation_task(session: Session, *, operation_task_id: str, status: str,
                          receipt_evidence_refs: list[str] | None = None, version: int | None = None,
                          owner: str | None = None, actor: str, role: str) -> dict[str, Any]:
    """执行任务登记（docs/12 §4.2：仅更新动作计划，按阶段验证；回执必填约束）。"""
    if role not in _READABLE:
        raise ApiError("forbidden", "当前角色无权办理执行任务")
    task = session.get(OperationTask, operation_task_id)
    if task is None:
        raise ApiError("not_found", f"执行任务不存在: {operation_task_id}")
    if status not in ACTION_STATUSES:
        raise ApiError("invalid_request", f"不支持的动作状态: {status}")
    if version is not None and version != task.version:
        raise ApiError("stale_input", "任务版本已变化，请刷新后重试（docs/12 §4.1）")
    refs = [str(r).strip() for r in (receipt_evidence_refs or []) if str(r).strip()]
    if status == "completed" and not refs:
        raise ApiError("invalid_request", "完成执行动作必须提交可核验回执（docs/12 §2.4）")
    before = task.status
    task.status = status
    if refs:
        task.receipt_evidence_refs = sorted(set((task.receipt_evidence_refs or []) + refs))
    if owner:
        task.owner = owner
    if status == "completed":
        task.completed_at = _now()
        task.completed_by = actor
    task.version = (task.version or 1) + 1
    # 同步回写 Requirement.action_status（下一次匹配按登记状态判定，双源一致）
    req = session.scalar(select(Requirement).where(
        Requirement.requirement_id == task.action_requirement_id).limit(1))
    if req is not None:
        req.action_status = status
    _audit(session, actor=actor, action="operation_task.updated",
           basis=f"task={operation_task_id} {before}->{status} receipts={len(refs)}",
           outcome=status, object_ref=task.project_id)
    session.commit()
    return _operation_task_payload(task)


# ---------------------------------------------------------------------------
# 解析质量队列（docs/12 §2.5：按风险排序 + 结构化处置）
# ---------------------------------------------------------------------------

def _current_parse_exception_snapshot(session: Session, project_id: str) -> list[ParseException]:
    """项目最新快照版本的解析例外行（含处置历史；历史版本可经 parse_version 查询）。"""
    latest_version = session.scalar(
        select(ParseException.snapshot_version).where(ParseException.project_id == project_id)
        .order_by(ParseException.created_at.desc()).limit(1))
    if latest_version is None:
        return []
    return list(session.scalars(select(ParseException).where(
        ParseException.project_id == project_id,
        ParseException.snapshot_version == latest_version,
    ).order_by(ParseException.risk_rank, ParseException.created_at)))


def parse_exceptions_view(session: Session, *, project_id: str, parse_version: str | None = None,
                          status: str | None = None, role: str, actor: str) -> dict[str, Any]:
    _require_readable(role)
    _project(session, project_id)
    if parse_version:
        rows = list(session.scalars(select(ParseException).where(
            ParseException.project_id == project_id,
            ParseException.snapshot_version == parse_version,
        ).order_by(ParseException.risk_rank, ParseException.created_at)))
    else:
        rows = _current_parse_exception_snapshot(session, project_id)
    if status:
        rows = [r for r in rows if r.status == status]
    return {
        "project_id": project_id,
        "snapshot_version": rows[0].snapshot_version if rows else None,
        "total": len(rows),
        "pending_count": len([r for r in rows if r.status in ("open", "deep_review")]),
        "items": [_parse_exception_payload(r) for r in rows],
    }


def _parse_exception_payload(r: ParseException) -> dict[str, Any]:
    return {
        "parse_exception_id": r.parse_exception_id,
        "source_link": (f"/api/v1/materials/{r.material_id}/file?version={r.material_version}"
                        if r.material_id else None),
        "candidate_id": r.candidate_id,
        "material_id": r.material_id,
        "material_version": r.material_version,
        "snapshot_version": r.snapshot_version,
        "exception_type": r.exception_type,
        "risk_rank": r.risk_rank,
        "risk_label": PARSE_EXCEPTION_RISK_LABEL.get(r.exception_type, r.exception_type),
        "req_type": r.req_type,
        "title": r.title,
        "payload": {
            "anchor_key": (r.payload or {}).get("anchor_key"),
            "page_no": (r.payload or {}).get("page_no"),
            "confidence": (r.payload or {}).get("confidence"),
            "missing_marker": (r.payload or {}).get("missing_marker"),
            "clause_ref": (r.payload or {}).get("clause_ref"),
            "locate_hints": (r.payload or {}).get("locate_hints"),
        } if r.payload else None,
        "status": r.status,
        "decision": r.decision,
        "decision_reason": r.decision_reason,
        "search_scope": r.search_scope,
        "page_refs": r.page_refs or [],
        "decided_by": r.decided_by,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "decision_history": r.decision_history or [],
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def decide_parse_exception(session: Session, *, parse_exception_id: str, decision: str,
                           reason: str, search_scope: str | None = None,
                           page_refs: list[int] | None = None, snapshot_version: str | None = None,
                           note: str | None = None, actor: str, role: str) -> dict[str, Any]:
    """结构化处置表单的后端（docs/12 §2.2-5 / §4.2：代替浏览器 prompt）。

    - decision ∈ approved / not_applicable / deep_review；
    - reason 必填；not_applicable（本文件无此条款）必须给出 search_scope（检索词或范围）；
    - snapshot_version 不一致 → 409 stale_input（例外已被处置/刷新，不静默覆写）；
    - approved/not_applicable 复用 parse_service.decide_candidate（沿用 ADR-007 全部
      红线与审计）；处置后沿用「例外清零自动重确认 + 重算」链。
    """
    if role not in _READABLE:
        raise ApiError("forbidden", "当前角色无权处置解析例外")
    row = session.get(ParseException, parse_exception_id)
    if row is None:
        raise ApiError("not_found", f"解析例外不存在: {parse_exception_id}")
    # F021 §2.1 v1.5 契约全集：approved / rejected（无法确认+原因）/ revised（人工定位）/ not_applicable / deep_review；
    # located 候选不可 not_applicable、missing 候选不可 approved，由 parse_service.decide_candidate 红线校验
    if decision not in ("approved", "rejected", "not_applicable", "deep_review"):
        raise ApiError("invalid_request", "决策必须为：approved / rejected（无法确认）/ not_applicable / deep_review")
    reason = (reason or "").strip()
    if not reason:
        raise ApiError("invalid_request", "处置理由必填（审计留痕，docs/12 §2.2-5）")
    if snapshot_version and snapshot_version != row.snapshot_version:
        raise ApiError("stale_input", "解析例外快照版本已变化，请刷新后重试")
    if decision == "not_applicable" and not (search_scope or "").strip():
        raise ApiError("invalid_request", "声明「本文件无此条款」必须填写检索词或检索范围（禁止无依据断言）")

    from runtime.db import parse_service
    from runtime.db.parse_service import CandidateAlreadyDecided, ParseServiceError

    review_note = f"{reason}" + (f"｜检索范围：{search_scope}" if search_scope else "") \
        + (f"｜备注：{note}" if note else "")

    history = list(row.decision_history or [])
    history.append({
        "decision": decision, "reason": reason, "search_scope": search_scope,
        "page_refs": page_refs or [], "note": note, "by": actor, "at": _now().isoformat(),
        "previous_status": row.status,
    })
    row.decision_history = history
    row.decision = decision
    row.decision_reason = reason
    row.search_scope = (search_scope or "").strip() or None
    row.page_refs = page_refs or []
    row.decided_by = actor
    row.decided_at = _now()

    if decision == "rejected":
        # 无法确认（可重开）：不建规则、留痕原因（F021 §2.1 v1.5/v1.6）
        try:
            parse_service.decide_candidate(
                session, candidate_id=row.candidate_id, material_id=row.material_id,
                version=row.material_version, decision="rejected",
                reviewer=actor, review_note=review_note,
            )
        except CandidateAlreadyDecided as exc:
            raise ApiError("stale_input", f"该解析例外对应的候选已决策（{exc}），请刷新后查看最新快照")
        except ParseServiceError as exc:
            raise ApiError("invalid_request", str(exc))
        row.status = PARSE_EXC_RESOLVED
        _audit(session, actor=actor, action="parse_exception.decided",
               basis=f"exception={parse_exception_id} decision=rejected scope={search_scope or '—'}",
               outcome=PARSE_EXC_RESOLVED, object_ref=row.project_id)
        _reconfirm_if_cleared(session, project_id=row.project_id, material_id=row.material_id,
                              version=row.material_version, actor=actor)
        session.commit()
        return _parse_exception_payload(row)

    if decision == "deep_review":
        # 转深度复核：候选保持 pending，跳转深度处理视图（queue.html），不自动决策
        row.status = PARSE_EXC_DEEP_REVIEW
        _audit(session, actor=actor, action="parse_exception.deep_review",
               basis=f"exception={parse_exception_id} scope={search_scope or '—'}",
               outcome=PARSE_EXC_DEEP_REVIEW, object_ref=row.project_id)
        session.commit()
        return _parse_exception_payload(row)

    # approved / not_applicable → 走既有候选决策通道（ADR-007 红线：missing 候选 approve 400 等）
    try:
        parse_service.decide_candidate(
            session, candidate_id=row.candidate_id, material_id=row.material_id,
            version=row.material_version, decision=decision,
            reviewer=actor, review_note=review_note,
        )
    except CandidateAlreadyDecided as exc:
        raise ApiError("stale_input", f"该解析例外对应的候选已决策（{exc}），请刷新后查看最新快照")
    except ParseServiceError as exc:
        raise ApiError("invalid_request", str(exc))
    row.status = PARSE_EXC_RESOLVED
    _audit(session, actor=actor, action="parse_exception.decided",
           basis=f"exception={parse_exception_id} decision={decision} scope={search_scope or '—'}",
           outcome=PARSE_EXC_RESOLVED, object_ref=row.project_id)

    # 例外清零 → 自动增量重确认 + 重算（沿用 ADR-007 §2.4 链路，docs/12 §2.5）
    _reconfirm_if_cleared(session, project_id=row.project_id, material_id=row.material_id,
                          version=row.material_version, actor=actor)
    session.commit()
    return _parse_exception_payload(row)


def _reconfirm_if_cleared(session: Session, *, project_id: str, material_id: str,
                          version: int | None, actor: str) -> None:
    from runtime.db import parse_service

    remaining = parse_service.pending_exception_count(session, project_id=project_id)
    if remaining:
        return
    # 取项目当前招标材料做重确认（与 parse 路由同口径）
    from runtime.db.models import Material
    tender = session.scalar(
        select(Material).where(Material.project_id == project_id,
                               Material.material_type == "tender_document")
        .order_by(Material.version.desc()).limit(1))
    if tender is None:
        return
    reconfirmed = parse_service.reconfirm_rules_after_exceptions(
        session, project_id=project_id, material_id=tender.material_id,
        version=tender.version, actor=actor,
    )
    if reconfirmed is None:
        return
    rule_set_id = reconfirmed.get("rule_set_id")
    from runtime.db import worker_service
    worker_service.create_job(
        session, kind="match.recalculate", input_ref=f"reconfirm:{rule_set_id}",
        project_id=project_id,
    )
    from runtime.db.api_service import mark_admission_results_stale
    mark_admission_results_stale(session, project_id)
    _audit(session, actor=actor, action="parse.reconfirmed",
           basis=f"rule_set={rule_set_id}", outcome="reconfirmed", object_ref=project_id)


# ---------------------------------------------------------------------------
# 候选班子（docs/12 §3.3 / §4.2）
# ---------------------------------------------------------------------------

def candidate_plans_view(session: Session, *, project_id: str, run_id: str | None = None,
                         role: str, actor: str) -> dict[str, Any]:
    _require_readable(role)
    project, run, admission, context = _context_and_admission(session, project_id, run_id)
    if run is None:
        return {"project_id": project_id, **context, "plans": []}
    plans = session.scalars(select(CandidatePlan).where(CandidatePlan.run_id == run.run_id)
                            .order_by(CandidatePlan.is_primary.desc(), CandidatePlan.created_at)).all()
    members = session.scalars(select(CandidatePlanMember).where(
        CandidatePlanMember.candidate_plan_id.in_([p.candidate_plan_id for p in plans] or ["-"]))).all()
    links = session.scalars(select(CandidatePlanRequirementLink).where(
        CandidatePlanRequirementLink.candidate_plan_id.in_([p.candidate_plan_id for p in plans] or ["-"]))).all()
    members_by_plan: dict[str, list] = {}
    for m in members:
        members_by_plan.setdefault(m.candidate_plan_id, []).append({
            "role_code": m.role_code, "role_label": ROLE_CODE_LABEL.get(m.role_code, m.role_code),
            "person_id": m.person_id, "person_kind": m.person_kind, "is_primary": m.is_primary,
            "availability_as_of": m.availability_as_of, "evidence_refs": m.evidence_refs or [],
        })
    links_by_plan: dict[str, list] = {}
    for l in links:
        links_by_plan.setdefault(l.candidate_plan_id, []).append({
            "requirement_id": l.requirement_id, "person_id": l.person_id,
            "result": l.result, "reason_code": l.reason_code,
        })
    return {
        "project_id": project_id,
        **context,
        "plans": [{
            "candidate_plan_id": p.candidate_plan_id,
            "role_code": p.role_code,
            "role_label": ROLE_CODE_LABEL.get(p.role_code, p.role_code),
            "status": p.status,
            "is_primary": p.is_primary,
            "selection_basis": p.selection_basis,
            "gap_reason_codes": p.gap_reason_codes or [],
            "members": members_by_plan.get(p.candidate_plan_id, []),
            "requirement_links": links_by_plan.get(p.candidate_plan_id, []),
            "selected_by": p.selected_by, "selected_reason": p.selected_reason,
            "selected_at": p.selected_at.isoformat() if p.selected_at else None,
        } for p in plans],
    }


def select_candidate_plan(session: Session, *, project_id: str, candidate_plan_id: str,
                          run_id: str, reason: str, actor: str, role: str) -> dict[str, Any]:
    """投标负责人（经营负责人）人工选定候选班子（docs/12 §4.2）。

    生成新快照输入并触发重算（旧结果 stale）；不改变结论本身——满足与否仍由
    引擎按新输入重算得出。
    """
    if role != rbac.BUSINESS_HEAD:
        raise ApiError("forbidden", "仅投标负责人（经营负责人）可以选定候选班子方案")
    reason = (reason or "").strip()
    if not reason:
        raise ApiError("invalid_request", "选定理由必填（审计留痕）")
    _project(session, project_id)
    plan = session.get(CandidatePlan, candidate_plan_id)
    if plan is None or plan.project_id != project_id:
        raise ApiError("not_found", f"候选班子方案不存在: {candidate_plan_id}")
    if run_id != plan.run_id:
        raise ApiError("stale_input", "方案所属运行与当前引用不一致，请刷新后重试")
    run = session.get(MatchRun, run_id)
    if run is None or not run.is_current:
        raise ApiError("stale_input", "所选方案不在当前运行中，不能据此重算（docs/12 §4.1）")
    others = session.scalars(select(CandidatePlan).where(
        CandidatePlan.run_id == run_id, CandidatePlan.status == "selected")).all()
    for other in others:
        other.status = "superseded"
    plan.status = "selected"
    plan.selected_by = actor
    plan.selected_reason = reason
    plan.selected_at = _now()
    from runtime.db import worker_service
    from runtime.db.api_service import mark_admission_results_stale
    mark_admission_results_stale(session, project_id)
    job, created = worker_service.create_job(
        session, kind="match.recalculate", input_ref=f"candidate-plan:{candidate_plan_id}",
        project_id=project_id,
    )
    _audit(session, actor=actor, action="candidate_plan.selected",
           basis=f"plan={candidate_plan_id} run={run_id} job={job.job_id}",
           outcome="recalc_scheduled", object_ref=project_id)
    session.commit()
    return {"candidate_plan_id": candidate_plan_id, "status": "selected",
            "job_id": job.job_id, "created": created}


# ---------------------------------------------------------------------------
# 聚合补证任务与资源处置建议（docs/12 §3.4 / §4.2）
# ---------------------------------------------------------------------------

def remediation_tasks_view(session: Session, *, project_id: str, run_id: str | None = None,
                           role: str, actor: str) -> dict[str, Any]:
    """按聚合缺口返回补证任务（同一缺口一条任务、覆盖条款完整）+ 资源处置建议分开。"""
    _require_readable(role)
    project, run, admission, context = _context_and_admission(session, project_id, run_id)
    tasks = session.scalars(select(RemediationTask).where(
        RemediationTask.project_id == project_id,
        RemediationTask.task_type == "evidence_supplement",
    ).order_by(RemediationTask.created_at.desc())).all()
    task_payloads = [{
        "task_id": t.task_id,
        "gap_key": t.gap_key,
        "lot_id": t.lot_id,
        "title": t.title,
        "state": t.state,
        "requirement_refs": t.requirement_refs or [],
        "clause_refs": t.clause_refs or [],
        "covering_count": len(t.requirement_refs or []),
        "assignee_role": t.assignee_role,
        "due_at": t.due_at.isoformat() if t.due_at else None,
        "evidence_required": t.evidence_required or [],
        "source_run_id": t.source_run_id,
    } for t in tasks]
    # 资源处置建议：明确不满足（blocked）项 —— 不能以补证"洗白"（docs/12 §4.3-4）
    resource_items = []
    for item in ((admission.blocked_items or []) if admission else []):
        resource_items.append({
            "requirement_id": item.get("requirement_id"),
            "clause_ref": item.get("clause_ref"),
            "text": item.get("text"),
            "reason_code": item.get("reason_code"),
            "reason_code_label": REASON_CODE_LABEL.get(item.get("reason_code") or "", None),
            "next_action": item.get("next_action") or next_action_for(item.get("reason_code")),
            "observed_value": item.get("observed_value"),
            "required_value": item.get("required_value"),
            "note": "资源事实缺口：需更换/补足资源或纠正在施安排，不能用「补一份证明」替代",
        })
    return {"project_id": project_id, **context,
            "evidence_tasks": task_payloads, "resource_disposition": resource_items}


# ---------------------------------------------------------------------------
# 历史样本测试上下文（docs/12 §1.2/§7：服务端标识，审批/递交二次拒绝）
# ---------------------------------------------------------------------------

def test_context_view(session: Session, *, project_id: str, role: str, actor: str) -> dict[str, Any]:
    _require_readable(role)
    project = _project(session, project_id)
    ctx = project.test_context or {}
    return {
        "project_id": project_id,
        "historical_sample": bool(ctx.get("historical_sample")),
        "declared_by": ctx.get("declared_by"),
        "reason": ctx.get("reason"),
        "declared_at": ctx.get("declared_at"),
        "banner": HISTORICAL_SAMPLE_BANNER if ctx.get("historical_sample") else None,
        "note": HISTORICAL_SAMPLE_NOTE if ctx.get("historical_sample") else None,
    }


def set_test_context(session: Session, *, project_id: str, historical_sample: bool,
                     reason: str, actor: str, role: str) -> dict[str, Any]:
    """登记/解除历史解析样本测试上下文（服务端唯一入口，写审计）。

    登记后：结果页显示醒目标识、准入 blocking_reasons 点名 historical_sample_context、
    审批创建与递交相关接口二次拒绝；**匹配/重算的身份与截止门禁对测试上下文放行**
    （ADR-008 / docs/12 §1.2——历史文件与公告不一致、截止已过属既定测试安排）。
    若项目尚无匹配运行（含此前被门禁终态拒绝的任务），登记后自动重新触发匹配。
    生产身份门禁（ADR-004）对未声明测试上下文的真实项目不受影响。
    """
    if role not in _READABLE:
        raise ApiError("forbidden", "当前角色无权登记测试上下文")
    reason = (reason or "").strip()
    if not reason:
        raise ApiError("invalid_request", "登记/解除测试上下文必须说明理由（审计留痕）")
    project = _project(session, project_id)
    previous = bool((project.test_context or {}).get("historical_sample"))
    if historical_sample:
        project.test_context = {
            "historical_sample": True, "declared_by": actor, "reason": reason,
            "declared_at": _now().isoformat(),
        }
    else:
        project.test_context = None
    _audit(session, actor=actor, action="project.test_context",
           basis=f"historical_sample={historical_sample} reason={reason}",
           outcome=f"previous={previous}", object_ref=project_id)
    retriggered = None
    if historical_sample and not previous:
        retriggered = _retrigger_match_for_test_context(session, project_id=project_id, actor=actor)
    session.commit()
    payload = test_context_view(session, project_id=project_id, role=role, actor=actor)
    if retriggered:
        payload["match_job"] = retriggered
    return payload


def _retrigger_match_for_test_context(session: Session, *, project_id: str, actor: str):
    """测试放行后的匹配重触发：规则集已存在且尚无 MatchRun 时（含此前被身份/截止
    门禁终态拒绝的 match.run 任务），以 retry_failed=True 复活/新建任务（与
    POST /match 路由同一幂等口径）。已有运行则不动（由重算流程负责）。"""
    from sqlalchemy import select as _select

    from runtime.core import orchestration
    from runtime.db import worker_service
    from runtime.db.models import MatchRun, Material, RuleSet

    rule_set = session.scalar(
        _select(RuleSet).where(RuleSet.project_id == project_id)
        .order_by(RuleSet.created_at.desc()).limit(1))
    if rule_set is None:
        return None
    existing_run = session.scalar(
        _select(MatchRun).where(MatchRun.project_id == project_id)
        .order_by(MatchRun.is_current.desc(), MatchRun.created_at.desc()).limit(1))
    if existing_run is not None:
        return None
    materials = session.scalars(
        _select(Material).where(Material.project_id == project_id)).all()
    material_dicts = [{"material_id": m.material_id, "material_type": m.material_type,
                       "parse_status": m.parse_status, "version": m.version} for m in materials]
    if not orchestration.should_trigger_first_match(material_dicts, 0):
        return None
    job, created = worker_service.create_job(
        session, kind="match.run", input_ref=project_id, project_id=project_id,
        retry_failed=True,
    )
    _audit(session, actor=actor, action="match.test_context_retrigger",
           basis=f"historical_sample test release; job={job.job_id}",
           outcome="created" if created else "revived", object_ref=project_id)
    return {"job_id": job.job_id, "created": created}


def ensure_not_historical_sample(session: Session, project_id: str, *, actor: str,
                                 action: str) -> None:
    """审批创建 / 审批包导出 / 递交相关接口的二次拒绝（docs/12 §7）。"""
    project = session.get(Project, project_id)
    if project is None:
        return
    if not bool((project.test_context or {}).get("historical_sample")):
        return
    _audit(session, actor=actor, action=f"{action}_denied",
           basis="historical_sample_context", outcome="historical_sample_context",
           object_ref=project_id)
    session.commit()
    raise ApiError(
        "invalid_state_transition",
        "历史解析样本测试上下文：本项目的解析/匹配结果不可用于真实审批或递交（docs/12 §1.2）",
        detail={"reason": "historical_sample_context"},
    )
