# 招投标 Schema 骨架树（生成稿）

> 生成时间：2026-08-26 15:41:02
> 树节点校验：通过

## 一、项目主卡字段清单

| 中文字段 | 内部字段 key | 类型 | 必填 | 来源 |
|---|---|:--:|:--:|---|
| 卡片ID | `card_id` | string | 是 | 系统生成 |
| 项目ID | `project_id` | string | 是 | 系统生成/聚合 |
| 项目名称 | `project_name` | string | 是 | 公告原文 |
| 招标编号 | `tender_no` | string | 否 | 公告原文 |
| 招标人 | `tenderee` | string | 是 | 公告原文 |
| 招标代理机构 | `agency` | string | 否 | 公告原文 |
| 地区 | `region` | string | 是 | 公告原文 |
| 项目类型 | `project_type` | enum | 是 | 公告原文 |
| 预算金额 | `budget_amount` | decimal | 否 | 公告原文 |
| 最高投标限价 | `ceiling_price` | decimal | 否 | 公告原文 |
| 投标保证金金额 | `bid_bond_amount` | decimal | 否 | 公告原文 |
| 报名截止时间 | `deadline_signup` | datetime | 否 | 公告原文 |
| 投标截止时间 | `deadline_bid` | datetime | 是 | 公告原文 |
| 开标时间 | `open_date` | datetime | 否 | 公告原文 |
| 资格要求 | `qualification_requirements` | ref[] | 否 | 子卡引用 |
| 评分规则 | `scoring_rules` | ref[] | 否 | 子卡引用 |
| 公司投标优势 | `company_advantage` | ref[] | 否 | 占位=待企业资料导入 |
| 来源链接 | `source_links` | url[] | 是 | 公告原文 |
| 置信度 | `confidence` | enum | 是 | 来源映射 |
| 状态 | `status` | enum | 是 | 项目状态 |

## 二、子卡类型清单

| 类型 | key | 说明 |
|---|---|---|
| 基本信息 | `basic` | 主卡摘要之外的补充信息 |
| 时间线 | `time` | 报名/答疑/投标/开标/保证金等时间点 |
| 资格要求 | `qualification` | 资质/人员/业绩/财务/信用等 |
| 评分规则 | `scoring` | 技术/商务/资信权重与废标条款 |
| 金额限价 | `amount` | 预算/限价/保证金/履约保证金 |
| 合规约束 | `compliance` | 暗标/CA/递交方式/原件备查等 |
| 清单式要求 | `requirement` | 资格/废标清单，逐条引用条款 |
| 投标动作清单 | `checklist` | 报名/CA/保证金/递交/开标动作 |
| 公司投标优势 | `advantage` | 企业私有库数据与招标要求匹配 |

## 三、公司投标优势子卡字段（占位）

| 中文字段 | key | 类型 | 必填 | 来源 |
|---|---|:--:|:--:|---|
| 优势卡ID | `advantage_id` | string | 是 | 自动生成 |
| 项目ID | `project_id` | string | 是 | 回链主卡 |
| 优势名称 | `advantage_name` | string | 否 | 占位 |
| 对应招标条款 | `tender_clause_ref` | ref[] | 否 | 回链资格/评分子卡 |
| 匹配依据 | `match_basis` | enum | 否 | 占位 |
| 匹配结果 | `match_result` | enum | 否 | 占位 |
| 支撑材料 | `evidence_files` | file[] | 否 | 待提供 |
| 有效期 | `valid_until` | date | 否 | 占位 |
| 来源库 | `source_library` | enum | 否 | 占位 |
| 状态 | `status` | enum | 是 | 占位 |
| 备注 | `remark` | string | 否 | 人工补充 |

> 占位规则：企业私有库资料导入前，全部置为“待企业资料导入”，禁止用公开公告编造内部优势。

## 四、骨架树结构

- 1 域识别与路由
  - 1.1 公告类型路由
    - 1.1.1 公告类型（`announcement_type` / enum / 必填）
  - 1.2 域边界
    - 1.2.1 项目类型（`project_type` / enum / 必填）
    - 1.2.2 企业资质边界
      - 1.2.2.1 建筑工程施工总承包特级
      - 1.2.2.2 建筑工程设计甲级
      - 1.2.2.3 人防工程设计甲级
      - 1.2.2.4 质量管理体系认证证书（ISO 9001）
      - 1.2.2.5 环境管理体系认证证书（ISO 14001）
      - 1.2.2.6 职业健康安全管理体系认证证书
      - 1.2.2.7 信息安全管理体系认证证书（ISO 27001）
      - 1.2.2.8 知识产权管理体系认证证书
      - 1.2.2.9 社会责任管理体系认证证书（2024-08-29~2027-08-29）
      - 1.2.2.10 安全生产许可证
    - 1.2.3 资质匹配规则
  - 1.3 平台注册表
    - 1.3.1 国家级 L1
      - 1.3.1.1 全国公共资源交易平台 ggzy.gov.cn
      - 1.3.1.2 中国招标投标公共服务平台 cebpubservice.com
      - 1.3.1.3 中国政府采购网 ccgp.gov.cn（京津冀走分站）
    - 1.3.2 北京 L1
      - 1.3.2.1 北京公共资源交易服务平台 ggzyfw.beijing.gov.cn ★
      - 1.3.2.2 北京市政府采购网 ccgp-beijing.gov.cn
      - 1.3.2.3 北京建设工程交易系统 zhjy.bcactc.com
    - 1.3.3 天津 L1
      - 1.3.3.1 天津市公共资源交易平台 ggzy.zwfwb.tj.gov.cn ★
      - 1.3.3.2 天津市政府采购网 ccgp-tianjin.gov.cn
    - 1.3.4 河北 L1
      - 1.3.4.1 河北省公共资源交易服务平台 szj.hebei.gov.cn/hbggfwpt/ ★（旧域名已注销）
      - 1.3.4.2 河北省政府采购网 ccgp-hebei.gov.cn ★
      - 1.3.4.3 惠招标（河北交投 ebidding.hebtig.com）
      - 1.3.4.4 雄安新区公共资源交易服务平台
    - 1.3.5 河北 11 市 L2（逐个合规评估）
      - 1.3.5.1 石家庄 sjzsggzyjyzx.org.cn
      - 1.3.5.2 唐山 ggzyjy.xzspj.tangshan.gov.cn
      - 1.3.5.3 邯郸 ggzy.hd.gov.cn
      - 1.3.5.4 秦皇岛 qhdggzy.cn
      - 1.3.5.5 承德 szj.chengde.gov.cn/cdsggzy/
      - 1.3.5.6 衡水 hsggzy.hengshui.gov.cn
      - 1.3.5.7 沧州 xzsp.cangzhou.gov.cn
      - 1.3.5.8 邢台 60.6.198.121:8888（IP直连）
      - 1.3.5.9 保定/廊坊（挂省级 inforc=1306/1310）
    - 1.3.6 第三方 L0（待合同）
      - 1.3.6.1 千里马 qianlima.com
      - 1.3.6.2 剑鱼标讯 jianyu360.com
      - 1.3.6.3 采招网 bidcenter.com.cn（≠比地）
      - 1.3.6.4 比地招标 bidizhaobiao.cn
    - 1.3.7 圈定标准与非公告源
- 2 招标事实维度
  - 2.1 项目基本信息
    - 2.1.1 项目名称（`project_name` / string / 必填）
    - 2.1.2 招标编号（`tender_no` / string / 选填）
    - 2.1.3 招标人（`tenderee` / string / 必填）
    - 2.1.4 招标代理机构（`agency` / string / 选填）
    - 2.1.5 地区（`region` / string / 必填）
    - 2.1.6 行业专业（`industry` / string / 选填）
    - 2.1.7 招标方式（`procurement_method` / enum / 选填）
    - 2.1.8 来源平台（`source_platform` / string / 必填）
    - 2.1.9 来源链接（`source_url` / url / 必填）
    - 2.1.10 发布时间（`publish_time` / datetime / 选填）
  - 2.2 时间线
    - 2.2.1 报名/文件获取截止时间（`deadline_signup` / datetime / 选填）
    - 2.2.2 答疑/澄清截止时间（`deadline_clarify` / datetime / 选填）
    - 2.2.3 投标截止时间（`deadline_bid` / datetime / 必填）
    - 2.2.4 开标时间与地点（`open_date` / datetime / 选填）
    - 2.2.5 保证金到账截止时间（`deadline_bond` / datetime / 选填）
    - 2.2.6 中标公示时间（`award_time` / datetime / 选填）
  - 2.3 资格要求
    - 2.3.1 资质要求（`qualification` / string / 选填）
    - 2.3.2 人员要求（`personnel` / string / 选填）
    - 2.3.3 业绩要求（`performance` / string / 选填）
    - 2.3.4 财务要求（`finance` / string / 选填）
    - 2.3.5 信用要求（`credit` / string / 选填）
    - 2.3.6 安全生产许可证（`safety_license` / string / 选填）
    - 2.3.7 联合体要求（`joint_venture` / string / 选填）
    - 2.3.8 其他资格（`other_qualification` / string / 选填）
    - 2.3.9 审查方式（`review_method` / enum / 选填）
  - 2.4 评分规则
    - 2.4.1 评审办法（`review_standard` / enum / 选填）
    - 2.4.2 商务评分（`business_scoring` / string / 选填）
    - 2.4.3 技术评分（`technical_scoring` / string / 选填）
    - 2.4.4 资信评分（`credit_scoring` / string / 选填）
    - 2.4.5 废标条款（`disqualification_clauses` / string[] / 选填）
    - 2.4.6 澄清/补正规则（`clarification_rules` / string / 选填）
  - 2.5 金额与限价
    - 2.5.1 预算金额（`budget_amount` / decimal / 选填）
    - 2.5.2 最高投标限价（`ceiling_price` / decimal / 选填）
    - 2.5.3 投标保证金（`bid_bond_amount` / decimal / 选填）
    - 2.5.4 履约/质量保证金（`performance_bond` / decimal / 选填）
    - 2.5.5 币种与金额单位（`currency` / string / 选填）
    - 2.5.6 计价方式（`pricing_method` / string / 选填）
  - 2.6 合规约束
    - 2.6.1 暗标要求（`dark_bid` / string / 选填）
    - 2.6.2 CA/电子签章（`ca_requirement` / string / 选填）
    - 2.6.3 递交方式（`submission_method` / string / 选填）
    - 2.6.4 授权委托书（`authorization_requirement` / string / 选填）
    - 2.6.5 踏勘/标前会（`site_visit` / string / 选填）
    - 2.6.6 其他合规条款（`other_compliance` / string / 选填）
- 3 来源可信度与质量闸门
  - 3.1 数据源分级（`source_level` / enum / 必填）
  - 3.2 来源评分五维
    - 3.2.1 官方性（`officialness` / enum / 选填）
    - 3.2.2 时效性（`timeliness` / string / 选填）
    - 3.2.3 完整性（`completeness` / string / 选填）
    - 3.2.4 一致性（`consistency` / string / 选填）
    - 3.2.5 可追溯性（`traceability` / string / 选填）
  - 3.3 置信度（`confidence` / enum / 必填）
  - 3.4 质量闸门（`quality_gate` / enum / 必填）
- 4 项目事实卡规范
  - 4.1 事实类型
    - 4.1.1 基本信息
    - 4.1.2 时间线
    - 4.1.3 资格要求
    - 4.1.4 评分规则
    - 4.1.5 金额限价
    - 4.1.6 合规约束
    - 4.1.7 清单式要求
    - 4.1.8 投标动作清单
    - 4.1.9 公司投标优势
  - 4.2 项目主卡字段
  - 4.3 子卡挂接规则
  - 4.4 纯事实红线
- 5 采集与合规
  - 5.1 采集模式
  - 5.2 robots（预检留痕不阻断，ADR-003）
  - 5.3 UA透明
  - 5.4 个人信息
  - 5.5 数据源分级
  - 5.6 合规红线
- 6 Pipeline 状态机 + 门禁
  - 6.1 G0 采集门禁
  - 6.2 G1 清洗门禁
  - 6.3 G2 事实卡门禁
  - 6.4 G3 情报库门禁
  - 6.5 G4 推送门禁
  - 6.6 G5 归档门禁
  - 6.7 G6′ 校准门禁
  - 6.8 G3.5 匹配门禁
- 7 去重规则
  - 7.1 L1 精确去重
  - 7.2 L2 近似去重
  - 7.3 L3 合并去重
  - 7.4 变更/澄清关联
- 8 QA 标准
  - 8.1 逐字段核对
  - 8.2 抽样规则
  - 8.3 红线检查
  - 8.4 溯源检查
  - 8.5 缺失检查
- 9 资格与资源核查域
  - 9.1 企业资料匹配
    - 9.1.1 矩阵ID（`matrix_id` / string / 必填）
    - 9.1.2 要求引用（`tender_clause_ref` / ref[] / 必填）
    - 9.1.3 企业证据引用（`evidence_refs` / ref[] / 必填）
    - 9.1.4 匹配结果（`match_result` / enum / 必填）
    - 9.1.5 计分（`score` / decimal / 选填）
    - 9.1.6 满分值（`max_score` / decimal / 必填）
    - 9.1.7 缺失项（`missing_items` / ref[] / 选填）
    - 9.1.8 判定时点（`as_of` / datetime / 必填）
    - 9.1.9 判定依据（`match_reason` / object / 必填）
  - 9.2 三类要求
    - 9.2.1 硬性要求 hard_requirement
    - 9.2.2 计分要求 scored_requirement
    - 9.2.3 动作要求 action_requirement
  - 9.3 项目经理匹配
    - 9.3.1 经理ID（`manager_id` / string / 必填）
    - 9.3.2 姓名/脱敏展示名（`display_name` / string / 必填）
    - 9.3.3 所属组织（`organization` / string / 必填）
    - 9.3.4 专业（`specialty` / string / 必填）
    - 9.3.5 注册证书（`reg_cert_type` / string / 必填）
    - 9.3.6 注册编号（`reg_cert_no` / string / 必填）
    - 9.3.7 证书等级（`cert_level` / string / 必填）
    - 9.3.8 证书有效期（`cert_valid_until` / date / 必填）
    - 9.3.9 继续教育/安全证书状态（`edu_safety_status` / enum / 必填）
    - 9.3.10 可担任项目类型（`eligible_project_types` / string[] / 必填）
    - 9.3.11 地区限制（`region_restriction` / string / 选填）
    - 9.3.12 历史业绩（`performance_refs` / ref[] / 选填）
    - 9.3.13 当前在建项目（`active_projects` / ref[] / 选填）
    - 9.3.14 预计可用日期（`expected_available_at` / date / 选填）
    - 9.3.15 可用状态（`availability` / enum / 必填）
    - 9.3.16 信用/处罚状态（`credit_penalty_status` / string / 选填）
    - 9.3.17 证据文件引用（`evidence_refs` / ref[] / 必填）
    - 9.3.18 最后核验时间（`verified_at` / datetime / 选填）
    - 9.3.19 资料责任人（`data_owner` / string / 必填）
    - 9.3.20 经理状态（`status` / enum / 必填）
    - 9.3.21 推荐角色（`recommendation_role` / enum / 选填）
    - 9.3.22 硬条件验证
  - 9.4 规则版本
    - 9.4.1 规则集ID（`rule_set_id` / string / 必填）
    - 9.4.2 规则版本（`rule_version` / string / 必填）
    - 9.4.3 生效日期（`effective_from` / date / 必填）
    - 9.4.4 创建人（`created_by` / string / 必填）
  - 9.5 四类结论与内部满分准入
    - 9.5.1 资格/响应性核查（`qualification_result` / object / 必填）
    - 9.5.2 当前评分结果（`scoring_result` / object / 必填）
    - 9.5.3 投标准备度（`operational_readiness` / object / 必填）
    - 9.5.4 内部准入结论（`internal_admission_result` / object / 必填）
    - 9.5.5 是否可送人工审批（`internal_admission_eligible` / boolean / 必填）
    - 9.5.6 结果新鲜度（`result_freshness` / enum / 必填）
    - 9.5.7 阻断/待补/复核项（`decision_items` / object[] / 选填）
- 10 准入状态机域
  - 10.1 准入状态
    - 10.1.1 准入状态（`admission_status` / enum / 必填）
  - 10.2 状态迁移规则
  - 10.3 人工豁免
    - 10.3.1 豁免ID（`waiver_id` / string / 必填）
    - 10.3.2 授权人（`authorizer` / string / 必填）
    - 10.3.3 原因（`reason` / string / 必填）
    - 10.3.4 证据（`evidence_refs` / ref[] / 必填）
    - 10.3.5 有效期（`valid_until` / date / 必填）
    - 10.3.6 审批时间（`approved_at` / datetime / 必填）
    - 10.3.7 覆盖的阻断项（`covered_items` / ref[] / 必填）
  - 10.4 审批记录
    - 10.4.1 审批ID（`approval_id` / string / 必填）
    - 10.4.2 项目ID（`project_id` / string / 必填）
    - 10.4.3 审批人（`approver` / string / 必填）
    - 10.4.4 决策（`decision` / enum / 必填）
    - 10.4.5 决策时间（`decided_at` / datetime / 必填）
    - 10.4.6 意见（`comment` / string / 选填）
    - 10.4.7 关联准入结果（`admission_result_ref` / ref[] / 必填）
    - 10.4.8 审计要求
- 11 数据治理域
  - 11.1 Material 契约字段
    - 11.1.1 材料ID（`material_id` / string / 必填）
    - 11.1.2 材料类型（`material_type` / enum / 必填）
    - 11.1.3 来源类型（`source_type` / enum / 必填）
    - 11.1.4 归属类型（`owner_type` / enum / 必填）
    - 11.1.5 密级（`classification` / enum / 必填）
    - 11.1.6 权限范围（`permission_scope` / enum / 必填）
    - 11.1.7 原文哈希（`content_hash` / string / 必填）
    - 11.1.8 版本（`version` / int / 必填）
    - 11.1.9 导入时间（`imported_at` / datetime / 必填）
    - 11.1.10 有效期（`valid_until` / date / 选填）
    - 11.1.11 解析状态（`parse_status` / enum / 必填）
    - 11.1.12 证据文件引用（`evidence_refs` / ref[] / 选填）
    - 11.1.13 数据责任人（`data_owner` / string / 必填）
    - 11.1.14 最后核验时间（`verified_at` / datetime / 选填）
    - 11.1.15 状态（`status` / enum / 必填）
  - 11.2 权限矩阵
  - 11.3 隔离与不可变约束
  - 11.4 缺失/过期处置
