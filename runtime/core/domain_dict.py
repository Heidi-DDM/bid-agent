# ADR-008 / docs/12 §3.1-§3.4：结果分层领域字典（单一事实源）
#
# 所有枚举、原因码、岗位、动作阶段、评分分类均以本模块为唯一字典，
# 带 DICT_VERSION 版本号；引擎、编排、API 投影、前端标签共享同一口径。
# 变更必须同步 docs/03-功能规格/F008/F020/F023 与 docs/04-修改日志。
#
# 红线（ADR-008 §2.3 / docs/12 §1.4-6）：
# - 不得用 unverifiable 作为前端唯一显示词：历史 DB 枚举保留，投影层映射到
#   细粒度结果 + reason_code；无可靠映射的历史行显示「历史结果，需重算确认」。
# - 满分 ≠ 自动投标；模型/向量分数不进入准入公式；结论必须条款+证据回链。
from __future__ import annotations

DICT_VERSION = "adr008-v1.0.0"

# ---------------------------------------------------------------------------
# 工作域（docs/12 §2.1：资格与资源 / 评分分析 / 投标执行 / 解析质量）
# Requirement.domain 取前三者；parse_quality 是运行级工作域（无 Requirement）。
DOMAIN_QUALIFICATION = "qualification"
DOMAIN_SCORING = "scoring"
DOMAIN_OPERATION = "operation"
DOMAIN_PARSE_QUALITY = "parse_quality"

DOMAINS = (DOMAIN_QUALIFICATION, DOMAIN_SCORING, DOMAIN_OPERATION, DOMAIN_PARSE_QUALITY)

DOMAIN_LABEL = {
    DOMAIN_QUALIFICATION: "资格与资源核查",
    DOMAIN_SCORING: "评分分析",
    DOMAIN_OPERATION: "投标执行计划",
    DOMAIN_PARSE_QUALITY: "解析质量与待确认项",
}

# req_type → domain 的确定性映射（服务端拥有，前端不猜测，docs/12 §3.1）
REQ_TYPE_DOMAIN = {
    "hard_requirement": DOMAIN_QUALIFICATION,
    "scored_requirement": DOMAIN_SCORING,
    "action_requirement": DOMAIN_OPERATION,
}

# ---------------------------------------------------------------------------
# 细粒度结论（docs/12 §3.2）。DB match_items.match_result 历史枚举
# satisfied/partial/not_satisfied/unverifiable/manual_review 保持兼容；
# 投影层按 reason_code 把 unverifiable 细分为 blocked_missing_data 等可行动状态。
RESULT_SATISFIED = "satisfied"
RESULT_NOT_SATISFIED = "not_satisfied"
RESULT_BLOCKED_MISSING_DATA = "blocked_missing_data"
RESULT_MANUAL_REVIEW = "manual_review"
RESULT_NOT_CALCULABLE = "not_calculable"
RESULT_PARSE_EXCEPTION = "parse_exception"
RESULT_NOT_EVALUATED = "not_evaluated"
RESULT_LEGACY_AMBIGUOUS = "legacy_ambiguous"

# 动作项结论 = 动作状态本身（docs/12 §2.4 / ADR-008 §2.3：
# not_started 不再伪装成缺证 unverifiable）
ACTION_NOT_STARTED = "not_started"
ACTION_READY = "ready"
ACTION_COMPLETED = "completed"
ACTION_OVERDUE = "overdue"
ACTION_NOT_APPLICABLE = "not_applicable"
ACTION_STATUSES = (ACTION_NOT_STARTED, ACTION_READY, ACTION_COMPLETED, ACTION_OVERDUE, ACTION_NOT_APPLICABLE)

ACTION_STATUS_LABEL = {
    ACTION_NOT_STARTED: "尚未开始",
    ACTION_READY: "就绪",
    ACTION_COMPLETED: "已完成",
    ACTION_OVERDUE: "已逾期",
    ACTION_NOT_APPLICABLE: "不适用",
}

# 细粒度结果的中文标签（颜色之外的文字标识，docs/12 §6）
RESULT_LABEL = {
    RESULT_SATISFIED: "已满足",
    RESULT_NOT_SATISFIED: "明确不满足",
    RESULT_BLOCKED_MISSING_DATA: "待补资料",
    RESULT_MANUAL_REVIEW: "人工复核",
    RESULT_NOT_CALCULABLE: "暂不可计算",
    RESULT_PARSE_EXCEPTION: "解析待处置",
    RESULT_NOT_EVALUATED: "未执行",
    RESULT_LEGACY_AMBIGUOUS: "历史结果，需重算确认",
    ACTION_NOT_STARTED: "尚未开始",
    ACTION_READY: "就绪",
    ACTION_COMPLETED: "已完成",
    ACTION_OVERDUE: "已逾期",
    ACTION_NOT_APPLICABLE: "不适用",
}

# 摘要卡固定排序（docs/12 §2.2-2）：明确不满足 → 待补资料 → 人工复核 → 已满足
SUMMARY_ORDER = (RESULT_NOT_SATISFIED, RESULT_BLOCKED_MISSING_DATA, RESULT_MANUAL_REVIEW, RESULT_SATISFIED)

# ---------------------------------------------------------------------------
# 最小原因码表（docs/12 §3.2）。新增码必须登记 F008/F023。
RC_VERIFIED_QUANTITY_INSUFFICIENT = "verified_quantity_insufficient"
RC_GRADE_BELOW_REQUIREMENT = "grade_below_requirement"
RC_CERTIFICATE_EXPIRED = "certificate_expired"
RC_PROFESSION_MISMATCH = "profession_mismatch"
RC_ONSITE_CONFLICT = "onsite_conflict"
RC_ENTERPRISE_RECORD_MISSING = "enterprise_record_missing"
RC_EVIDENCE_MISSING = "evidence_missing"
RC_EVIDENCE_PENDING_VERIFICATION = "evidence_pending_verification"
RC_VALIDITY_DATE_MISSING = "validity_date_missing"
RC_EVIDENCE_CONFLICT = "evidence_conflict"
RC_RULE_UNSTRUCTURED = "rule_unstructured"
RC_CANDIDATE_PLAN_UNSELECTED = "candidate_plan_unselected"
RC_PROFESSIONAL_JUDGEMENT_REQUIRED = "professional_judgement_required"
RC_FORMULA_MISSING = "formula_missing"
RC_AUTHORIZED_INPUT_MISSING = "authorized_input_missing"
RC_SCORING_EVIDENCE_MISSING = "scoring_evidence_missing"
RC_ANCHOR_NOT_LOCATED = "anchor_not_located"
RC_LLM_CANDIDATE_PENDING = "llm_candidate_pending"
RC_SOURCE_CONFLICT = "source_conflict"
RC_LEGACY_AMBIGUOUS = "legacy_ambiguous"
RC_NOT_APPLICABLE = "not_applicable"

REASON_CODE_BY_RESULT = {
    RESULT_NOT_SATISFIED: (
        RC_VERIFIED_QUANTITY_INSUFFICIENT, RC_GRADE_BELOW_REQUIREMENT, RC_CERTIFICATE_EXPIRED,
        RC_PROFESSION_MISMATCH, RC_ONSITE_CONFLICT,
    ),
    RESULT_BLOCKED_MISSING_DATA: (
        RC_ENTERPRISE_RECORD_MISSING, RC_EVIDENCE_MISSING, RC_EVIDENCE_PENDING_VERIFICATION,
        RC_VALIDITY_DATE_MISSING,
    ),
    RESULT_MANUAL_REVIEW: (
        RC_EVIDENCE_CONFLICT, RC_RULE_UNSTRUCTURED, RC_CANDIDATE_PLAN_UNSELECTED,
        RC_PROFESSIONAL_JUDGEMENT_REQUIRED,
    ),
    RESULT_NOT_CALCULABLE: (
        RC_FORMULA_MISSING, RC_AUTHORIZED_INPUT_MISSING, RC_SCORING_EVIDENCE_MISSING,
    ),
    RESULT_PARSE_EXCEPTION: (
        RC_ANCHOR_NOT_LOCATED, RC_LLM_CANDIDATE_PENDING, RC_SOURCE_CONFLICT,
    ),
    RESULT_LEGACY_AMBIGUOUS: (RC_LEGACY_AMBIGUOUS,),
}

REASON_CODE_LABEL = {
    RC_VERIFIED_QUANTITY_INSUFFICIENT: "已核验数量不足",
    RC_GRADE_BELOW_REQUIREMENT: "等级低于要求",
    RC_CERTIFICATE_EXPIRED: "证书已过期",
    RC_PROFESSION_MISMATCH: "专业/类别不符",
    RC_ONSITE_CONFLICT: "人员在施冲突",
    RC_ENTERPRISE_RECORD_MISSING: "企业台账无此类记录",
    RC_EVIDENCE_MISSING: "缺少可核验证据",
    RC_EVIDENCE_PENDING_VERIFICATION: "证据尚未核验",
    RC_VALIDITY_DATE_MISSING: "有效期字段缺失",
    RC_EVIDENCE_CONFLICT: "证据相互冲突",
    RC_RULE_UNSTRUCTURED: "规则未结构化",
    RC_CANDIDATE_PLAN_UNSELECTED: "无完整候选班子方案",
    RC_PROFESSIONAL_JUDGEMENT_REQUIRED: "需专业判断",
    RC_FORMULA_MISSING: "评分公式缺失",
    RC_AUTHORIZED_INPUT_MISSING: "授权输入（报价等）未录入",
    RC_SCORING_EVIDENCE_MISSING: "计分证据缺失",
    RC_ANCHOR_NOT_LOCATED: "关键条款未定位",
    RC_LLM_CANDIDATE_PENDING: "模型候选待确认",
    RC_SOURCE_CONFLICT: "多版本来源冲突",
    RC_LEGACY_AMBIGUOUS: "历史结果语义不明",
    RC_NOT_APPLICABLE: "不适用（非判定项）",
}

# 下一步动作（docs/12 §3.2 处置列）
NEXT_ACTION_LABEL = {
    "replace_or_add_qualified_person": "更换/补足合格人员",
    "renew_or_upgrade_certificate": "更新或升级证书资源",
    "resolve_onsite_conflict": "调整在施安排释放人员",
    "create_or_merge_evidence_task": "创建/合并补证任务",
    "verify_pending_evidence": "核验在途证据",
    "supply_validity_dates": "补录有效期字段",
    "assign_reviewer": "指派复核人处理",
    "structure_scoring_rule": "结构化评分规则",
    "await_authorized_input": "等待授权输入（报价等）",
    "handle_parse_exception": "处置解析例外",
    "confirm_not_applicable": "确认不适用（归档说明）",
    "complete_operation_task": "到阶段完成执行动作",
    "recalculate_to_confirm": "重算确认",
}

# reason_code → 默认下一步（服务端字典，不前端猜测）
NEXT_ACTION_BY_REASON = {
    RC_VERIFIED_QUANTITY_INSUFFICIENT: "replace_or_add_qualified_person",
    RC_GRADE_BELOW_REQUIREMENT: "renew_or_upgrade_certificate",
    RC_CERTIFICATE_EXPIRED: "renew_or_upgrade_certificate",
    RC_PROFESSION_MISMATCH: "replace_or_add_qualified_person",
    RC_ONSITE_CONFLICT: "resolve_onsite_conflict",
    RC_ENTERPRISE_RECORD_MISSING: "create_or_merge_evidence_task",
    RC_EVIDENCE_MISSING: "create_or_merge_evidence_task",
    RC_EVIDENCE_PENDING_VERIFICATION: "verify_pending_evidence",
    RC_VALIDITY_DATE_MISSING: "supply_validity_dates",
    RC_EVIDENCE_CONFLICT: "assign_reviewer",
    RC_RULE_UNSTRUCTURED: "assign_reviewer",
    RC_CANDIDATE_PLAN_UNSELECTED: "assign_reviewer",
    RC_PROFESSIONAL_JUDGEMENT_REQUIRED: "assign_reviewer",
    RC_FORMULA_MISSING: "structure_scoring_rule",
    RC_AUTHORIZED_INPUT_MISSING: "await_authorized_input",
    RC_SCORING_EVIDENCE_MISSING: "create_or_merge_evidence_task",
    RC_ANCHOR_NOT_LOCATED: "handle_parse_exception",
    RC_LLM_CANDIDATE_PENDING: "handle_parse_exception",
    RC_SOURCE_CONFLICT: "handle_parse_exception",
    RC_LEGACY_AMBIGUOUS: "recalculate_to_confirm",
    RC_NOT_APPLICABLE: "confirm_not_applicable",
}

# ---------------------------------------------------------------------------
# 证据状态（docs/12 §3.2 evidence_state）
EVIDENCE_STATE_LABEL = {
    "sufficient": "证据充分且满足",
    "sufficient_but_insufficient": "证据充分但数量/等级不足",
    "missing": "无可核验证据",
    "pending_verification": "证据在途未核验",
    "conflict": "证据冲突",
    "not_applicable": "无需企业证据",
    "none": "无",
}

# ---------------------------------------------------------------------------
# 评分分类（docs/12 §2.3 / §3.1 score_classification）
SC_OBJECTIVE_CALCULABLE = "objective_calculable"
SC_SUBJECTIVE_REVIEW = "subjective_review"
SC_PRICE_FORMULA = "price_formula"
SC_METHODOLOGY_ONLY = "methodology_only"
SC_UNSTRUCTURED = "unstructured"

SCORE_CLASSIFICATIONS = (
    SC_OBJECTIVE_CALCULABLE, SC_SUBJECTIVE_REVIEW, SC_PRICE_FORMULA,
    SC_METHODOLOGY_ONLY, SC_UNSTRUCTURED,
)

SCORE_CLASSIFICATION_LABEL = {
    SC_OBJECTIVE_CALCULABLE: "可自动复算的客观项",
    SC_SUBJECTIVE_REVIEW: "待内部质量评审项",
    SC_PRICE_FORMULA: "报价计算规则",
    SC_METHODOLOGY_ONLY: "评标方法说明",
    SC_UNSTRUCTURED: "评分规则待结构化",
}

# comparison_status（docs/12 §3.1）
COMPARISON_STATUS_LABEL = {
    "comparable": "可比较",
    "missing_rule_params": "规则参数缺失",
    "needs_professional_judgement": "需专业判断",
    "not_applicable": "不适用",
}

# decision_scope（docs/12 §3.1）：只有 admission 进入内部准入
DECISION_SCOPE_LABEL = {
    "admission": "参与内部准入",
    "information_only": "仅信息展示",
}

# ---------------------------------------------------------------------------
# 岗位与人员绑定（docs/12 §3.1 / §3.3）
ROLE_PROJECT_MANAGER = "project_manager"
ROLE_CONSTRUCTION_LEAD = "construction_lead"
ROLE_DESIGN_LEAD = "design_lead"
ROLE_TECH_LEAD = "tech_lead"
ROLE_SAFETY_OFFICER = "safety_officer"
ROLE_TECHNICAL_TEAM = "technical_team"

ROLE_CODE_LABEL = {
    ROLE_PROJECT_MANAGER: "项目经理",
    ROLE_CONSTRUCTION_LEAD: "施工负责人",
    ROLE_DESIGN_LEAD: "设计负责人",
    ROLE_TECH_LEAD: "技术负责人",
    ROLE_SAFETY_OFFICER: "专职安全员",
    ROLE_TECHNICAL_TEAM: "技术团队",
}

PERSON_BINDING_POLICY_LABEL = {
    "none": "无人员绑定",
    "same_person_required": "同一人须满足全部条件",
    "same_team_required": "同一班子须满足全部条件",
    "alternative_candidates_allowed": "允许多候选替代",
}

# 规则类型 → 岗位（确定性映射；不能仅靠文本匹配职位，docs/12 §3.1）
RULE_TYPE_ROLE_CODE = {
    "project_manager": ROLE_PROJECT_MANAGER,
    "construction_lead": ROLE_CONSTRUCTION_LEAD,
    "design_lead": ROLE_DESIGN_LEAD,
    "tech_lead": ROLE_TECH_LEAD,
    "safety_officer": ROLE_SAFETY_OFFICER,
    "technical_team": ROLE_TECHNICAL_TEAM,
    "tech_team": ROLE_TECHNICAL_TEAM,
}

# 需要同一人绑定的单岗位规则（docs/12 §3.3：项目经理相关条件按同一 person_id 聚合）
SINGLE_PERSON_RULE_TYPES = frozenset({
    "project_manager", "construction_lead", "design_lead", "tech_lead",
})

# ---------------------------------------------------------------------------
# 动作阶段与任务种类（docs/12 §2.4 / §3.1；required_by_stage 增 preparation）
STAGE_PREPARATION = "preparation"
STAGE_APPROVAL_READY = "approval_ready"
STAGE_SUBMISSION_READY = "submission_ready"
STAGE_SUBMITTED = "submitted"
STAGE_OPENED = "opened"

STAGES = (STAGE_PREPARATION, STAGE_APPROVAL_READY, STAGE_SUBMISSION_READY, STAGE_SUBMITTED, STAGE_OPENED)

STAGE_LABEL = {
    STAGE_PREPARATION: "投标准备",
    STAGE_APPROVAL_READY: "审批前就绪",
    STAGE_SUBMISSION_READY: "准备递交",
    STAGE_SUBMITTED: "已递交",
    STAGE_OPENED: "开标",
}

TASK_KIND_LABEL = {
    "registration": "报名/文件获取",
    "ca_cert": "CA 证书",
    "bond": "保证金",
    "preparation": "标书编制",
    "submission": "递交投标文件",
    "opening": "开标",
    "site_visit": "现场踏勘",
    "other": "其他动作",
}

# ---------------------------------------------------------------------------
# 解析例外风险分层（docs/12 §2.5：按风险而非探针顺序排序）
PARSE_EXC_CONFLICT = "source_conflict"          # rank 1：已定位但条款/版本冲突
PARSE_EXC_HARD_ANCHOR_MISS = "anchor_not_located"  # rank 2：硬性强锚点未定位
PARSE_EXC_LLM_PENDING = "llm_candidate_pending"    # rank 3：模型定位/低置信
PARSE_EXC_PROBE_MISS = "probe_miss"                # rank 4：常见探针未命中（无明确原文预期）

PARSE_EXCEPTION_RISK_RANK = {
    PARSE_EXC_CONFLICT: 1,
    PARSE_EXC_HARD_ANCHOR_MISS: 2,
    PARSE_EXC_LLM_PENDING: 3,
    PARSE_EXC_PROBE_MISS: 4,
}

PARSE_EXCEPTION_RISK_LABEL = {
    PARSE_EXC_CONFLICT: "条款/版本冲突",
    PARSE_EXC_HARD_ANCHOR_MISS: "硬性关键条款未定位",
    PARSE_EXC_LLM_PENDING: "模型定位候选待确认",
    PARSE_EXC_PROBE_MISS: "常见条款未检出（不代表文件无此要求）",
}

PARSE_EXC_OPEN = "open"
PARSE_EXC_DEEP_REVIEW = "deep_review"
PARSE_EXC_RESOLVED = "resolved"
PARSE_EXC_STATUS_LABEL = {
    PARSE_EXC_OPEN: "待处置",
    PARSE_EXC_DEEP_REVIEW: "深度复核中",
    PARSE_EXC_RESOLVED: "已处置",
}

# ---------------------------------------------------------------------------
# 准入阻断原因（docs/12 §4.3/§4.4：blocking_reasons[] 使用稳定枚举）
BLOCK_QUALIFICATION = "qualification_not_satisfied"
BLOCK_SCORING = "scoring_not_ready"
BLOCK_PARSE_EXCEPTIONS = "parse_exceptions_pending"
BLOCK_APPROVAL_ACTIONS = "approval_actions_not_ready"
BLOCK_HISTORICAL_SAMPLE = "historical_sample_context"
BLOCK_STALE_RESULT = "result_stale"

BLOCKING_REASON_LABEL = {
    BLOCK_QUALIFICATION: "资格/资源存在明确不满足或缺证",
    BLOCK_SCORING: "评分未达到内部满分条件",
    BLOCK_PARSE_EXCEPTIONS: "存在待处置解析例外（满分准入暂不可用）",
    BLOCK_APPROVAL_ACTIONS: "审批前动作未就绪",
    BLOCK_HISTORICAL_SAMPLE: "历史解析样本测试上下文（不可用于真实审批或递交）",
    BLOCK_STALE_RESULT: "结果已过期（stale），需重算",
}

# 历史样本标识（docs/12 §1.2/§7：服务端测试上下文，不可由前端参数伪造）
HISTORICAL_SAMPLE_BANNER = "历史解析样本／非当前项目文件"
HISTORICAL_SAMPLE_NOTE = "本结果仅用于验证解析与匹配能力，不可用于真实项目准入、审批或递交"


def reason_code_label(code: str | None) -> str:
    return REASON_CODE_LABEL.get(code or "", code or "")


def next_action_for(reason_code: str | None) -> str | None:
    return NEXT_ACTION_BY_REASON.get(reason_code or "")


def project_result_state(match_result: str | None, reason_code: str | None = None,
                         req_type: str | None = None) -> str:
    """历史 DB 枚举 → 细粒度投影状态（docs/12 §3.2：禁止 unverifiable 作唯一显示词）。

    - action_requirement 的结果本就是动作状态，直接透传；
    - unverifiable 按 reason_code 映射 blocked_missing_data / parse_exception / 保留；
    - scored 的不可计算在引擎侧已写 not_calculable；
    - 无 reason_code 的历史 unverifiable → legacy_ambiguous（历史结果，需重算确认）。
    """
    if not match_result:
        return RESULT_NOT_EVALUATED
    if req_type == "action_requirement":
        return match_result
    if match_result == "unverifiable":
        if reason_code in (RC_ANCHOR_NOT_LOCATED, RC_LLM_CANDIDATE_PENDING, RC_SOURCE_CONFLICT):
            return RESULT_PARSE_EXCEPTION
        if reason_code in REASON_CODE_BY_RESULT.get(RESULT_BLOCKED_MISSING_DATA, ()):
            return RESULT_BLOCKED_MISSING_DATA
        if reason_code in REASON_CODE_BY_RESULT.get(RESULT_NOT_CALCULABLE, ()):
            return RESULT_NOT_CALCULABLE
        return RESULT_LEGACY_AMBIGUOUS
    if match_result == "partial":
        # 历史枚举保留但生产引擎不产出；语义不明，按历史歧义处理
        return RESULT_LEGACY_AMBIGUOUS
    return match_result
