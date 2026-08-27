---
type: schema
domain: 招投标（某建设集团 · 投标信息收集智能体）
version: 0.5.1
status: active
generated_by: domain-schema-generator-v2
skeleton_source: 投标智能体-智库底座复用方案_v1.md §四（招投标 Schema 骨架树细化版 v1）+ PRD_v1.md + ADR-001
audit_iterations: 3
placeholder_hits: 0
coverage_rate: "125/125"
passed_gate: 2026-08-13 / 2026-08-25（R002）/ 2026-08-25（R003）
change_status: v0.5.1 骨架树已重新生成并通过结构校验；行业黄金样本语义验证待执行
---

# 招投标 · 域操作规范（schema）

> 「信息搜集 Agent 的宪法」——修改此文件即修改 Agent 行为。
> 本 schema 定义从**采集 → 清洗 → 事实卡 → 项目情报库 → 匹配与准入 → 人工审批 → 反馈校准**的完整规则，全部围绕「纯事实、可溯源、零分析断语」红线。
> 与智库 schema 的关键差异：**无分析层**（无 7 维分析框架、无 SCQA、无报告撰写标准、无作战地图）。公开情报层只产出结构化情报，不做投/不投判断。
> 新增边界（ADR-001）：资格与资源核查层（§十一）允许**规则化匹配与计分**——但仅限可配置规则和证据化结果；资格/响应性、当前评分、投标准备度和内部准入必须分别输出。投标审批（§十二）必须由企业负责人完成，系统永不自动投标/报价。
> 关联 PRD：《投标信息收集智能体-PRD_v1.md》为开发基线，字段 key、采集模式、门禁以 PRD 为准并同步更新；范围变化以 ADR-001 为准。

---

## 一、域识别与路由

### 1.1 公告类型路由 `[table]`

公告类型决定后续流水线分支（G0 采集门禁必填字段 `announcement_type`）：

| 公告类型 | 处理分支 | 是否进入情报库 | 备注 |
|---------|---------|:--:|------|
| 招标公告 | 采集→清洗→事实卡→主卡（新建 project_id） | ✅ | 全流程主线 |
| 资格预审公告 | 采集→事实卡→挂接主卡（同 project_id） | ✅ | 资格预审项目专用 |
| 变更/澄清公告 | 采集→事实卡→**挂接原项目**，不覆盖历史版本 | ✅ | 关联 7.4 |
| 中标候选人公示 | 采集→事实卡→更新主卡状态 | ✅ | 关联 6.4 状态机 |
| 中标结果公示 | 采集→事实卡→更新主卡状态 awarded | ✅ | 关联 6.4 状态机 |
| 流标/终止公告 | 采集→事实卡→主卡状态 closed | ✅ | 关联 G5 归档门禁 |

> 路由判定依据：以公告原文载明类型为准；正文与标题不一致时以正文为准，并标注 `note: 标题与正文类型不一致`。

### 1.2 域边界 `[table + rule]`

| 属于本域（采集） | 不属于本域（剔除） |
|---------|-----------|
| 依法必须招标的工程建设/货物/服务类公告 | 非招标采购：询价、比选、竞争性谈判、单一来源 |
| 招标公告、资格预审、变更澄清、中标候选人/结果公示、流标终止公告 | 企业内部零星采购、电商直采 |
| 公开招标与邀请招标公告 | 付费墙/会员专区/需登录可见内容（合规 R2） |
| 京津冀 + 国家级平台公开信息 | 非公开渠道信息（微信群/内部文件） |
| 公告中公开的联系人/电话/邮箱（内部使用，30 天脱敏） | 未公开发布的项目信息 |

> **边界模糊时的处理规则**：以公告载明的采购方式/法定依据为准（如"竞争性谈判"字样→剔除）；模糊时降级**人工复核**，标记 `route_status: manual_review`，不自动放行。

### 1.3 企业资质边界（识别条件）`[table]`

某建设集团现有资质为项目匹配识别条件（来源：复用方案 §四 1.2.2，待投标专员补全）：

| # | 资质/证书 | 级别 | 备注 |
|:--:|---------|------|------|
| 1 | 建筑工程施工总承包 | 特级（核心） | 最高等级，覆盖多数房建项目资格门槛 |
| 2 | 建筑工程设计 | 甲级（核心） | 设计施工一体化项目可用 |
| 3 | 人防工程设计 | 甲级（核心） | 人防工程专用 |
| 4 | 质量管理体系认证证书 | ISO 9001 | 通用加分项 |
| 5 | 环境管理体系认证证书 | ISO 14001 | 通用加分项 |
| 6 | 职业健康安全管理体系认证证书 | — | 通用加分项 |
| 7 | 信息安全管理体系认证证书 | ISO 27001 | 信息化类项目加分项 |
| 8 | 知识产权管理体系认证证书 | — | 加分项 |
| 9 | 社会责任管理体系认证证书 | — | 有效期 2024-08-29 ~ 2027-08-29，注意边界 |
| 10 | 安全生产许可证 | 有效期内 | 工程类项目硬性门槛 |
| 11 | 其他集团现有资质 | — | 待投标专员补清单 |

> 资质清单为**配置数据**（data_source: 企业资质清单），schema 只定义字段与匹配规则，资质明细变更不改 schema，只改配置。

### 1.4 平台注册表 `[table]`

范围圈定（2026-08-17 依据《京津冀招投标平台对照表》实测筛选冻结，替代初版 4 行占位）：

| 级别 | 平台 | URL | 采集方式 | 备注（实测） |
|:--:|------|-----|---------|------|
| 国家级 L1 | 全国公共资源交易平台 | `ggzy.gov.cn` | L1 官方页面 | 汇聚 3000+ 交易中心；URL 含 YYYYMMDD 日期段，可作官方日期验证 |
| 国家级 L1 | 中国招标投标公共服务平台 | `cebpubservice.com` | L1 官方页面 | 国家电子招投标枢纽，CA 互认 |
| 国家级 L1 | 中国政府采购网 | `ccgp.gov.cn` | L1 官方页面 | 政采工程/服务；京津冀实操走三省市分站（见下） |
| 北京 L1 | 北京公共资源交易服务平台 ★ | `ggzyfw.beijing.gov.cn` | L1 官方页面 | 全国 CA 互认（移动证书） |
| 北京 L1 | 北京市政府采购网 | `ccgp-beijing.gov.cn` | L1 官方页面 | 市本级招标公告入口 |
| 北京 L1 | 北京建设工程交易系统 | `zhjy.bcactc.com` | L1 官方页面 | 房建/市政工程全流程；强制北京 CA（仅下载文件/投标环节） |
| 天津 L1 | 天津市公共资源交易平台 ★ | `ggzy.zwfwb.tj.gov.cn` | L1 官方页面 | 五类角色注册登录 |
| 天津 L1 | 天津市政府采购网 | `ccgp-tianjin.gov.cn` | L1 官方页面 | 投标需供应商注册 |
| 河北 L1 | 河北省公共资源交易服务平台 ★ | `szj.hebei.gov.cn/hbggfwpt/` | L1 官方页面 | ⚠️ 旧域名 `ggzy.hebei.gov.cn` 已注销，禁用 |
| 河北 L1 | 河北省政府采购网 ★ | `ccgp-hebei.gov.cn/province/cggg/zbgg/` | L1 官方页面 | 三省市分站中字段最全（浏览全字段免费） |
| 河北 L1 | 惠招标 | （河北交投 `ebidding.hebtig.com`） | L1 官方页面 | 招标与采购服务平台 |
| 河北 L1 | 雄安新区公共资源交易服务平台 | （雄安新区） | L1/L2 按评估 | 战略覆盖圈 |
| 河北 L2 | 石家庄市公共资源交易中心 | `sjzsggzyjyzx.org.cn` | L2 自建（评估） | 需 CA |
| 河北 L2 | 唐山市公共资源交易中心 | `ggzyjy.xzspj.tangshan.gov.cn` | L2 自建（评估） | 需 CA |
| 河北 L2 | 邯郸市公共资源交易中心 | `ggzy.hd.gov.cn` | L2 自建（评估） | 需 CA |
| 河北 L2 | 秦皇岛市公共资源交易中心 | `qhdggzy.cn` | L2 自建（评估） | 需 CA |
| 河北 L2 | 承德市公共资源交易中心 | `szj.chengde.gov.cn/cdsggzy/` | L2 自建（评估） | 需 CA |
| 河北 L2 | 衡水市公共资源交易中心 | `hsggzy.hengshui.gov.cn` | L2 自建（评估） | 需 CA |
| 河北 L2 | 沧州市公共资源交易中心 | `xzsp.cangzhou.gov.cn` | L2 自建（评估） | 需 CA（代理 Type=2） |
| 河北 L2 | 邢台市公共资源交易中心 | `60.6.198.121:8888`（IP 直连） | L2 自建（评估） | JS 门户，列表异步加载 |
| 河北 L2 | 保定市 | 挂省级平台（inforc=1306） | 走省级 L1 | 无独立域名 |
| 河北 L2 | 廊坊市 | 挂省级平台（inforc=1310） | 走省级 L1 | 无独立域名 |
| 第三方 L0 | 千里马 | `qianlima.com` | 官方 API/授权推送（待合同） | 免注册看标题/编号，电话打码；普通 7999/高级 12999/VIP 21999 元/年 |
| 第三方 L0 | 剑鱼标讯 | `jianyu360.com` | 官方 API/授权推送（待合同） | 免费看全文+联系；报价议价 |
| 第三方 L0 | 采招网 | `bidcenter.com.cn` | 官方 API/授权推送（待合同） | ⚠️ 是**采招网**不是比地；API 0.1 元/条 |
| 第三方 L0 | 比地招标 | `bidizhaobiao.cn` | 官方 API/授权推送（待合同） | 免费看全文（疑似免费站） |

**非公告源（剔除，不接入）**：河北省住建厅（`zfcxjst.hebei.gov.cn`，政策/资质审批，招标信息在交易平台）、住建部四库一平台（`jzsc.mohurd.gov.cn`，查资质业绩）、中国采购与招标网（`chinabidding.com.cn`，正文加密混淆）、张家口电子保函平台（`60.8.117.38:5027/zjkbhpt/`，仅保函服务非公告源）、河北政采网 11 地市分站（`ccgp-hebei.gov.cn` 各地市子站，经省级站覆盖）。

> **关键认知（2026-08-17 实测结论）**：①看招标公告 = 免费免登录（《招标投标法》第 16 条法定公开）；CA 锁只在「下载招标文件 + 投标」环节需要——公告采集无门槛；②工程大项目主战场 = 省市公共资源交易平台（北京 ggzyfw / 天津 ggzy.zwfwb / 河北 szj.hebei.gov.cn + 11 市交易中心）；③政采中小项目（百万级）= ccgp 三省市分站；④商业平台 = 花钱买省时间，不是买信息——只做采集则官方免费源足够，付费 L0 非必需。
>
> **采集策略（2026-08-18 用户决策，覆盖初版"待圈定优先级"）**：
> 1. **L2 各地市不区分优先级，全部纳入采集清单**（石家庄/唐山/邯郸/秦皇岛/承德/衡水/沧州/邢台独立接入；保定/廊坊走省级；逐个过合规评估后启用，宁缺毋滥原则不变）。
> 2. **千里马/剑鱼免费资源充分利用**：免费额度纳入日常巡检（免注册可看标题/编号/摘要，剑鱼可看全文+联系），作为官方源的补充线索源；付费 API/会员（千里马 7999+ / 采招网 API 0.1 元/条）仍待合同确认后接入，**不阻塞采集**。
> 3. 聚合平台（toobiao/dlzb 等）标注日期**不可直接采信**（推送硬准则），须官方平台验证后才计入当日公告。
>
> **圈定标准**：默认 = 属地半径 + 战略导向（京津冀固定圈 + 雄安周边 + 集团重点拓展省份）；获取集团近 3 年投标记录后升级为"业务导向"口径。新增平台必须过法务《数据来源合规清单》后方可启用。

### 1.5 资质匹配规则 `[rule + example]`

匹配维度（复用方案 §四 1.2.3）：资质类别 / 等级 / 专业 / 有效期 / 业绩年限与规模 / 人员注册证书。

判定结果：

| 判定 | 含义 | 处置 |
|------|------|------|
| satisfied 满足 | 公告资格要求全部被集团现有资质覆盖 | 进入情报库，可推送 |
| rejected 不满足 | 关键项不满足（如要求施工总承包特级而集团无此等级） | 剔除或标"不匹配"，不推送 |
| partial 部分满足（人工复核） | 非关键项缺失（如业绩年限差 1 年） | 标记人工复核，推送但显著标注 |

输出：匹配状态 + 缺失项清单（回链 2.3 资格要求子卡）。

> **示例（测试样本）**：公告要求"建筑工程施工总承包三级及以上"，集团持有特级 → satisfied（高等级覆盖低等级）；公告要求"近 3 年具有同类 2 亿以上业绩 1 项"，集团业绩库暂无该规模记录 → partial（人工复核）。**注意：优势匹配为占位能力（见 4.1.9），企业资料导入前一律输出"待企业资料导入"，禁止用公开公告编造匹配结论。**

---

## 二、招标事实维度（6 维，替代智库 7 维分析框架）

> 每字段定义格式：`field_key`（类型，必填性）— 提取来源 — 提取规则。所有字段值必须来自公告原文，逐字段绑定来源链接 + 原文摘录。

### 2.1 项目基本信息 `[schema]`

| field_key | 中文 | 类型 | 必填 | 提取规则 |
|-----------|------|:--:|:--:|------|
| project_name | 项目名称 | string | ✅ | 公告标题/首段，取全称 |
| tender_no | 招标编号 | string | — | 公告"招标编号/项目编号"字段 |
| tenderee | 招标人 | string | ✅ | 公告"招标人"栏；含统一社会信用代码则一并记录 |
| agency | 招标代理机构 | string | — | 公告"代理机构"栏 |
| region | 地区 | string | ✅ | 省/市/区县三级，从公告地址/采购人属地判定 |
| project_type | 项目类型 | enum | ✅ | 工程/货物/服务 |
| industry | 行业专业 | string | — | 房建/市政/公路/水利/港口航道等 |
| procurement_method | 招标方式 | enum | — | 公开招标/邀请招标 |
| announcement_type | 公告类型 | enum | ✅ | 回链 1.1 |
| source_platform | 来源平台 | string | ✅ | 采集源平台名 |
| source_url | 来源链接 | url | ✅ | 公告原文 URL，可访问性由 G4 验证 |
| publish_time | 发布时间 | datetime | — | 公告发布日期 |
| tender_doc_link | 招标文件获取入口 | url+text | — | 「招标文件的获取」段提取的平台入口 URL + 获取时间窗口（§4 通道①）|
| tender_doc_method | 招标文件获取方式 | enum | — | direct（公开直链）/ platform（需登录平台）/ none（无链接）|

### 2.2 时间线 `[schema]`

| field_key | 中文 | 类型 | 必填 | 提取规则 |
|-----------|------|:--:|:--:|------|
| deadline_signup | 报名/文件获取截止时间 | datetime | — | "获取招标文件时间"栏 |
| deadline_clarify | 答疑/澄清截止时间 | datetime | — | "答疑截止"或澄清公告载明 |
| deadline_bid | 投标文件递交截止时间 | datetime | ✅ | 硬字段，错过=废标；**分钟级时效字段** |
| open_date | 开标时间与地点 | datetime | — | "开标时间"栏 + 开标地点 |
| deadline_bond | 投标保证金到账截止时间 | datetime | — | "保证金到账截止"栏 |
| award_time | 中标候选人/结果公示时间 | datetime | — | 后续跟踪补入，不来自首采 |

> 时区/工作日口径：均以公告原文为准；缺失标"待补"，不推断、不换算。

### 2.2.7 时效优先级分级（采集第一判定）`[rule + table]`

> ⚠️ 系统有效性核心规则（2026-08-13 优化）。判定基准 = **报名/文件获取截止时间 `deadline_signup`**（非投标截止）。报名期过后招标文件不可下载，项目即失去参与价值——即使投标截止未到。

| 优先级 | 判定条件（T=当前时间，S=`deadline_signup`） | 处置 |
|:--:|------|------|
| **P0 最高** | S − 3 个工作日内（含当天） | **立即推送**，推送卡片顶部醒目标注「⏰ 报名即将截止」 |
| **P1 次高** | S − 7 天 至 S − 3 个工作日 | 当日推送，标注「📅 报名窗口有限」 |
| **P2 常规** | T < S − 7 天（报名窗口充足） | 入库，随常规批次推送 |
| **✗ 不予采纳** | T > S（报名已截止）或 T > `deadline_bid`（投标已截止） | **不入库、不推送、不记录**，G0 直接丢弃 |

**执行规则**：
1. 判定发生在 **G0 采集门禁**：抓取后立即读取 `deadline_signup`，已过 → 丢弃并记采集日志（`dropped_reason: 报名已截止`），不生成 intake。
2. 优先级随时间**滚动升级**：入库时为 P2/P1 的项目，每次推送任务重算，P2→P1→P0 自动升级。
3. "3 个工作日"按中国法定工作日计（不含周末/节假日）。
4. 公告未载明 `deadline_signup` 时：以 `deadline_bid` 前推 7 天估算，并在字段标注 `deadline_signup: 估算(投标截止前7天)`；两者均缺失 → 按 P2 入库并标记 `时效待核`。
5. 采集建议频次：P0/P1 项目每日重扫相关平台，P2 项目按常规频次。

### 2.3 资格要求（回链 1.5）`[schema]`

| field_key | 中文 | 类型 | 必填 | 提取规则 |
|-----------|------|:--:|:--:|------|
| qualification | 资质要求 | string | — | 类别/等级/专业/有效期，逐条摘录 |
| personnel | 人员要求 | string | — | 注册建造师/职称/数量/社保 |
| performance | 业绩要求 | string | — | 同类业绩/年限/规模/证明材料 |
| finance | 财务要求 | string | — | 审计报告/净资产/银行资信 |
| credit | 信用要求 | string | — | 信用中国/黑名单/行政处罚 |
| safety_license | 安全生产许可证 | string | — | 是否要求+等级 |
| joint_venture | 联合体要求 | string | — | 是否允许/牵头人/分工 |
| other_qualification | 其他资格 | string | — | 特定行业许可/备案/本地化要求 |
| review_method | 审查方式 | enum | — | 资格预审/资格后审 |

### 2.4 评分规则 `[schema]`

| field_key | 中文 | 类型 | 必填 | 提取规则 |
|-----------|------|:--:|:--:|------|
| review_standard | 评审办法 | enum | — | 综合评估法/经评审最低价法/其他 |
| business_scoring | 商务评分 | string | — | 价格权重、报价公式、扣分项 |
| technical_scoring | 技术评分 | string | — | 技术方案/施工组织设计权重 |
| credit_scoring | 资信评分 | string | — | 企业信用/业绩/荣誉加分 |
| disqualification_clauses | 废标条款 | string[] | — | 否决投标条件，**逐条原文摘录** |
| clarification_rules | 澄清/补正规则 | string | — | 澄清时限/补正方式 |

### 2.5 金额与限价 `[schema]`

| field_key | 中文 | 类型 | 必填 | 提取规则 |
|-----------|------|:--:|:--:|------|
| budget_amount | 预算金额 | decimal | — | 公告原文数字，保留单位 |
| ceiling_price | 最高投标限价 | decimal | — | 公告原文数字 |
| bid_bond_amount | 投标保证金 | decimal | — | 金额或比例（注明口径） |
| performance_bond | 履约/质量保证金 | decimal | — | 后续补入 |
| currency | 币种与单位 | string | — | 默认人民币（元/万元），载明则录 |
| pricing_method | 计价方式 | string | — | 清单计价/定额等，公告载明则录 |

### 2.6 合规约束 `[schema]`

| field_key | 中文 | 类型 | 必填 | 提取规则 |
|-----------|------|:--:|:--:|------|
| dark_bid | 暗标要求 | string | — | 技术标明标/暗标、格式要求 |
| ca_requirement | CA/电子签章 | string | — | CA 证书/电子签章要求 |
| submission_method | 递交方式 | string | — | 电子/纸质/双信封 |
| authorization_requirement | 授权委托书 | string | — | 法定代表人/授权代表要求 |
| site_visit | 踏勘/标前会 | string | — | 是否组织、时间地点 |
| other_compliance | 其他合规条款 | string | — | 原件备查/装订/密封等 |

---

## 三、来源可信度与质量闸门

### 3.1 数据源分级 `[table]`

| 级别 | 数据源类型 | 接入方式 | 合规风险 | 策略 |
|:--:|------|----------|:--:|------|
| L0 | 千里马/剑鱼标讯等第三方 | 官方 API/授权推送 | 极低（有合同授权） | **优先采用**，默认主数据源 |
| L1 | 中国招标投标公共服务平台、省级指定平台 | 官方公开接口/页面 | 低（法定公开） | L0 的补充与校验 |
| L2 | 各地市公共资源交易平台 | 自建采集（半自动，频率受限） | 中 | 逐个评估 robots/服务条款后接入，宁缺毋滥 |
| L3 | 付费墙、会员专区、需登录内容 | 禁止 | 高 | 不接入（合规 R2） |

### 3.2 来源评分五维（MECE 适配）`[rule + table]`

对**招标信息来源**评分（复用智库 MECE 五维机制，评分对象从"行业资料"改为"招标公告来源"）：

| 维度 | 满分 | 评分规则 |
|------|:--:|------|
| 官方性 | 25 | 官方发布=25；授权 API 推送=20；转载=10 |
| 时效性 | 25 | 发布时间与公告日期一致=25；滞后 ≤24h=20；滞后 >24h=10 |
| 完整性 | 25 | 编号/时间/金额/联系方式等要件齐备=25；缺 1-2 项=15；缺 ≥3 项=5 |
| 一致性 | 15 | 多源交叉一致=15；单源=10；存在冲突=5（需人工复核） |
| 可追溯性 | 10 | 原文链接可访问=10；链接失效=0（驳回） |

总分 ≥80 直接通过；60-79 人工复核；<60 驳回。低置信度不入情报库（3.4）。

### 3.3 置信度映射 `[rule + table]`

| 置信度 | 来源映射 | 触发条件 |
|--------|---------|---------|
| confirmed | 官方平台（L1 官方发布） | 官方平台原文直采，可溯源 |
| high | 第三方授权 API（L0） | 有合同授权，字段完整 |
| medium | 转载源（L2 或转载） | 非一手发布，需多源核对 |
| low | 无法溯源 | **不采用**，不入库 |

### 3.4 质量闸门 `[gate]`

| 判定 | 条件 | 处置 |
|------|------|------|
| 通过 | 评分 ≥80 且置信度 confirmed/high | 进入事实卡提取 |
| 人工复核 | 评分 60-79 或置信度 medium | 人工确认后放行或驳回 |
| 驳回 | 评分 <60 或置信度 low | 不入情报库，记入采集日志 |

---

## 四、项目事实卡规范

### 4.1 事实类型（9 类）`[table]`

> 6 类复用智库事实类型 + 新增 requirement/checklist + 企业私有 advantage（复用方案 §四 4.1）。

| # | fact_type | 中文 | 内容 | 示例句（测试样本，非真实公告） |
|:--:|-----------|------|------|------|
| 1 | basic | 基本信息 | 项目名称/招标人/代理/编号/地区 | "项目名称：某市综合业务楼工程施工招标公告（测试样本）" |
| 2 | time | 时间线 | 报名/答疑/投标/开标/保证金 | "投标文件递交截止时间：2026-08-25 09:30（测试样本）" |
| 3 | qualification | 资格要求 | 资质/人员/业绩/财务/信用 | "资质要求：建筑工程施工总承包三级及以上（测试样本）" |
| 4 | scoring | 评分规则 | 评审办法/商务/技术/资信/废标 | "评审办法：综合评估法（测试样本）" |
| 5 | amount | 金额限价 | 预算/限价/保证金/履约保证金 | "预算金额：1200 万元；最高投标限价：1150 万元（测试样本）" |
| 6 | compliance | 合规约束 | 暗标/CA/递交方式/原件备查 | "递交方式：电子投标文件线上递交（测试样本）" |
| 7 | requirement | 清单式要求 | 资格/废标逐条清单，引用条款号 | "废标条款：投标报价超过最高投标限价的，否决投标（条款 3.2，测试样本）" |
| 8 | checklist | 投标动作清单 | 报名/CA/保证金/递交/开标 | "动作 1：2026-08-20 前完成报名（测试样本）" |
| 9 | advantage | 公司投标优势 | 企业私有库与招标要求匹配 | **占位：待企业资料导入**（禁止公开公告编造） |

### 4.2 项目主卡字段 `[schema]`

> 主卡 = 一项目一卡，只存结构化摘要；非空字段必须绑定来源链接 + 原文摘录；缺失标"待补"。

```yaml
card_id: string            # 必填，主键，系统生成（如 T-CARD-0001）
project_id: string         # 必填，多公告聚合键（如 T-PROJ-0001）
project_name: string       # 必填，公告原文
tender_no: string          # 选填
tenderee: string           # 必填
agency: string             # 选填
region: string             # 必填，省/市/区县
project_type: enum         # 必填，工程/货物/服务
budget_amount: decimal     # 选填
ceiling_price: decimal     # 选填
bid_bond_amount: decimal   # 选填
deadline_signup: datetime  # 选填
deadline_bid: datetime     # 必填
open_date: datetime        # 选填
priority: enum              # 必填，P0/P1/P2（时效分级，§2.2.7；不予采纳项不入库）
qualification_requirements: ref[]  # 选填，子卡引用
scoring_rules: ref[]       # 选填，子卡引用
company_advantage: ref[]   # 选填，占位=待企业资料导入
source_links: url[]        # 必填，每条可点击
confidence: enum           # 必填，confirmed/high/medium/low
status: enum               # 必填，active/closed/awarded
```

> 编译规则：空字段标"待补"，不硬造（复用智库"空字段『暂无数据』"规则）。`company_advantage` 保持"待企业资料导入"占位。

### 4.3 子卡挂接规则 `[rule]`

1. 一项目一主卡；子卡按事实类型拆分（time/qualification/scoring/requirement/checklist/advantage 等）。
2. 子卡 YAML 头部含 `project_id` + `parent_card_id` 回链主卡；主卡保留子卡引用列表。
3. advantage 子卡**仅引用企业私有库**（资质/人员/业绩/历史标书/成本），不引用公开公告。
4. 同项目不同公告（公告/澄清/中标）按 `project_id` 聚合，不覆盖历史版本（关联 7.4）。

### 4.4 纯事实红线 `[rule + example]`

**信息搜集 Agent 只允许记录"公告原文说了什么"，禁止任何分析性断语。**

禁止示例（红线词检测，命中即 QA 阻断）：
- ❌ "该标值得投" / "不建议投"（决策判断）
- ❌ "甲方对 X 有倾向" / "招标人偏好"（主观推断）
- ❌ "中标率较高" / "竞对机会大"（预测性结论）
- ❌ "价格过低可能废标"（风险评估）

允许示例：
- ✅ "公告要求投标保证金 20 万元，投标截止 2026-08-25 09:30（原文摘录）"
- ✅ "资质要求：建筑工程施工总承包三级及以上（原文摘录）"

> 分析能力属于独立新建的核查引擎（主方案③），不在信息搜集层出现。此红线由 G2 门禁 + QA §8.3 双重检查。

---

## 五、采集与合规

### 5.1 采集模式 `[rule + workflow]`

| 模式 | 配置值 | 适用期 | 行为 |
|------|--------|--------|------|
| 触发式 | `manual_trigger` | 测试期（默认） | 手动全量/指定源采集、单条公告 URL 触发；不启用定时任务；限频与熔断仍生效 |
| 定时式 | `scheduled` | 正式期 | 单源 ≤1 次/5 分钟；全平台 ≤200 次/小时；失败熔断 |

**切换门槛**：P0 验收 10 条真实公告通过 + 法务《数据来源合规清单》签字 → 切 `scheduled`；平台改版/异常/合规复核时可随时切回 `manual_trigger`。

```text
触发器（何时采）：手动 | 单条URL | 定时      → 调度层
执行器（怎么采）：合规限频 → 抓取 → 清洗 → 入库   → 执行层
测试期只装手动/URL触发器，正式期加定时触发器；共用同一执行管道。
```

### 5.2 robots 协议 `[rule]`

遵守目标站点 robots.txt；禁止路径不采集；自动化检查并记录 `robots_status`。

### 5.3 UA 透明 `[rule]`

使用标识性 UA（含公司信息），不做伪装；透明采集，可被联系。

### 5.4 个人信息最小化 `[rule]`

公告内联系人姓名/电话/邮箱：最小化采集、仅内部使用、展示脱敏、**30 天后脱敏**。

### 5.5 数据源分级 `[table]`

回链 3.1：L0 第三方授权 API / L1 官方平台 / L2 各地市自建 / L3 禁止。每条数据记录 `source_level`。

### 5.6 合规红线（R1-R6）`[table]`

| # | 红线 | 检查点 |
|:--:|------|--------|
| R1 | 不绕过平台技术保护措施 | 无验证码破解/登录态模拟/反爬突破代码路径 |
| R2 | 不采集非公开信息 | 付费墙/会员专区/需登录一律不采 |
| R3 | 不干扰平台正常运行 | 限频硬约束，低于常规反爬阈值 |
| R4 | 个人信息最小化 | 30 天脱敏策略生效 |
| R5 | 数据仅限内部投标用途 | 不转售/不外供/不用于围标串标 |
| R6 | AI 输出可溯源 | 每条推送绑定原始公告链接 |

---

## 六、Pipeline 状态机 + 门禁（G0-G6′）

### 6.1 状态机总览 `[diagram]`

```text
公告 URL / 平台源
   │
   ▼
G0 采集门禁 ──失败──▶ 熔断/告警/人工
   │
   ▼
G1 清洗门禁（字段标准化 + L1/L2 去重）
   │
   ▼
G2 事实卡门禁（必填齐全 + 溯源 + 纯事实红线）
   │
   ▼
G3 情报库门禁（主卡+子卡 + 多源合并）
   │
   ▼
G3.5 匹配门禁（资格/资源逐项匹配 + 满分判定，v0.4.0 新增；准入链路，G4 独立）
   │
   ▼
G4 推送门禁（匹配命中 + 脱敏 + 链接可点）
   │
   ▼
G5 归档门禁（流标/中标/过期 → 归档 + 审计）
   │
   ▼
G6′ 校准门禁（月度 ≥100 条人工抽查 → 阈值校准）
```

### 6.2 状态文件定位 `[table]`

| 项 | 值 |
|----|-----|
| 状态文件路径 | `schema/.pipeline_state.yml`（招投标域独立，不读写智库状态） |
| 格式 | YAML |
| 读取时机 | 每次采集/推送任务开始时 |
| 写入时机 | 每道门禁通过后 |
| 读取方式 | 读 status 字段，MISSING→执行该门禁；PASSED→跳过 |

### 6.3 状态文件 Schema `[schema]`

```yaml
domain: zhaotoubiao
schema_version: 1
phase_a:
  raw_intake:
    count: 0
    status: MISSING          # raw → carded
  evidence_card:
    count: 0
    status: MISSING
phase_b:                     # 采集-情报流水线
  step_01_collect:    { status: MISSING, description: "触发式/定时式采集公告", output_artifacts: ["raw/"] }
  step_02_clean:      { status: MISSING, description: "字段标准化+去重", output_artifacts: ["raw/_清洗报告.md"] }
  step_03_factcard:   { status: MISSING, description: "事实卡提取", output_artifacts: ["raw/卡/*.md"] }
  step_04_intel_lib:  { status: MISSING, description: "项目情报库主卡+子卡", output_artifacts: ["情报库/"] }
  step_05_push:       { status: MISSING, description: "推送飞书/邮件", output_artifacts: ["推送日志"] }
  step_06_archive:    { status: MISSING, description: "归档+审计", output_artifacts: ["情报库/归档/"] }
phase_c:
  step_01_calibrate:  { status: MISSING, description: "月度人工抽查≥100条+阈值校准", output_artifacts: ["质量报告"] }
```

### 6.4 门禁 G0-G6′ `[rule + gate + auto_action]`

> 每道门禁内嵌 `auto_action`，Agent 自动执行修复，**汇报但不询问是否继续**。

**G0 采集门禁**
- rule: 来源级别 ∈ {L0,L1,L2}；robots/频率合规；失败熔断（单源连续 3 次失败 → 暂停）；**时效判定（§2.2.7）：`deadline_signup` 已过 → 不入库直接丢弃**
- auto_action: 跳过 L3 源 → 校验 robots 与频率 → **计算时效优先级 P0/P1/P2/不予采纳（滚动重算）** → 对失败源暂停并告警 → 重试 ≤2 次 → 验证通过后更新状态文件
- fallback: 持续失败 → 标记 `source_suspended`，等人工介入
- check: raw/intake/ 下 intake 的 `deadline_signup` 均未过；已丢弃项记 `dropped_reason`

**G1 清洗门禁**
- rule: 字段标准化完成；L1 精确去重（URL/公告ID/招标编号一致）；L2 近似去重（项目名+招标人+时间+地点相似度）；缺字段标"待补"
- auto_action: 执行字段映射修复 → 执行 L1/L2 去重 → 补"待补"标记 → 验证 → 更新状态

**G2 事实卡门禁**
- rule: 必填字段齐全（card_id/project_id/project_name/tenderee/region/project_type/deadline_bid/source_links/confidence/status）；每条可溯源；纯事实红线零命中
- auto_action: 缺字段 → 回源补采 → 重新提取 → 验证 → 更新状态（复用方案 #10：缺事实卡→自动回源补采→验证→继续）
- fallback: 原文已失效（链接 404）→ 标记 `source_dead`，人工确认

**G3 情报库门禁**
- rule: 主卡+子卡齐备（4.3）；状态流转正确（6.4 项目状态机）；多源合并完成（7.3）；**intake.pipeline_status ∈ {carded, intel} 且主卡存在（C 方案硬校验前置）**
- auto_action: 生成主卡 → 挂接子卡 → 多源引用合并 → **更新 intake pipeline_status=intel** → **执行事件同步 `python3 scripts/publish/sync_events.py`（为每个已入库 intake 创建/更新 AnnouncementEvent，幂等；计算 content_hash/dedup_key；同 dedup_key 跨平台归并为一条事件 + source_links 追加）** → 验证 → 更新状态
- check: `python3 scripts/pipeline_gates.py --intake <id>` 输出 G0/G2/G3/事件同步 全 ✅

**G3.5 匹配门禁（v0.5.1 修订，准入链路）**
- rule: 资格/资源逐项匹配完成（§11.1）；三类要求、标段/联合体、条款时点和澄清版本已锁定（§11.2）；项目经理硬条件验证完成（§11.3）；资格/评分/准备度/内部准入四类结论均可解释（§11.5）；匹配结论全部回链招标条款与企业证据。
- auto_action: 缺失/无法核验 → `blocked_missing_data` 待补队列；资格性要求证据充分但不满足 → `not_qualified`；响应性硬要求证据充分但不满足 → `blocked_hard_requirement`；解析不完整或规则无法执行 → `manual_review`；仅内部准入政策满足且结果未失效 → `qualified_full_score` → `pending_bid_approval`。
- fallback: `manual_review`、`not_calculable` 或 `stale` 不参与自动准入；门禁模式可停止状态迁移，诊断模式仍输出可独立判断的全部缺口。
- 边界: 本门禁只判定“能否进入人工审批”，**不阻断公开情报推送（G4 独立运行）**；内部“满分”不等于评标委员会得分，更不等于自动投标。

### 6.4.0 门禁链硬校验（C 方案，2026-08-17 防 Agent 跳步）`[rule]`

> **背景**：Agent 可绕过 G1/G2/G3 直接跑 sync_events → 日报 → 推送（曾发生：8/17 广宗项目只有 intake 就同步，事件卡 detail_fields 只剩 4 字段、无事实卡/主卡）。`pipeline_gates.py` 原为"报告器"（只输出该执行哪个门禁，不强制）。

**硬规则**：
- **intake.pipeline_status 状态机**：`raw`（G0 完成）→ `carded`（G2 事实卡完成）→ `intel`（G3 主卡完成）→ `synced`（事件已同步）。新采集 intake 必须写 `pipeline_status: raw`；G2 后更新 `carded`；G3 后更新 `intel`。
- **sync_events.py 硬校验**：对每个 intake，`pipeline_status ∈ {carded, intel}` **且** 主卡存在才允许同步；否则 `gate_blocked` 拒绝（不产生事件卡）。`--force` 可绕过但记录审计，**仅限人工确认场景，禁止 cron 使用**。
- **兼容历史数据**：无 `pipeline_status` 字段但主卡存在 → 视为已过 G2/G3（intel），放行。
- **检查命令**：`python3 scripts/pipeline_gates.py --intake <intake_id>` 输出 G0/G2/G3/事件同步 门禁链状态。
- **cron 巡检流程（C）**：pipeline_gates.py 检查 → 检索 → 时效判定 → G1 入库（pipeline_status=raw）→ G2 事实卡（carded）→ G3 主卡（intel）→ sync_events.py → generate_daily_issue.py → generate_push_cards.py 原样输出。**禁止 --force、禁止跳步**。

**G4 推送门禁**
- rule: 匹配规则命中；个人信息已脱敏；**链接浏览器级可达性复检通过（§6.4.2，check_links_browser.py Chrome headless 实测）**；**招标文件获取入口已提取并标注（§4 通道①）**；卡片格式合规（templates/推送卡片模板.md，无模板外段落、字段用枚举）
- auto_action: 重新校验链接（浏览器级）→ 附备选入口或标注"链接待人工验证" → 补脱敏 → 重发推送 → 验证 → 更新状态

### 6.4.1 招标文件获取入口（G4 前置，2026-08-14 通道①）

推送卡片必须包含「招标文件获取」段（链接发现 + 标注，**不做自动下载**）：

| 检查项 | 规则 |
|--------|------|
| 链接提取 | 从公告「招标文件的获取」段提取平台入口 URL（原文有则抄原文 URL；无 URL 仅有平台名 → 标"见公告指引"） |
| 获取方式分类 | `direct` 公开直链（.pdf/.docx 可直接下载）→ 标注"可在线查看"；`platform` 需登录平台 → 标注"需登录下载（人工）"；`none` 无链接 → 标注"见公告指引" |
| 获取时间窗口 | 抄录原文「请于 X 至 Y 下载」的起止时间；缺失标"待补" |
| 链接复检 | G4 推送前对入口 URL 做 **浏览器级** 可达性复检（§6.4.2）；不可达 → 标注"链接待人工验证" |
| 合规红线 | **绝不过验证码、不模拟登录（R1）；登录墙后文件不自动抓取（R2）**；需登录 → 明确标注由投标专员人工下载 |
| 溯源 | 入口 URL + 获取时间窗口均抄录原文，可回链公告 |

### 6.4.2 链接浏览器级可达性复检（G4 前置，2026-08-17 修复）

> **背景**：ggzy.gov.cn / ebidding.hebtig.com / eps.ctg.com.cn / chnenergybidding.com.cn 等平台
> 服务器 TLS 配置老旧，拒绝现代浏览器（Chrome/Safari/飞书内置）默认的 TLS 1.3 握手
> （`ERR_CONNECTION_CLOSED` → 用户看到"无法访问此网站"），但 curl/urllib（协商 TLS 1.2）返回 200。
> **curl HTTP 200 ≠ 浏览器可打开**——旧复检（urllib HEAD）全部误判"可点击"。

**硬规则**：
- 推送前对每条公告的**原文链接 + 招标文件入口链接**运行 `python3 scripts/check_links_browser.py "<url>"`（Chrome headless 实测握手，输出三态 + JSON，含备选入口映射）。
- 判定与卡片标注：

| 复检结果 | 含义 | 卡片标注 |
|---------|------|---------|
| ✅ browser_ok | 浏览器可访问 | 正常「原文链接（可点击）」 |
| ⚠️ browser_blocked / browser_fail | curl 200 但浏览器握手被拒 | 「⚠️ 链接在当前浏览器可能打不开（平台 TLS 兼容问题，内容存在）」+ **必须附备选入口**（平台首页 + 公告标题关键词，脚本 JSON `fallback_entry`） |
| ❌ unreachable | 内容层面不可达 | 「❌ 链接不可访问，待人工验证」+ 附平台首页 |

- **已知 TLS 不稳定平台名单**（维护在 `check_links_browser.py` `FALLBACK_ENTRIES`）：ggzy.gov.cn / ebidding.hebtig.com / eps.ctg.com.cn / chnenergybidding.com.cn。这些平台 TLS 行为**时好时坏**（实测几分钟内从握手超时变可访问），**即使单次复检 ✅ 也建议附备选入口**。
- 旧脚本 `scripts/check_tender_doc_links.py`（urllib 同栈）仅作内容存在性参考，**不得作为浏览器可达性判定依据**。
- 卡片格式合规并入 G4：无模板外段落（如【匹配初判】）、`project_type` 用枚举（工程/货物/服务）、描述放 `industry` 字段（2026-08-17 B 前置）。

**G5 归档门禁**
- rule: 流标/中标/过期项目 → 状态 closed/awarded → 归档，保留审计
- auto_action: 状态流转 → 移入归档目录 → 写审计日志 → 验证 → 更新状态

**G6′ 校准门禁（反馈闭环）**
- rule: 月度人工抽查 ≥100 条"有用/无用"回标；阈值校准
- auto_action: 生成抽查清单 → 等待人工回标 → 计算召回率/误报率 → 调阈值 → 写月度质量报告

### 6.5 自动执行算法 `[workflow]`

```text
1. 读取 schema/.pipeline_state.yml
2. for G0 → G6′:
   a. 若 rule 不满足 且 有 auto_action → 执行 auto_action → 验证 → 更新状态文件
   b. 若 rule 不满足 且 无 auto_action → 阻断，等用户指令
   c. 若 rule 满足 → 标记 PASSED → 继续下一门禁
3. 全部 PASSED → 任务完成报告
```

汇报格式：`🔧 门禁 G{N} 未通过 → 自动执行 → [进度] → ✅ 通过 → 继续`。

---

## 七、去重规则

### 7.1 L1 精确去重 `[decision-tree + 伪代码]`

```python
def dedupe_l1(candidate, existing):
    keys = [candidate.source_url, candidate.tender_no, candidate.announcement_id]
    for k in keys:
        if k and k in {e[k] for e in existing}:
            return "L1_DUPLICATE"   # 完全重复 → 丢弃，不新建卡
    return "NEW"
```

### 7.2 L2 近似去重 `[rule]`

- 判定特征：项目名 + 招标人 + 时间 + 地点相似（编辑距离/向量相似度，阈值 ≥0.9）
- 处置：同一项目多平台发布 → 合并为主卡 + 多源引用（关联 7.3）

### 7.3 L3 合并去重 `[rule + table]`

| 项 | 规则 |
|----|------|
| 合并目标 | 多平台同一项目 → 主卡 + 多源引用 |
| 冲突字段 | 保留多源记录（`source_links[]` 多条 + 各自原文摘录） |
| 置信度 | 多源一致 → 取最高；冲突 → 标记人工复核 |
| 保留策略 | 先入库版本为主，新源作为补充引用追加 |

### 7.4 变更/澄清关联 `[rule]`

同一 `project_id` 下的变更/澄清/中标公告按公告类型挂接，**不覆盖历史版本**；每次挂接记录时间戳与来源链接。

### 7.5 去重动作与数据结构 `[table]`

| 场景 | 保留源状态 | 被合并源状态 | 证据卡影响 |
|------|-----------|-------------|-----------|
| L1 重复 | 原卡 | 标记 `dup_of: <card_id>`，不删除 | 无新增 |
| L2 近似 | 原卡 + 追加 source_link | 标记 `merged_into` | 合并来源摘录 |
| L3 多源合并 | 主卡 + 多源引用 | 各源保留原文快照 | 逐源绑定摘录 |

### 7.6 置信度影响 + 域内特殊规则 `[rule]`

去重后置信度：多源一致取最高；冲突取最高但标记 `manual_review`；单源保持原值。

**域内特殊规则（招投标定制，≥3 条）**：
1. **多平台同项目高频重复**：同一招标公告常在"中国招标投标公共服务平台 + 省级平台 + 惠招标"3 处发布，属 L2 近似重复，必须合并，禁止生成 3 张主卡。
2. **招标编号是强去重键**：同一项目变更/澄清公告沿用同一招标编号（tender_no），L1 去重时**必须区分公告类型**——编号相同但公告类型不同（招标 vs 澄清）不判重，按 7.4 挂接。
3. **项目名称近似但编号不同**：招标人相似、项目名微差（如"一期"vs"二期"）且编号不同 → 判为不同项目，不合并；需人工复核时标记 `manual_review`。
4. **资格预审与正式招标**：资格预审公告与后续正式招标公告同项目但**不同编号** → 通过 project_id 语义聚合（招标人+项目名+预算），不靠编号去重。

---

## 八、QA 标准

### 8.1 逐字段核对 `[gate]`

事实卡每个字段 vs 原文段落逐条核对，字段级溯源；核对人（或核对 Agent）与提取人分离或复核。

### 8.2 抽样规则 `[rule]`

- P0 上线：10 条真实公告**全检**。
- 常态化：每日抽检 + 月度 ≥100 条（人工回标）。

### 8.3 红线检查 `[rule]`

扫描全库：禁止"值得投/有倾向/中标率/建议投"等分析断语；命中即阻断，回炉重提。

### 8.4 溯源检查 `[rule]`

每条事实卡含 `source_link` 且可访问；链接失效标记 `source_dead` 并人工确认。

### 8.5 缺失检查 `[rule]`

缺失字段必须标"待补"，不硬造；无"空字段 = 无此项"的默认推断。

### 8.6 QA 输出 `[template]`

```yaml
qa_report:
  period: "2026-08"
  checked_cards: N
  pass_rate: N%
  red_line_hits: []        # 应为空
  missing_links: []        # 应为空
  calibration:             # G6′ 输出
    recall: N%
    false_positive: N%
    threshold_adjustments: []
```

---

## 九、术语定义 `[table]`

| 术语 | 英文/缩写 | 定义 |
|------|----------|------|
| 招标公告 | Tender Notice | 招标人公开发布的项目招标信息 |
| 资格预审 | Prequalification | 开标前对投标人资格进行的审查 |
| 资格后审 | Post-qualification | 开标后评标前对资格进行审查 |
| 最高投标限价 | Ceiling Price | 投标报价不得超过的最高价格 |
| 投标保证金 | Bid Bond | 投标人按约定缴纳的担保金 |
| 废标条款 | Disqualification Clause | 导致投标被否决的条款 |
| 中标候选人公示 | Award Candidate Notice | 评标后公示的前 N 名候选人 |
| 澄清公告 | Clarification | 对原公告的疑问解答与说明 |
| 招标编号 | Tender No. | 招标项目唯一编号，去重强键 |
| 暗标 | Dark Bid | 技术标匿名评审方式 |
| 综合评估法 | Comprehensive Evaluation | 商务+技术+资信综合打分评审 |
| 双信封 | Two-Envelope | 商务标与技术标分装递交 |
| Material | Material | 最小数据治理单元：公告、招标文件、企业资质、业绩、人员证照等（F003 §4.1，契约冻结于 §13.1） |
| 权限矩阵 | Permission Matrix | 公开读取/企业内读取/受限（匹配层）/仅审批人 四档权限（F003 §6.2，契约冻结于 §13.2） |
| 匹配矩阵 | Match Matrix | 招标要求与企业私有资料逐项匹配的字段级结果（§11.1） |
| 满分准入 | Full-Score Admission | 全部硬条件满足+计分项满分+证据有效+合格可用项目经理+动作就绪（§11.5） |
| 待投标审批 | Pending Bid Approval | 满分后进入人工审批队列的状态，不等于自动投标（§12.1） |
| 人工豁免 | Waiver | 审批人对个别阻断项的授权放行，须记录授权人/原因/证据/有效期/审批时间（§12.3） |

---

## 十、发布与归档层（v0.2.0 新增，任务书：推送/编排/展示/归档/来源审计改造）

> **本章是"发布层与审计层分离"的规范**：事实数据（§二~§七 不变）与展示数据分层；客户展示内容与后台审计内容分离；历史数据保留但不重复推送。
> 新增能力**全部位于下游发布层**，不改变 G0-G6′ 门禁、事实卡、去重、溯源等既有规则。现有字段含义不静默改变（见 10.10 兼容策略）。

### 10.1 四类数据对象 `[table]`

| 数据对象 | 定义 | 存储 | 说明 |
|---------|------|------|------|
| Tender/Project 项目主体 | 一个招标项目 | 情报库主卡（不变） | 项目主体与事件分离 |
| **AnnouncementEvent** 公告事件 | 项目在某个时间发生的一次公告事件（招标/澄清/变更/中标…） | data/announcement_events/ | 日报的原料单位 |
| **DailyIssue** 日报 | 某个自然日发布给客户的日报 | data/daily_issues/ | 一自然日一份，幂等生成 |
| **SourceRun** 巡检运行 | 某个平台一次巡检/采集运行记录 | data/source_runs/ | 只进后台审计与周报 |

> 客户日报基于 AnnouncementEvent 生成，**不得**直接基于项目最后更新时间或系统首次发现时间生成。

### 10.2 日期口径与准入（v0.3.0 可参与性准入）`[rule]`

统一时区：Asia/Shanghai（北京时间）。**准入 = 当天可参与（2026-08-18 用户决策，替代"当天发布"口径）**：

- **可参与判定**：`deadline_signup`（报名/文件获取截止）或 `deadline_bid`（投标截止）**任一 ≥ now** → 当天仍可报名/下载/投递 → 收录进日报。
- **不再限定"当天发布"**：publish_time 仅作展示与溯源字段；历史发布（publish_time < D）但截止未过的公告**同样收录**（覆盖 v0.2.x 的 `publish_time ∈ [D 00:00, D+1 00:00)` 口径）。
- **截止已过**（两个 deadline 均 < now）→ `delivery_status = excluded`、`exclusion_reason = deadline_expired`，后台保留不进日报。
- **截止时间缺失**（两个 deadline 均无）→ 复核队列（`pending_review` / `missing_deadline`），无法判定可参与性，不硬造、不推测。
- **归属日期展示**：日报条目标注公告原文发布时间 publish_time；判定**禁止**用 collected_at/created_at/updated_at/首次发现时间/入库时间替代。

### 10.3 已推送去重（日报准入硬规则，v0.3.0）`[rule]`

- 日报生成时扫描历史日报（data/daily_issues/*.md）items，**已推送过的事件 ID 一律排除**，同一公告绝不重复推送（`daily_issue.exclude_pushed`，默认 true）。
- 已推送去重独立于跨平台去重：跨平台去重（§10.5）合并同公告多来源为一条；已推送去重保证同一条不被再次推给客户。
- 关闭开关（`exclude_pushed: false`）仅用于内部审计/重放场景，客户渠道不得关闭。

### 10.4 历史公告与截止公告 `[rule]`

- 历史公告继续保存在事实库（去重/变更关联/全生命周期查询/审计/补录/来源追溯），**不得删除**。
- **截止已过**（deadline 均 < now）的公告不进日报、不推送；即使当天被重新抓取/重新解析/字段补全/人工修正/重新过门禁/当天首次进入系统，只要无法参与就不推。
- 同一项目当天发布的新事件（变更/澄清/更正/终止/中标候选人/中标结果等）按**新 AnnouncementEvent** 处理，截止未过则进入当天日报；展示标注「公告类型」+「关联项目」。
- 兼容旧口径：`late_discovered` / `missing_publish_time` 语义保留于代码兼容分支（mode=day），正式配置一律 `participable`。

### 10.5 跨平台去重（日报维度）`[rule]`

- 指纹组合：`normalized_project_name + announcement_type + publisher_or_tenderer + publish_date + normalized_content_hash`（实现：scripts/publish/dedup.py）。
- 同指纹组保留一个主来源 `primary_source` + 多个备用来源 `source_links[]`（每源记录平台名/原文地址/发布时间，差异保留）。
- **不得通过删除来源记录实现去重**；原始来源只增不删。
- 主来源优先级（配置化）：法定官方平台 > 招标人或招标代理官方平台 > 政府公共资源平台 > 权威聚合平台 > 其他转载平台。
- 与 §7.6 域内规则兼容：指纹含 announcement_type，同项目"招标公告 vs 澄清公告"指纹不同，不误合并；项目名近似但编号不同的不合并（§7.6 #3 不变）。
- **content_hash 兜底**：事件创建时由 sync_events 计算（输入优先 intake.content_snapshot 正文快照，否则 intake 正文摘录；事件记录 `content_hash_source` 说明输入来源）。哈希为空的事件在日报去重时**保守不合并**（宁可多显示，不误合并内容无法确认相同的公告）。

### 10.6 紧迫程度（展示分组）`[rule]`

- 明确规则计算，禁止模型自由判断：`deadline <= now + 24h → urgent`；`<= now + 72h → high`；其他 `normal`；字段缺失 `unknown`（不虚构）。
- 判定基准：deadline_signup 优先，缺失用 deadline_bid。
- 展示分组（daily_issue_item.section）：urgent（报名即将截止）/ deadline（投标即将截止）/ new（今日新增）/ change（变更与澄清）/ result（中标及结果）。

### 10.7 展示白名单与内部字段隔离 `[rule]`

- 客户可见字段以 config/publish_config.yml `render.visible_fields` 为准；模板层只做选择/格式化/排序/隐藏/链接/枚举映射，**不得修改事实字段**。
- 内部字段（G0-G4、L0-L3、confidence_score、parser_version、crawler_task_id、raw_record_id、fact_gate_status、intake_id 等）**不得渲染到客户页面**；渲染器输出后执行完整性检查（防泄漏防呆）。
- 来源说明使用业务语言，例如「信息来源：河北省公共资源交易服务平台」「原文发布时间：2026-08-17 09:30」。
- **产物形式（v0.2.1，2026-08-17）**：日报与周报以 **Markdown 报告**形式存储（`报告/日报/`、`报告/来源与覆盖/`），不再生成 HTML；每条公告按骨架树维度（§二 六维 + 招标文件获取 + 溯源）分组直观展示全部可找到字段；缺失字段不堆在条目正文，统一汇总到报告文末「待补充与人工审核提醒」（待补充字段清单 / 人工审核队列 / 迟到公告）。推送简报（飞书/微信）只含客户可见结果，不含后台与复核信息。
- **推送卡片由渲染管线生成（v0.2.3，2026-08-17 B 方案）**：
  - 飞书/微信推送卡片**必须**由 `renderer.render_push_cards_md()`（`scripts/generate_push_cards.py` CLI）生成，**禁止** Agent/cron 手工编写卡片文本（曾发生格式漂移：出现模板外【匹配初判】段、漏必含段、project_type 填长描述）。
  - 卡片内容 = 日报头部 + 按 `templates/推送卡片模板.md` 分组逐条渲染；**有值字段才展示**，缺失字段不堆正文、统一汇总到文末「【待补充字段】（公告原文未载明，不编造）」清单（整段无值 → 该段不显示）；优先级标注 P0/P1/P2（unknown 按 P2 常规）；链接标注走 §6.4.2 复检结果（⚠️ 附备选入口）；置信度必填（骨架树 §3.3，`AnnouncementEvent.confidence`）。
  - 数据边界与日报一致：唯一输入 `build_customer_context(issue, events)`，只允许 `issue.items` 当天事件；渲染前后同款硬校验（条目数一致、无后台状态泄漏、内部字段零泄漏）。
  - cron 巡检流程（B）：检索 → 时效判定 → 入库 raw/intake → `sync_events.py` → `generate_daily_issue.py` → `generate_push_cards.py` 取卡片文本**原样输出**。
- **客户渲染数据边界（v0.2.2，2026-08-17 修复"渲染层数据越权"）**：
  - 所有客户渠道（微信/网页/邮件/Markdown/HTML）的公告输入**唯一来源是 DailyIssue.items**，统一经 `build_customer_context(issue, events)` 取事件；**禁止**任何客户渲染逻辑直接扫描全量事件库（`data/announcement_events/*`、`raw/*`、`情报库/*`）并追加公告。
  - `backend_only` 下客户日报**不得生成"迟到公告"章节**，历史公告标题/发布时间/链接一律不出现；文末后台提醒仅保留统计数字（待复核 N 条 / 迟到 N 条），不含事件明细。
  - 渲染前后硬性校验：输入事件必须 ⊆ items；输出文本不得含非当天事件标题、不得含 `late_discovered/pending_review/迟到公告` 等后台状态；渲染条目数必须等于 item_count。
  - 客户发布策略收敛：`late_announcement_policy` 正式配置仅允许 `backend_only`；`supplement_section/urgent_only` 仅限内部审计预览（如 make_preview 审计样例），禁止用于客户渠道（config 校验强制）。

### 10.8 新增数据结构 `[schema]`

字段语义与任务书 §九 一致，落地为 YAML frontmatter + Markdown body（data/ 目录，不引入数据库）：

- **announcement_event**：id / project_id / announcement_type / title / publish_time（ISO8601 带时区）/ timezone / primary_source_id / primary_source_url / source_links[] / content_hash / dedup_key / discovered_at / fact_gate_status / delivery_status / exclusion_reason / related_event_ids / created_at / updated_at；迟到附加：late_discovered_at / original_publish_time。
- **daily_issue**：id / issue_date / timezone / title / status（draft|published|published_empty|revised|failed）/ item_count / urgent_count / version / generated_at / published_at / last_updated_at / stable_url / archive_path；items 内嵌（见 10.9）。
- **daily_issue_item**：id / daily_issue_id / announcement_event_id / section / urgency / display_order / included_at / inclusion_reason。
- **delivery_record**：id / daily_issue_id / channel（wechat|feishu|email|web）/ target / status（pending|sent|failed|retrying）/ sent_at / retry_count / rendered_url / error_summary / created_at。
- **source_run**：id / source_id / source_name / started_at / finished_at / status（success|partial|failed）/ pages_scanned / raw_items_found / valid_events_found / duplicate_items / rejected_items / late_items / error_category / error_summary / retry_count / last_success_at / task_version / created_at。
- **weekly_source_report**：id / period_start / period_end / timezone / status / version / source_count / successful_source_count / partial_source_count / failed_source_count / raw_item_count / deduplicated_event_count / delivered_event_count / late_event_count / pending_review_count / report_url / export_path / generated_at / published_at。

### 10.9 唯一约束 `[constraint]`

- `UNIQUE(daily_issue_id, announcement_event_id)`：同一公告事件不得在同一日报重复出现（生成器强制，models.unique() 防呆）。
- 同一事件可出现在不同日报版本（force 重生成覆盖同版本内容，不跨日期）。

### 10.10 幂等、版本与兼容策略 `[rule]`

- `generate_daily_issue(date, force=false)`：force=false 且已发布 → 仅一致性检查；force=true → 重新生成，version +1，记录修订原因/时间/执行主体/差异条目。
- 日报修订稳定 URL 不变（/daily/2026-08-17），版本号记录修订。
- 空日报照常生成：status=published_empty、item_count=0，页面显示「当日无新增公告」，证明系统当日正常运行。
- 所有生成任务幂等：重复执行不重复创建日报/条目/投递记录。
- **兼容策略**：本章为纯新增章节，不改动 §一~§九 既有规则；publish_time 在 raw/事实卡层保持选填（§2.1 不变），"日报准入必填"为发布层新增语义；历史数据缺 publish_time 不补齐、不重推；现有推送日志/事实卡/raw 只读不批量修改。

---

## 十一、资格与资源核查域（v0.4.0 新增，v0.5.1 行业规则补强）

> **边界声明**：本章为 ADR-001 显式范围变更的落地层——公开情报层红线（§四 4.4 / §八 8.3）不变，公开层仍然零分析断语；匹配/计分只允许在本章出现，且**必须**是可配置规则 + 证据化结果（每条结论回链招标条款 + 企业证据）。企业资料不足时输出"不可判定/待补材料"，**不得默认满分**（ADR-001 §2.4）。

### 11.1 企业资料匹配（MatchMatrix）`[schema]`

匹配矩阵 = 招标要求（F005 子卡）逐项 vs 企业私有资料（F006/F007）的字段级匹配结果：

| field_key | 中文 | 类型 | 必填 | 说明 |
|-----------|------|:--:|:--:|------|
| matrix_id | 矩阵ID | string | ✅ | 主键，关联 project_id + 规则版本 |
| tender_clause_ref | 要求引用 | ref[] | ✅ | 回链招标条款（F005 子卡），每条结论必回链 |
| evidence_refs | 企业证据引用 | ref[] | ✅ | 回链 F006/F007 证据，无证据不判"满足" |
| match_result | 匹配结果 | enum | ✅ | satisfied / partial / not_satisfied / unverifiable / manual_review |
| score | 计分 | decimal | — | 计分项得分 |
| max_score | 满分值 | decimal | ✅ | 计分项满分值（规则配置） |
| missing_items | 缺失项 | ref[] | — | 待补/待核实清单 |

> **差异登记**：旧 §三 advantage 占位字段 `match_result`（满足/部分满足/待核实/不满足）为占位枚举（§四 4.1.9，保持不动）；本章为正式匹配矩阵枚举。`manual_review` 仅表示解析不完整、规则无法执行或须人工判断，不能视为满足。

### 11.2 三类要求 `[rule]`

| 类型 | 内容 | 判定 |
|------|------|------|
| `hard_requirement` 硬性要求 | 资格、资质、人员、业绩、信用、联合体、否决条款等 | 必须配置失败后果；无法核验为待补，不得误作不满足 |
| `scored_requirement` 计分要求 | 商务、技术、资信、报价等评分项 | 每项定义公式、满分、输入、证据、去重和主观内部评审边界 |
| `action_requirement` 动作要求 | 报名、CA、保证金、递交、开标 | 按 `approval_ready/submission_ready/submitted/opened` 阶段核查 |

Requirement 必须带 `requirement_id`、`lot_id`、`clause_ref`、`assertion`、`rule`、`evidence_required[]`、`as_of`、`missing_action`、`logic_group/operator`、`consortium_role`、`priority`；硬性项另带 `failure_effect`。RuleSet 必须固化这些字段、评分公式/取整和招标文件或澄清版本。详情见 F008 §4。

### 11.3 项目经理匹配（ProjectManagerProfile）`[schema]`

> 字段定义与 F007 §4 逐项对齐；硬条件验证（F007 §6.1）在匹配时先于计分执行，任一不满足或**无法核验** → 该经理不计入"满分可投标"。

| field_key | 中文 | 必填 | 说明 |
|-----------|------|:--:|------|
| manager_id | 经理ID | ✅ | 主键，如 PM-0001 |
| display_name | 姓名/脱敏展示名 | ✅ | 列表默认脱敏（如 张**），明细按权限 |
| organization | 所属组织 | ✅ | 公司/部门/项目部 |
| specialty | 专业 | ✅ | 建筑工程/市政/公路等 |
| reg_cert_type | 注册证书 | ✅ | 一级建造师/二级建造师等 |
| reg_cert_no | 注册编号 | ✅ | 唯一，校验格式 |
| cert_level | 证书等级 | ✅ | 一级/二级 |
| cert_valid_until | 证书有效期 | ✅ | 过期自动 expired |
| edu_safety_status | 继续教育/安全证书状态 | ✅ | valid / expiring / expired / pending |
| eligible_project_types | 可担任项目类型 | ✅ | 与项目类型匹配 |
| region_restriction | 地区限制 | — | 如仅限河北省 |
| performance_refs | 历史业绩 | — | 回链 F006 PerformanceRecord |
| active_projects | 当前在建项目 | — | 项目ID + 预计结束 |
| expected_available_at | 预计可用日期 | — | |
| availability | 可用状态 | ✅ | available / occupied / planning |
| credit_penalty_status | 信用/处罚状态 | — | 仅限企业合法维护范围（有证据） |
| evidence_refs | 证据文件引用 | ✅ | 注册证书、社保、继续教育证明、业绩证明 |
| verified_at | 最后核验时间 | — | |
| data_owner | 资料责任人 | ✅ | |
| status | 经理状态 | ✅ | active / unavailable / expired / pending_verification / archived |
| recommendation_role | 推荐角色 | — | primary / backup（主推荐/备选，投标专员设置，留存审计） |

### 11.4 规则版本（RuleSet）`[schema]`

`rule_set_id` / `rule_version` / `effective_from` / `created_by`——规则版本化（F008 §4.3）：历史项目保留当时规则版本快照；规则变更留审计（谁/何时/改了什么）。

### 11.5 四类结论与内部满分准入 `[rule + formula]`

```text
qualification_result = all(hard_requirements == satisfied)
operational_readiness.approval_ready =
  all(actions required_by_stage=approval_ready are ready/completed)
internal_admission_eligible =
  qualification_result.status == passed
  AND scoring_result.internal_full_score_ready
  AND operational_readiness.approval_ready
  AND exists(qualified_available_project_manager)
  AND all(required_evidence is valid_at_as_of)
  AND result_freshness == current
```

- 判定结果字段：`qualification_result`、`scoring_result`、`operational_readiness`、`internal_admission_result`、`internal_admission_eligible`、`result_freshness`；以及阻断/待补/复核清单。
- `internal_full_score_ready` 是企业内部政策，不是评标委员会实际评分。报价或其他输入缺失时为 `not_calculable`；主观项仅可记录内部质量评审状态。
- **内部准入为真才进入 `pending_bid_approval`，绝不自动投标**（ADR-001 §2.3）。

---

## 十二、准入状态机域（v0.4.0 新增，ADR-001 §2.2）

### 12.1 准入状态 `[schema]`

`admission_status`（enum，必填）：

```text
draft
  → collecting
  → parsed
  → matching
  → blocked_missing_data          （数据缺失/证据不足，默认阻断）
  → blocked_hard_requirement      （响应性硬要求明确不满足）
  → not_qualified                 （明确不满足资格）
  → qualified_full_score          （内部满分准入政策满足，待送审批）
  → pending_bid_approval          （仅进入人工审批，不代表已投标）
  → approved_for_bidding / rejected_by_approver
  → archived
```

### 12.2 状态迁移规则 `[rule]`

| 迁移 | 触发 | 说明 |
|---|---|---|
| draft → collecting | 人工创建任务并开始导入 | 仅 manual_trigger |
| collecting → parsed | 原文入库并解析完成 | 解析失败 → 人工复核，不自动前进 |
| parsed → matching | 匹配任务启动 | 企业资料不足 → blocked_missing_data |
| matching → blocked_missing_data | 关键证据缺失 | 默认阻断，不推断满足 |
| matching → blocked_hard_requirement | 响应性硬要求证据充分但明确不满足 | 一票否决 |
| matching → not_qualified | 明确不满足资格 | 证据充分的不满足 |
| matching → qualified_full_score | F008 的内部准入政策全满足 | 非评标委员会得分 |
| qualified_full_score → pending_bid_approval | 进入人工审批队列 | 满分不等于自动投标 |
| pending_bid_approval → approved_for_bidding / rejected_by_approver | 投标负责人审批/驳回 | 可附人工豁免 |
| approved_for_bidding / rejected_by_approver → archived | 归档 | 保留审计 |

### 12.3 人工豁免（Waiver）`[schema]`

| field_key | 中文 | 必填 | 说明 |
|-----------|------|:--:|------|
| waiver_id | 豁免ID | ✅ | 主键 |
| authorizer | 授权人 | ✅ | 审批人 |
| reason | 原因 | ✅ | 必填 |
| evidence_refs | 证据 | ✅ | 在途材料/受理回执等，必填 |
| valid_until | 有效期 | ✅ | 必填，过期自动失效 |
| approved_at | 审批时间 | ✅ | |
| covered_items | 覆盖的阻断项 | ✅ | 具体豁免哪项 |

> 豁免不改变"满分"定义（ADR-001 §2.5），只允许带着豁免项进入人工审批视野；豁免到期未批准 → 回到阻断状态。

### 12.4 审批记录（ApprovalRecord）`[schema]`

| field_key | 中文 | 必填 | 说明 |
|-----------|------|:--:|------|
| approval_id | 审批ID | ✅ | 主键 |
| project_id | 项目ID | ✅ | 回链主卡 |
| approver | 审批人 | ✅ | 经营负责人 |
| decision | 决策 | ✅ | approved / rejected / waived |
| decided_at | 决策时间 | ✅ | |
| comment | 意见 | — | 驳回时必填 |
| admission_result_ref | 关联准入结果 | ✅ | 回链 §11.5 |

> 审计要求（F009 §6.4）：全部动作留痕——谁、何时、依据什么、结论；审计记录不可删改；审批层数据仅审批人可见（F003 权限矩阵 approver_only）。

---

## 十三、数据治理域（v0.5.0 新增，F003 契约冻结）

> **边界声明**：本章为 R003（F003-最小数据治理与权限）的落地层——系统同时持有公开原文与企业私有资料，所有材料统一纳入 Material 治理；公开/私有严格分层隔离；raw 原文不可覆盖；所有匹配结论回链招标条款 + 企业证据。不实现完整权限系统（Iteration 3 冻结实现方案，随薄切片落地），本章只冻结契约。

### 13.1 Material 契约 `[schema]`

最小数据治理单元：公告、招标文件、企业资质、业绩、人员证照、证据文件。字段契约与 F003 §4.1 逐项对齐：

| field_key | 中文 | 类型 | 必填 | 说明 |
|-----------|------|:--:|:--:|------|
| material_id | 材料ID | string | ✅ | 主键，如 MAT-PUB-xxx / MAT-PRV-xxx（前缀区分归属） |
| material_type | 材料类型 | enum | ✅ | announcement / tender_document / qualification_cert / performance_record / personnel_cert / evidence_file |
| source_type | 来源类型 | enum | ✅ | official_platform / agency / uploaded / internal / manual_entry |
| owner_type | 归属类型 | enum | ✅ | public / enterprise（公开/私有严格分层） |
| classification | 密级 | enum | ✅ | public / internal / confidential |
| permission_scope | 权限范围 | enum | ✅ | public_read / enterprise_read / restricted / approver_only（逐材料生效） |
| content_hash | 原文哈希 | string | ✅ | SHA-256，防篡改；不一致 = 篡改风险，阻断使用 |
| version | 版本 | int | ✅ | 从 1 递增，不覆盖历史 |
| imported_at | 导入时间 | datetime | ✅ | |
| valid_until | 有效期 | date | — | 证书/证照必填；过期标 expired，不得用于满分判定 |
| parse_status | 解析状态 | enum | ✅ | pending / parsed / partial / failed / manual_review |
| evidence_refs | 证据文件引用 | ref[] | — | 回链 evidence_file |
| data_owner | 数据责任人 | string | ✅ | 谁负责维护/核验 |
| verified_at | 最后核验时间 | datetime | — | |
| status | 状态 | enum | ✅ | active / expired / archived / invalid |

### 13.2 权限矩阵 `[table]`

目标态（Iteration 3 冻结实现，F003 §6.2）：

| 数据 | public_read 公开读取 | enterprise_read 企业内读取 | restricted 受限（匹配层） | approver_only 仅审批人 |
|---|---|---|---|---|
| 公开原文/事实卡 | ✅ | ✅ | ✅ | ✅ |
| 企业资质/业绩 | — | ✅ | ✅ | ✅ |
| 项目经理个人信息 | — | 脱敏 | 匹配结果 | 明细 |
| 审批/豁免/审计 | — | — | — | ✅ |

- `permission_scope` 逐材料生效；权限不足 → 拒绝访问 + 审计日志。
- 项目经理个人信息最小化：列表默认脱敏（如 张**），明细按权限。

### 13.3 隔离与不可变约束 `[rule]`

1. 公开数据（`owner_type=public`）与企业私有数据（`owner_type=enterprise`）**严格分层存储**（不同目录/表空间 + 权限矩阵），禁止混存。
2. `raw` 原文**不可覆盖**：每次导入为新增版本（`version` 递增）；公告变更/澄清按 `project_id` 挂接（关联 §7.4），不覆盖历史版本。
3. 原文哈希不一致（`content_hash` 校验失败）= 篡改风险 → 阻断使用 + 告警（F003 §7）。
4. Git 白名单（`.gitignore`）保证企业资料、招标文件、原始抓取内容不入库（AGENTS.md §3.6）。

### 13.4 缺失/过期处置 `[rule]`

- 企业证书、人员资质、业绩必须有：有效期、证据文件、数据责任人；缺失字段一律 `pending_verification` / "待补/待核实"，**禁止编造**。
- 数据缺失/无法核验 → 匹配层输出"不可判定/待补材料"，默认阻断（`blocked_missing_data`，ADR-001 §2.4），不得推断满足。
- 数据过期（`valid_until` 已过）→ 标 `expired`，不计入满分判定（F008）。
- 解析失败/部分解析（`parse_status ∈ {failed, partial}`）→ 人工复核，不进入匹配。

---

- **changelog**：本文件每次版本升级记录变更点，不可虚报（声称补齐的章节数必须与实际一致）。
- **活文档声明**：每次管道运行后发现此 schema 不足 → 立即补充。
- **域定制说明**：本 schema 为招投标行业流程定制版——删除智库"报告撰写/SCQA/作战地图/分析框架"整章（信息搜集 Agent 无分析产出）；新增"采集与合规"章与"Pipeline 门禁 G0-G6′"章。字段 key 与 PRD §六 严格对齐，禁止引入 PRD 之外的字段。

---

## 变更记录

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-08-13 | v0.0.0 | 初版：按复用方案 §四 骨架树 8 大章全量填充，基于 PRD_v1 数据模型 |
| 2026-08-13 | v0.0.0 | 通过 domain-schema-generator-v2 门禁（coverage 51/51，placeholder 0），schema_draft_v0 → schema.md 转正 |
| 2026-08-13 | v0.1.0 | **优化（MVP 反馈）**：①新增 §2.2.7 时效优先级分级（P0/P1/P2/不予采纳，基准=报名截止日）；②G0 门禁接入时效判定，已过报名期不入库；③推送卡片规格=主卡同等详细+中文表头（§6.4 G4）；④主卡字段新增 `priority` |
| 2026-08-17 | v0.2.0 | **发布与归档层改造（任务书）**：①新增 §十（发布与归档层）：四类数据对象（AnnouncementEvent/DailyIssue/SourceRun/WeeklySourceReport）+ 日期口径（publish_time ∈ [D, D+1) 北京时间）+ publish_time 日报准入必填 + 迟到公告（late_discovered，策略配置化）+ 跨平台指纹去重（含主来源优先级）+ 紧迫度规则（24h/72h）+ 展示白名单/内部字段隔离 + 六类数据结构 + UNIQUE(daily_issue_id, announcement_event_id) + 幂等与版本管理；②发布层配置落 config/publish_config.yml；③兼容策略：纯新增章节，不改 §一~§九 既有规则与字段含义 |
| 2026-08-17 | v0.2.1 | **产物形式调整（用户决策）**：日报/周报改为 **Markdown 报告**存储（报告/日报、报告/来源与覆盖），不再生成 HTML（不部署网站）；报告按骨架树六维分组展示全部可找到字段，缺失字段与人工审核项汇总文末提醒；推送简报仅含客户可见结果；sync_events 事件创建合并 intake frontmatter 业务字段 |
| 2026-08-17 | v0.2.2 | **修复渲染层数据越权（用户优化建议）**：①客户渲染唯一输入 = DailyIssue.items（新增 build_customer_context 统一接口，禁止客户渠道扫描全量事件库）；②删除客户日报"迟到公告"章节，历史公告标题/时间/链接零出现，文末仅保留统计数字；③渲染前后硬性校验（输入 ⊆ items / 输出标题边界 / 无后台状态 / item_count 一致）；④late_announcement_policy 收敛 backend_only（supplement_section/urgent_only 仅内部审计预览）；⑤模板/预览/测试同步修正，新增客户边界回归测试 |
| 2026-08-18 | v0.2.3 | **数据源注册表升级（依据《京津冀招投标平台对照表-20260817》实测筛选）**：①§1.4 平台注册表由 4 行占位扩展为 24 行全量清单（国家级 L1 3 个 + 北京 L1 3 个 + 天津 L1 2 个 + 河北 L1 4 个 + 河北 L2 11 市 + L0 第三方 4 个），含 URL/采集方式/实测备注；②明确非公告源剔除名单（住建厅/四库一平台/采购与招标网/张家口保函平台/政采地市子站）；③固化 4 条实测认知：公告采集免费免登录（招标投标法 16 条）、工程主战场=省市公共资源平台、政采中小项目=ccgp 三省市分站、商业平台非必需；④纠偏：河北旧域名 `ggzy.hebei.gov.cn` 已注销 → `szj.hebei.gov.cn/hbggfwpt/`；`bidcenter.com.cn` 是采招网 ≠ 比地（比地=`bidizhaobiao.cn`）；本机代理 127.0.0.1:7890 失效 → 采集脚本 trust_env=False |
| 2026-08-18 | v0.2.4 | **采集策略收敛（用户决策）**：①L2 各地市**不区分优先级全部纳入采集清单**（覆盖 v0.2.3"待圈定优先级"表述），逐个过合规评估后启用；②**千里马/剑鱼免费资源纳入日常巡检**（免注册可看标题/编号/摘要）作官方源补充线索，付费 API 仍待合同、不阻塞采集；③聚合平台日期不可直接采信（官方验证后才计当日） |
| 2026-08-18 | v0.3.0 | **日报准入口径重构（用户决策：放宽标准）**：①准入 = **可参与性**（deadline_signup 或 deadline_bid 任一未过 → 当天仍可报名/下载/投递 → 收录），**不再限定"当天发布"**——历史发布但截止未过的公告同样推送（§10.2）；②**已推送去重**：扫描历史日报 items 排除已推送事件，同一公告绝不重复推送（§10.3，`daily_issue.exclude_pushed`）；③截止已过 → excluded/deadline_expired 后台；截止缺失 → 复核队列 missing_deadline（替代 missing_publish_time）；④渲染文案同步（可参与公告/当日无新增可参与公告）；⑤测试 48→53 项适配新口径 |
| 2026-08-18 | v0.3.1 | **推送卡片排版优化（参考 Google/Facebook 通知风格）**：①每条公告标序号（1. 2. …，序号+优先级标签）；②去多余空行（分组间不空行、卡片间单空行）；③九段分组结构与字段内容不变、有值才展示、文末待补充汇总保持；④条目数校验正则改 `^\d+\.`；⑤模板同步；⑥重新推送 PUSH-20260818-003 成功（本行补录，依据 .pipeline_state.yml phase_e step_03） |
| 2026-08-25 | v0.4.0 | **资格与资源核查域 + 准入状态机域（ADR-001 范围变更落地，R002）**：①新增 §十一（资格与资源核查域）：匹配矩阵 7 字段（§11.1，含 match_result 正式枚举，登记与旧优势卡占位差异）、三类要求（§11.2）、项目经理匹配 21 字段 + 硬条件验证（§11.3，对齐 F007）、规则版本（§11.4）、满分准入公式（§11.5，对齐 F008 §6.1）；②新增 §十二（准入状态机域）：admission_status 12 态（ADR-001 §2.2）、迁移表（§12.2）、人工豁免 7 字段（§12.3，F009 §4.2）、审批记录 7 字段（§12.4，F009 §4.1）；③门禁新增 **G3.5 匹配门禁**（§6.4，F008 §6.4 定稿命名）：准入链路独立门禁，不阻断公开情报推送；④骨架树同步扩展第 9/10 章（tender_skeleton_tree.json 重新生成，scripts/build_skeleton_tree.py）；⑤全量审计通过：coverage 107/107、placeholder 0、无重复定义（见 _schema_generation_log.md）；⑥frontmatter version 0.2.0 → 0.4.0（对齐 .pipeline_state.yml 既有 v0.3.x 演进，v0.3.1 补录）；⑦兼容策略：本章为纯新增，不改动 §一~§十 既有规则与字段含义 |
| 2026-08-25 | v0.5.0 | **数据治理域（R003，F003 契约冻结）**：①新增 §十三（数据治理域）：Material 契约 15 字段（§13.1，对齐 F003 §4.1，枚举全冻结）、权限矩阵（§13.2，F003 §6.2 目标态，Iteration 3 冻结实现）、隔离与不可变约束（§13.3：公开/私有分层、raw 不可覆盖、哈希防篡改）、缺失/过期处置（§13.4：待补/待核实、过期不计满分、解析失败人工复核）；②骨架树同步新增第 11 章（数据治理域：11.1 十五字段 + 11.2 权限矩阵 + 11.3 隔离约束 + 11.4 缺失处置，tender_skeleton_tree.json 重新生成，scripts/build_skeleton_tree.py）；③全量审计通过：coverage 125/125、placeholder 0、无重复定义（见 _schema_generation_log.md §九）；④术语表 Material 词条补源（F003 §4.1）；⑤兼容策略：本章为纯新增，不改动 §一~§十二 既有规则与字段含义；权限系统实现仍属 Iteration 3 非目标（F003 §2） |
| 2026-08-26 | v0.5.1 | **R008 行业规则补强**：对齐 ADR-001 v1.1/F008 v1.3，G3.5 与 §十一/§十二改为资格/评分/准备度/内部准入四类结论；补 `manual_review`、缺失与明确不满足的唯一处置、规则时点/标段/联合体/澄清版本、阶段动作和内部满分边界。骨架树已重新生成并通过结构校验/公告冒烟测试；v1.3 行业黄金样本语义验证仍待执行，不复用 v0.5.0 的 coverage 结论。 |
