/* ============================================================
   投标智能体 · 原型脱敏数据（R011 / F010）
   数据源：验证受限材料/农大/*（真实案例脱敏）+ 农大 20 条黄金规则
   口径：与 scripts/matching/golden_requirements_nongda.json 一致
   ============================================================ */

/* 状态机（ADR-001 §2.2）用于徽标渲染 */
const STATE_META = {
  draft:                    { label: "草稿",            tone: "gray" },
  collecting:               { label: "采集中",          tone: "info" },
  parsed:                   { label: "已解析",          tone: "info" },
  matching:                 { label: "匹配中",          tone: "info" },
  blocked_missing_data:     { label: "阻断·待补材料",    tone: "warn" },
  blocked_hard_requirement: { label: "阻断·硬性不满足",  tone: "danger" },
  not_qualified:            { label: "不满足资格",       tone: "danger" },
  qualified_full_score:     { label: "满分·待送审批",    tone: "gold" },
  pending_bid_approval:     { label: "待投标审批",       tone: "gold" },
  approved_for_bidding:     { label: "已批准投标",       tone: "ok" },
  rejected_by_approver:     { label: "审批驳回",         tone: "danger" },
  blocked_waiver_expired:   { label: "豁免过期·回阻断",  tone: "warn" },
  archived:                 { label: "已归档",          tone: "gray" },
};

const MATCH_META = {
  satisfied:    { label: "满足",         tone: "ok" },
  not_satisfied:{ label: "不满足",       tone: "danger" },
  unverifiable: { label: "待补/不可判定", tone: "warn" },
  manual_review:{ label: "人工复核",     tone: "review" },
};

/* 角色（F010 §3 / §9；F026/ADR-005 收敛为两级，历史 data_admin/legal 归并投标专员） */
const ROLES = {
  bid_specialist: { label: "投标专员",  canApprove: false },
  data_admin:     { label: "投标专员（原数据管理员）", canApprove: false },
  business_head:  { label: "经营负责人", canApprove: true },
  legal:          { label: "投标专员（原法务）", canApprove: false },
};

const PROTOTYPE = {
  version: "0.1.0",
  demo_note: "原型演示数据来自真实脱敏案例（河北农业大学东校区研究生宿舍建设项目施工，事后验证样本），用于 Iteration 3 用户验证；非真实系统数据。",

  /* ---------- 任务列表（首页） ---------- */
  projects: [
    {
      id: "ND-2025",
      name: "河北农业大学东校区研究生宿舍建设项目施工（脱敏演示）",
      state: "blocked_missing_data",
      region: "保定市",
      type: "房屋建筑工程 · 公开招标 · 资格后审",
      budget_cap: "12,471.59 万元",
      bond: "20 万元",
      bid_validity: "120 日历天",
      deadline: "2025-10-30 09:00",
      deadline_fmt: "2025 年 10 月 30 日 09:00",
      clarify_count: 2,
      clarify_note: "澄清 ×2（共 35 问，无资格实质变更）",
      source: "河北省公共资源交易服务平台",
      source_url: "https://ggzy.hebei.gov.cn/（演示链接）",
      tender_file: "招标文件.pdf（476 页，正文可解析）",
      parse_status: "parsed",
      updated: "2026-08-28",
      is_main: true,
    },
    {
      id: "BY-2024",
      name: "博野县高标准农田建设项目（国债）施工（8 标段）",
      state: "archived",
      region: "保定市博野县",
      type: "市政/农田水利 · 事后验证样本（已投标·已验收）",
      source: "河北省公共资源交易服务平台",
      updated: "2026-08-27",
      note: "R009 黄金样本：16 条规则 × 8 标段矩阵实测通过；进入原型任务列表仅作历史对照，不参与主旅程。",
    },
    {
      id: "FP-2024",
      name: "阜平县高铁片区综合管网建设项目",
      state: "parsed",
      region: "保定市阜平县",
      type: "市政公用工程 · 公开招标",
      source: "河北省公共资源交易服务平台",
      parse_status: "partial",
      updated: "2026-08-26",
      note: "解析部分完成（评分办法待人工复核）→ 演示“解析失败/人工复核”异常态。",
    },
    {
      id: "GZ-2024",
      name: "广宗县葫芦中学建设项目",
      state: "blocked_hard_requirement",
      region: "邢台市广宗县",
      type: "房屋建筑工程 · 公开招标",
      source: "河北省公共资源交易服务平台",
      updated: "2026-08-26",
      note: "企业安全生产许可证过期 → 演示“资料过期”异常态（硬性一票否决）。",
    },
    {
      id: "KB-2023",
      name: "康保县储能电站 EPC 总承包项目",
      state: "collecting",
      region: "张家口市康保县",
      type: "EPC 总承包 · 公开招标",
      source: "河北省公共资源交易服务平台",
      updated: "2026-08-25",
      note: "企业资料库为空 → 演示“空态”（不可判定/待补材料）。",
    },
  ],

  /* ---------- 主项目（ND-2025）匹配矩阵：20 条，与农大黄金规则一致 ---------- */
  requirements: [
    { id: "NQ-H-001", type: "hard",  category: "资质",     clause: "招标公告 §3.2",
      assertion: "建筑工程施工总承包二级及以上（不接受资质预警/异常企业）",
      match: "satisfied", reason: "建筑工程施工总承包 特级 ≥ 二级（证据：资质证书扫描件 OCR 结构化）",
      evidence: [
        { kind: "企业证据", name: "建筑业企业资质证书（建筑工程施工总承包 特级）", detail: "扫描件 OCR 结构化 · 证书编号 D213035404 · 有效期至 2029-09-11", ref: "E-Q-001", conf: "置信度 0.92 · 第 1 页" },
        { kind: "条款", name: "招标公告 §3.2 投标人资格要求（1）", detail: "须具备建设行政主管部门核发的建筑工程施工总承包二级及以上资质", ref: "clause:NQ-H-001" },
      ] },
    { id: "NQ-H-002", type: "hard",  category: "资质",     clause: "招标公告 §3.4 / 资格审查表 #7",
      assertion: "具备有效的企业安全生产许可证",
      match: "satisfied", reason: "安全生产许可证（冀）JZ安许证字[2005]000049 有效",
      evidence: [
        { kind: "企业证据", name: "安全生产许可证", detail: "（冀）JZ安许证字[2005]000049 · 有效期至 2028-10", ref: "E-Q-002", conf: "置信度 0.85 · 第 1 页" },
        { kind: "条款", name: "招标公告 §3.4 / 资格审查表 #7", detail: "具备有效的安全生产许可证", ref: "clause:NQ-H-002" },
      ] },
    { id: "NQ-H-003", type: "hard",  category: "人员",     clause: "招标公告 §3.7 / 资格审查表 #3/#4",
      assertion: "项目经理：建筑工程二级及以上注册建造师 + 有效 B 证 + 无在建",
      match: "satisfied", reason: "项目经理 M-ND-01（脱敏 张**）专业、等级、B 证、在建状态均满足",
      evidence: [
        { kind: "企业证据", name: "项目经理名录 · M-ND-01", detail: "一级注册建造师（建筑工程）· B 证有效 · 无在建项目 · 可用状态 active", ref: "E-PM-01" },
        { kind: "条款", name: "招标公告 §3.7 / 资格审查表 #3/#4", detail: "拟派项目经理具备建筑工程专业二级及以上注册建造师执业资格，具备有效安全生产考核合格证书（B 类），且未担任其他在建工程的项目经理", ref: "clause:NQ-H-003" },
      ] },
    { id: "NQ-H-004", type: "hard",  category: "人员",     clause: "招标公告 §3.9 / 资格审查表 #6",
      assertion: "项目经理 2025-01-01 至投标截止任意连续 3 个月社保",
      match: "satisfied", reason: "社保连续缴纳 3 个月满足要求",
      evidence: [
        { kind: "企业证据", name: "社保缴纳证明（M-ND-01）", detail: "2025-01 至 2025-03 连续缴纳，与投标截止日窗口匹配", ref: "E-PM-02" },
        { kind: "条款", name: "招标公告 §3.9 / 资格审查表 #6", detail: "提供拟派项目经理在投标单位近 3 个月社保缴纳证明", ref: "clause:NQ-H-004" },
      ] },
    { id: "NQ-H-005", type: "hard",  category: "人员",     clause: "招标公告 §3.10 / 资格审查表 #10",
      assertion: "专职安全生产管理人员 2 人（对应有效 C 证）",
      match: "satisfied", reason: "专职安全生产管理人员 3 人（≥2）且 C 证有效",
      evidence: [
        { kind: "企业证据", name: "岗位证书（安全员 C 证）", detail: "3 人（脱敏名单）· 证书编号/发证机关齐全 · 岗位证书长期有效（公司口径 2026-08-27 确认）", ref: "E-CERT-01~03" },
        { kind: "条款", name: "招标公告 §3.10 / 资格审查表 #10", detail: "专职安全生产管理人员不少于 2 人，具备有效的安全生产考核合格证书（C 类）", ref: "clause:NQ-H-005" },
      ] },
    { id: "NQ-H-006", type: "hard",  category: "人员",     clause: "招标公告 §3.13① / 资格审查表 #12",
      assertion: "技术团队：建筑/给排水/暖通/电气专业各至少 1 名",
      match: "satisfied", reason: "技术团队覆盖专业 建筑/暖通/电气/给排水（要求 4 专业全覆盖）",
      evidence: [
        { kind: "企业证据", name: "技术职称人员清单", detail: "覆盖 4 专业（脱敏：正高/高级职称若干）· 技术职称 2962 人盘点", ref: "E-CERT-04" },
        { kind: "条款", name: "招标公告 §3.13① / 资格审查表 #12", detail: "项目技术负责人及各专业技术人员配备齐全", ref: "clause:NQ-H-006" },
      ] },
    { id: "NQ-H-007", type: "hard",  category: "财务",     clause: "招标公告 §3.13② / 资格审查表 #11",
      assertion: "提供 2022/2023/2024 年度审计报告（或成立不足企业银行资信证明+基本账户证明）",
      match: "unverifiable", reason: "缺少 2022/2023/2024 年度财务审计报告或银行资信证明",
      evidence: [
        { kind: "待补材料", name: "财务审计报告（2022/2023/2024）或银行资信证明", detail: "素材缺口（当年必有，待公司补充后重跑可复现满分路径）· 处置：进入待补队列，不推断满足", ref: "pending:NQ-H-007" },
        { kind: "条款", name: "招标公告 §3.13② / 资格审查表 #11", detail: "提供近三年（2022/2023/2024）经审计的财务报告", ref: "clause:NQ-H-007" },
      ] },
    { id: "NQ-H-008", type: "hard",  category: "信用",     clause: "招标公告 §3.11",
      assertion: "投标人及拟派项目经理自 2022-09-01 起未被列入失信被执行人",
      match: "satisfied", reason: "信用三查证据完整且无失信记录",
      evidence: [
        { kind: "企业证据", name: "信用核查报告", detail: "信用中国 + 法院失信被执行人查询 · 投标人及项目经理均无记录", ref: "E-CR-01" },
        { kind: "条款", name: "招标公告 §3.11", detail: "投标人及其拟派项目经理未被列入失信被执行人名单", ref: "clause:NQ-H-008" },
      ] },
    { id: "NQ-H-009", type: "hard",  category: "联合体",   clause: "招标公告 §3.3",
      assertion: "不接受联合体投标",
      match: "satisfied", reason: "已声明独立投标",
      evidence: [
        { kind: "企业证据", name: "联合体投标声明", detail: "投标函中声明独立投标，不组成联合体", ref: "E-BID-01" },
        { kind: "条款", name: "招标公告 §3.3", detail: "本项目不接受联合体投标", ref: "clause:NQ-H-009" },
      ] },
    { id: "NQ-H-010", type: "hard",  category: "响应性",   clause: "须知 3.3.1 / 投标函附录",
      assertion: "投标有效期 120 日历天",
      match: "satisfied", reason: "响应性文件证据已核验",
      evidence: [
        { kind: "企业证据", name: "投标函附录", detail: "投标有效期承诺 120 日历天", ref: "E-BID-02" },
        { kind: "条款", name: "投标人须知 3.3.1 / 投标函附录", detail: "投标有效期 120 日历天", ref: "clause:NQ-H-010" },
      ] },
    { id: "NQ-H-011", type: "hard",  category: "保证金",   clause: "须知 3.4.1",
      assertion: "保证金 200,000 元，银行汇票/电汇/支票/银行保函/电子保函/保证保险，截止前到账",
      match: "satisfied", reason: "保证金金额、形式和到账凭证满足要求",
      evidence: [
        { kind: "企业证据", name: "投标保函（OCR 复核）", detail: "中国建设银行保定竞秀支行 · 编号 2025101361086000000002 · 贰拾万元整 · 连带责任保证 · 有效期至 2026-03-15", ref: "E-BOND-01", conf: "OCR 复核通过 · 3 页" },
        { kind: "条款", name: "投标人须知 3.4.1", detail: "投标保证金 20 万元，可采用银行汇票、电汇、支票、银行保函、电子保函或保证保险", ref: "clause:NQ-H-011" },
      ] },
    { id: "NQ-H-012", type: "hard",  category: "响应性",   clause: "须知 3.2.4 / 评标办法",
      assertion: "报价不超过最高投标限价 12,471.590889 万元，唯一报价+格式签章合规",
      match: "satisfied", reason: "报价 121,598,056.76 元 ≤ 最高投标限价 124,715,908.89 元",
      evidence: [
        { kind: "企业证据", name: "投标报价（商务标）", detail: "总报价 121,598,056.76 元 · 唯一报价 · 格式与签章合规", ref: "E-PRICE-01" },
        { kind: "条款", name: "投标人须知 3.2.4 / 评标办法", detail: "投标报价不得超过最高投标限价 12471.590889 万元", ref: "clause:NQ-H-012" },
      ] },
    { id: "NQ-S-001", type: "scored", category: "技术标",  clause: "第三章 四(2) 技术标暗标+明标",
      assertion: "技术标 30%（暗标 80 分 + 明标 20 分）",
      match: "manual_review", reason: "待内部质量评审，不能计入内部满分", max_score: 100,
      evidence: [
        { kind: "人工复核", name: "内部质量评审（主观项）", detail: "按公司内部评审流程打分后回填；评审前不计入“内部满分”", ref: "review:NQ-S-001" },
        { kind: "条款", name: "第三章 评标办法 四(2)", detail: "技术标 30 分（暗标 80 分 + 明标 20 分）", ref: "clause:NQ-S-001" },
      ] },
    { id: "NQ-S-002", type: "scored", category: "商务标/信用", clause: "第三章 四(3)(4)",
      assertion: "商务标 64% + 信用 6%（基准价 A×C，信用门槛 85 分）",
      match: "manual_review", reason: "评分公式或输入不完整，需人工复核", max_score: 70,
      evidence: [
        { kind: "人工复核", name: "评分公式/报价输入", detail: "基准价 A×C 与报价参数待人工复核确认后计算", ref: "review:NQ-S-002" },
        { kind: "条款", name: "第三章 评标办法 四(3)(4)", detail: "商务标 64 分、信用 6 分（信用评价得分低于 85 分不得推荐为中标候选人）", ref: "clause:NQ-S-002" },
      ] },
    { id: "NQ-S-003", type: "scored", category: "技术标明标/类似业绩", clause: "第三章 四(5) 技术标明标评审内容 2",
      assertion: "投标人具有一项及以上类似项目：2022-09-01 至开标日单体建筑面积 ≥2 万㎡ 房屋建筑类业绩",
      match: "satisfied", reason: "投标人类似业绩满足：石家庄学院实训基地项目施工 34,199.93㎡，竣工 2022-09-30", max_score: 2.5, score: 2.5,
      evidence: [
        { kind: "企业证据", name: "类似业绩（投标人）", detail: "石家庄学院实训基地项目施工 · 单体 34,199.93㎡ · 竣工 2022-09-30 · 房屋建筑类 · 中标通知书+合同+竣工验收报告", ref: "E-PF-01" },
        { kind: "条款", name: "第三章 四(5) 评审内容 2 备注 c", detail: "2022-09-01 起单体建筑面积 ≥2 万㎡ 的房屋建筑类业绩，主体结构不可通用", ref: "clause:NQ-S-003" },
      ] },
    { id: "NQ-S-004", type: "scored", category: "技术标明标/类似业绩", clause: "第三章 四(5) 技术标明标评审内容 3",
      assertion: "拟投入项目经理具有一项及以上类似项目（限以项目经理身份参与，口径同 NQ-S-003）",
      match: "satisfied", reason: "项目经理类似业绩满足：隆尧县医院建设项目工程施工 59,633.29㎡，竣工 2022-10-20", max_score: 2.5, score: 2.5,
      evidence: [
        { kind: "企业证据", name: "类似业绩（项目经理 M-ND-01）", detail: "隆尧县医院建设项目工程施工 · 单体 59,633.29㎡ · 竣工 2022-10-20 · 以项目经理身份参与", ref: "E-PF-02" },
        { kind: "条款", name: "第三章 四(5) 评审内容 3", detail: "拟投入项目经理具有一项及以上类似项目业绩（限项目经理身份）", ref: "clause:NQ-S-004" },
      ] },
    { id: "NQ-A-001", type: "action", category: "报名/招标文件获取", clause: "招标公告 §4.1", stage: "approval_ready",
      assertion: "完成招标文件获取（2025-09-30 ~ 10-13）", match: "satisfied", reason: "动作状态=completed" },
    { id: "NQ-A-002", type: "action", category: "保证金到账", clause: "须知 3.4.1", stage: "approval_ready",
      assertion: "保证金 20 万到账（截止 2025-10-30 09:00 前）", match: "satisfied", reason: "动作状态=completed" },
    { id: "NQ-A-003", type: "action", category: "递交", clause: "招标公告 §5.1", stage: "submission_ready",
      assertion: "递交投标文件（2025-10-30 09:00 前）", match: "satisfied", reason: "动作状态=completed" },
    { id: "NQ-A-004", type: "action", category: "开标", clause: "须知 4.1.5", stage: "opened",
      assertion: "完成开标/解密", match: "satisfied", reason: "动作状态=completed" },
  ],

  /* 主项目准入摘要（与 nongda_match_result.json 一致） */
  admission: {
    qualification: "pending",          // 资格/响应性核查：11/12 满足，1 待补
    hard_satisfied: 11,
    hard_total: 12,
    scoring: "not_full",               // 当前评分：客观 5/5 可复算，主观 2 项待评审
    objective_score: 5,
    objective_max: 5,
    readiness: "ready",                // 审批前动作 4/4 completed
    action_done: 4,
    action_total: 4,
    eligible: false,                   // 内部准入
    state: "blocked_missing_data",
    missing: [
      { req: "NQ-H-007", text: "缺少 2022/2023/2024 年度财务审计报告或银行资信证明（素材缺口，待公司补充）" },
    ],
    review: [
      { req: "NQ-S-001", text: "技术标主观项：待内部质量评审，评审前不计入内部满分" },
      { req: "NQ-S-002", text: "商务标/信用评分公式或报价输入不完整，需人工复核" },
    ],
  },

  /* 满分构造口径（仅演示审批流；approval_demo_result.json 注明“不代表实际审批”） */
  full_score_demo: {
    note: "以下为补齐 NQ-H-007 材料并完成内部评审后的构造口径，仅用于演示审批/豁免/审计流程，不代表对农大项目的实际审批。",
    state: "pending_bid_approval",
    admission_ref: "admission:ND-2025:v1.0.0",
  },

  /* 审批记录（approval_demo_result.json 脱敏） */
  approvals: [
    {
      id: "ND-REAL-1",
      project: "ND-2025",
      status: "rejected_create",
      note: "非满分创建审批被拒绝：内部准入未满足（待补 NQ-H-007 + 复核 2 项）→ 只有满分才可进入投标审批",
    },
    {
      id: "ND-REAL-2",
      project: "ND-2025",
      status: "approved",
      approver: "经营负责人",
      decided_at: "2026-08-28 10:30",
      comment: "同意投标",
      basis: "admission:ND-2025:v1.0.0",
      audit: [
        { at: "created", actor: "经营负责人", action: "create_approval", basis: "admission:ND-2025:v1.0.0", outcome: "pending_bid_approval" },
        { at: "2026-08-28 10:30", actor: "经营负责人", action: "approve", basis: "admission:ND-2025:v1.0.0", outcome: "approved_for_bidding" },
      ],
    },
    {
      id: "ND-REAL-3",
      project: "ND-2025",
      status: "waived",
      approver: "经营负责人",
      decided_at: "2026-08-28 11:30",
      comment: "带豁免批准，跟进材料",
      waivers: [
        { id: "W-ND-1", authorizer: "经营负责人", reason: "审计报告在途（已受理）", evidence: "受理回执 E-ND-2025-01", valid_until: "2026-09-30", covered: ["NQ-H-007"] },
      ],
      audit: [
        { at: "created", actor: "经营负责人", action: "create_approval", basis: "admission:ND-2025", outcome: "pending_bid_approval" },
        { at: "2026-08-28", actor: "经营负责人", action: "add_waiver", basis: "受理回执 E-ND-2025-01", outcome: "waiver:W-ND-1" },
        { at: "2026-08-28 11:30", actor: "经营负责人", action: "decide_waived", basis: "waivers=[W-ND-1]", outcome: "approved_with_waiver" },
      ],
    },
    {
      id: "ND-REAL-4",
      project: "ND-2025",
      status: "blocked_waiver_expired",
      approver: "经营负责人",
      comment: "（豁免 W-ND-2 已过期，系统自动回阻断）",
      waivers: [
        { id: "W-ND-2", authorizer: "经营负责人", reason: "材料在途", evidence: "E-ND-2025-02", valid_until: "2026-08-01", covered: [] },
      ],
      audit: [
        { at: "created", actor: "经营负责人", action: "create_approval", basis: "admission:ND-2025", outcome: "pending_bid_approval" },
        { at: "2026-07-28", actor: "经营负责人", action: "add_waiver", basis: "E-ND-2025-02", outcome: "waiver:W-ND-2" },
        { at: "2026-08-28", actor: "system", action: "expire_waivers", basis: "valid_until 已过", outcome: "blocked_waiver_expired" },
      ],
    },
  ],

  /* 公司资料选择页数据（脱敏盘点口径，05-项目现状 §1.2） */
  company_data: {
    qualifications: [
      { name: "建筑工程施工总承包", level: "特级", valid_until: "2029-09-11", status: "active", evidence: "资质证书 D213035404（OCR）", selected: true },
      { name: "市政公用工程施工总承包", level: "一级", valid_until: "2028-12-22", status: "active", evidence: "资质证书（OCR）", selected: true },
      { name: "公路工程施工总承包", level: "一级", valid_until: "2029-06-30", status: "active", evidence: "资质证书（OCR）", selected: true },
      { name: "机电工程施工总承包", level: "一级", valid_until: "2029-06-30", status: "active", evidence: "资质证书（OCR）", selected: true },
    ],
    safety_license: { name: "安全生产许可证", no: "（冀）JZ安许证字[2005]000049", valid_until: "2028-10", status: "active" },
    managers: [
      { id: "M-ND-01", name: "张**", specialty: "建筑工程", cert: "一级注册建造师", b_cert: "有效", social_security: "已核验（连续 3 个月）", active_project: "无在建", status: "active", role: "主推荐", similar_perf: "隆尧县医院 59,633.29㎡（2022-10-20）" },
      { id: "M-ND-02", name: "李**", specialty: "建筑工程", cert: "二级注册建造师", b_cert: "有效", social_security: "已核验", active_project: "无在建", status: "active", role: "备选", similar_perf: "待补业绩证据" },
      { id: "M-ND-03", name: "王**", specialty: "市政公用工程", cert: "一级注册建造师", b_cert: "待核实", social_security: "待核验", active_project: "有在建（工期至 2027-03）", status: "unavailable", role: "—", similar_perf: "—" },
    ],
  },

  /* 异常态演示（F010 §7：5 类） */
  exceptions: [
    {
      id: "empty",
      title: "空态：企业资料库为空",
      desc: "康保储能 EPC 项目：未导入任何企业资料 → 匹配页输出“不可判定/待补材料”，不得推断满足。",
      demo: "empty_demo",
    },
    {
      id: "expired",
      title: "资料过期：资质/证书过期",
      desc: "广宗葫芦中学项目：安全生产许可证已过期 → expired 徽标 + 硬性一票否决（blocked_hard_requirement），不计分。",
      demo: "expired_demo",
    },
    {
      id: "parse_fail",
      title: "解析失败/部分解析",
      desc: "阜平高铁片区管网项目：评分办法解析不完整 → manual_review，可跳转人工复核，不静默跳过。",
      demo: "parse_demo",
    },
    {
      id: "permission",
      title: "权限不足（403 模拟）",
      desc: "非审批人（投标专员/数据管理员/法务）访问审批页 → 403 模拟页。右上角切换角色体验。",
      demo: "permission_demo",
    },
    {
      id: "no_manager",
      title: "无合格可用项目经理",
      desc: "构造项目：全部候选经理不满足硬条件（专业/等级/B 证/在建冲突任一不满足或无法核验）→ 评分面板显示“无合格可用项目经理”，准入判定阻断。",
      demo: "no_manager_demo",
    },
  ],
};

/* 无合格项目经理演示数据 */
const NO_MANAGER_DEMO = {
  project: "（构造）某房建项目 · 无合格经理演示",
  candidates: [
    { id: "M-X-01", name: "赵**", fail: "专业为市政公用工程，不满足建筑工程", tone: "danger" },
    { id: "M-X-02", name: "孙**", fail: "二级注册建造师 + 无在建，但 B 证信息待核实（无法核验，不计入）", tone: "warn" },
    { id: "M-X-03", name: "周**", fail: "一级注册建造师 + B 证有效，但有在建项目（工期冲突）", tone: "danger" },
  ],
  conclusion: "候选 0 人通过硬条件核查 → 准入判定阻断（待补/不满足），不生成“无中生有”的合格经理。",
};