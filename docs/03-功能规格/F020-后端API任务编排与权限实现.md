# F020-后端 API、任务编排与权限实现

- **需求来源**：R020 ｜ **状态**：定稿草案 v1.2（2026-08-31）
- **关联**：F003-F010、F017-F019、F025、ADR-001、ADR-002

## 1. 目标

把已冻结的目标态 API 变为可调用服务，并用统一任务模型串联上传、解析、导入、匹配和审批。接口不改变业务字段和状态机定义。

## 2. API 分组

| 分组 | 最小接口 |
|---|---|
| 材料 | `POST/GET /api/v1/materials`、`GET /{id}/versions`、`POST /{id}/verify` |
| 搜索/推送 | `POST /api/v1/intake/announcement/search`、`GET /api/v1/intake/announcement/search/{job_id}`（测试期 `manual_trigger`；输出结构化公告事实卡） |
| 入库 | `POST /api/v1/intake/announcement`、`POST /api/v1/intake/tender-document`、`GET /api/v1/intake/{id}`、`POST /api/v1/intake/{id}/reparse` |
| 解析/OCR | `POST /api/v1/ocr/route`、`GET /api/v1/ocr/jobs/{id}`、`GET /api/v1/ocr/review-queue`、`POST /api/v1/ocr/review/{id}` |
| 企业资料 | `POST /api/v1/qualifications`、`/performances`、`/personnel`、`/evidences` |
| 知识库/检索 | `POST /api/v1/knowledge/search`、`GET /api/v1/knowledge/indexes/{material_id}/{version}`、`GET /api/v1/retrieval-runs/{id}`；检索结果仅候选证据，受 F025 权限过滤 |
| 匹配 | `POST /projects/{id}/match`、`GET /projects/{id}/match-runs/{run_id}`、`GET /projects/{id}/match-runs/latest`（`POST match` 仅由任务编排器或重算流程调用） |
| 结果/队列 | `GET /api/v1/projects/{id}/matrix`、`GET /api/v1/projects/{id}/admission`、`GET /api/v1/projects/{id}/queues` |
| 审批 | `GET /api/v1/approvals/pending`、`POST /api/v1/projects/{id}/approval/approve`、`/reject`、`/waivers`、`GET /api/v1/projects/{id}/audit` |

所有响应包含 `request_id`；异步接口返回 `job_id`，通过 `GET` 查询结果，不长连接等待大文件处理。

`POST /api/v1/knowledge/search` 请求必须含 `knowledge_layers`、`permission_scope` 和查询上下文；涉及 L2/L3 时必须含 `project_id`、`as_of`，有标段时含 `lot_id`。响应必须含 `candidate_only=true`、Material/版本/哈希/页码引用；不得接受或返回准入结论。匹配服务可调用该接口或内部等价服务，前端不得据其绕过 F023 规则执行。

检索 API 的 GET/POST 不得创建匹配 run 或改变项目状态；索引任务仅由材料版本入库、解析完成或核验通过事件创建，并使用 `material_id + version + embedding_model + index_version` 幂等键。

## 2.1 原型页面到 API 的联通顺序

| 原型页面 | 前端调用 | 后端动作与下一步 |
|---|---|---|
| 搜索与推送 `index.html` | `POST announcement/search` → 轮询 `GET .../search/{job_id}` | 仅采集并保存公开公告事实；返回结构化推送和 `project_id`，不触发解析、匹配或准入。 |
| 选择与解析 `import.html` | `POST /api/v1/intake/tender-document`（绑定 `project_id`、公告 Material、文件 hash）→ 轮询 `GET /api/v1/intake/{id}` | 文件固化成功后创建解析任务；未上传完整文件不得创建解析任务。解析成功发布 `parse.completed`。 |
| 自动匹配 `matrix.html` | `GET /api/v1/projects/{id}`、`GET /api/v1/projects/{id}/requirements`、`GET /api/v1/projects/{id}/match-runs/latest` | 前端只读展示解析产物和一次自动匹配结果；匹配由 `parse.completed` 触发，不提供“选择资料”或手动匹配按钮。 |
| 风险与缺失 `score.html` | `GET /api/v1/projects/{id}/matrix`、`GET /api/v1/projects/{id}/admission`、`GET /api/v1/projects/{id}/queues` | 展示风险、缺失、阻断和证据；不触发新匹配。 |
| 人工补录 `queue.html` | `POST materials` / `POST .../verify`；补录核验后 `POST projects/{id}/recalculate` | 新版本先进入 `pending_verification`；核验通过后以新证据快照重算，旧结果标记 `stale`。 |
| 人工审核 `approval.html` | `GET approvals/pending`、审批/驳回/豁免接口、`GET audit` | 仅最新有效且 `internal_admission_eligible=true` 的结果可进入审批；所有动作追加审计。 |

`materials.html` 仅供数据管理员维护企业资料，不作为项目流程页面；其读写不由投标专员在单个项目内发起资料选择。

## 2.2 接口数据契约（v1.2，按最新原型 `prototype/data.js` 字段对齐）

> 原则：响应字段名与原型 `data.js` 保持一致，前端可直接渲染；枚举值对齐 `STATE_META`（13 态）、`MATCH_META`（4 态）、`ROLES`（4 角色）；时间一律 ISO 8601；缺失字段输出 `null` 或"待补/待核实"，禁止编造；分页统一 `?limit=&offset=`，响应 `{items, total, limit, offset}`。

### 2.2.1 搜索与结构化推送（`index.html`）

**`POST /api/v1/intake/announcement/search`** —— 测试期 `manual_trigger`，遵守限频/robots/透明 UA；只保存公开公告事实，不触发解析/匹配/准入。

```json
// 请求
{ "keyword": "房屋建筑施工", "region": "河北省",
  "sources": ["hebei_ggzy"], "collect_mode": "manual_trigger" }
// 响应（异步，先返回 job_id）
{ "request_id": "r-…", "job_id": "jb-…" }
```

**`GET /api/v1/intake/announcement/search/{job_id}`** —— 轮询任务结果，成功返回结构化公告事实卡数组（对齐 `PROTOTYPE.projects` 卡片字段）：

```json
{ "request_id": "r-…", "status": "succeeded",
  "items": [ { "project_id": "ND-2025", "project_name": "河北农业大学东校区研究生宿舍建设项目施工",
      "state": "collecting", "region": "保定市", "project_type": "房屋建筑工程 · 公开招标 · 资格后审",
      "budget_cap": "12,471.59 万元", "bond": "20 万元", "bid_validity": "120 日历天",
      "deadline": "2025-10-30 09:00", "source": "河北省公共资源交易服务平台",
      "source_url": "https://ggzy.hebei.gov.cn/…", "parse_status": "pending",
      "updated": "2026-08-28", "note": null } ] }
```

**`GET /api/v1/projects`** —— 结构化推送列表（同卡片字段，追加 `is_main`、`clarify_count`、`tender_file`）；`state` 取 13 态之一。

### 2.2.2 选择与解析（`import.html`）

**`POST /api/v1/intake/tender-document`** —— multipart 上传完整招标文件（docx/pdf）。必填：`file`、`project_id`、`material_type=tender_document`、`source_type`、`data_owner`、`Idempotency-Key`。**未上传完整文件（缺 file 或空文件）→ 400 `invalid_request`，不创建解析任务**；`.gef/.etb` → 400 `unsupported_format`；sha256 重复 → 200 返回既有版本（`created=false`），不重复建任务。响应：

```json
{ "request_id": "r-…", "job_id": "jb-…", "material_id": "MAT-ND-001",
  "version": 1, "content_hash": "9c42…77be", "created": true }
```

**`GET /api/v1/intake/{id}`** —— 入库任务状态（`status` + `parse_status`：pending/parsed/partial/failed/manual_review）。

**`GET /api/v1/projects/{project_id}`** —— 项目主卡 + 原文与解析状态表（对齐 `import.html` 原文表）：`project`（名称/状态/来源/截止/限价/澄清数）+ `materials[]`（对齐原型表列：材料名、版本、解析状态、sha256 前 16 位、查看原文 object_uri）。多版本按 `project_id` 挂接、只增不覆盖。

### 2.2.3 解析与自动匹配（`matrix.html`）

**`GET /api/v1/projects/{project_id}/requirements`** —— 20 条要求（对齐 `PROTOTYPE.requirements`，含证据抽屉所需字段）：

```json
{ "request_id": "r-…", "items": [ { "requirement_id": "NQ-H-001", "req_type": "hard_requirement",
      "category": "资质", "clause_ref": "招标公告 §3.2",
      "assertion": "建筑工程施工总承包二级及以上（不接受资质预警/异常企业）",
      "match": "satisfied", "match_reason": "建筑工程施工总承包 特级 ≥ 二级（证据：资质证书扫描件 OCR 结构化）",
      "evidence": [ { "kind": "企业证据", "name": "建筑业企业资质证书（建筑工程施工总承包 特级）",
          "detail": "扫描件 OCR 结构化 · 证书编号 D213035404 · 有效期至 2029-09-11",
          "ref": "E-Q-001", "conf": "置信度 0.92 · 第 1 页" },
                    { "kind": "条款", "name": "招标公告 §3.2 投标人资格要求（1）",
          "detail": "须具备建设行政主管部门核发的建筑工程施工总承包二级及以上资质",
          "ref": "clause:NQ-H-001" } ],
      "max_score": null, "score": null } ] }
```

`match` 枚举对齐 `MATCH_META`：`satisfied / not_satisfied / unverifiable / manual_review`；`req_type` 对齐 F008：`hard_requirement / scored_requirement / action_requirement`（原型 tab：hard/scored/action）。

**`GET /api/v1/projects/{project_id}/match-runs/latest`** —— 最近一次匹配运行：`{run_id, rule_set_id, rule_set_version, as_of, mode, coverage, status, created_at}`。**只读，不创建 run**；无 run 时返回 `{run_id: null}`（前端显示"尚未匹配"）。

### 2.2.4 风险与缺失（`score.html`）

**`GET /api/v1/projects/{project_id}/admission`** —— 准入摘要（对齐 `PROTOTYPE.admission`）：

```json
{ "request_id": "r-…", "admission": { "qualification": "pending",
    "hard_satisfied": 11, "hard_total": 12, "scoring": "not_full",
    "objective_score": 5, "objective_max": 5, "readiness": "ready",
    "action_done": 4, "action_total": 4, "eligible": false, "state": "blocked_missing_data",
    "missing": [ { "req": "NQ-H-007", "text": "缺少 2022/2023/2024 年度财务审计报告或银行资信证明（素材缺口，待公司补充）" } ],
    "review": [ { "req": "NQ-S-001", "text": "技术标主观项：待内部质量评审，评审前不计入内部满分" } ],
    "manager": { "manager_id": "M-ND-01", "role": "主推荐", "check": "passed" },
    "price": { "bid": 121598056.76, "cap": 124715908.89 } } }
```

**`GET /api/v1/projects/{project_id}/matrix`** —— 矩阵页合并视图：requirements + 逐条 match_items（含证据链）；不触发新匹配。

**`GET /api/v1/projects/{project_id}/queues`** —— 三类处置队列（对齐 `queue.html`）：

```json
{ "request_id": "r-…", "queues": {
  "blocked_hard_requirement": [ { "project_id": "GZ-2024", "requirement_id": null,
      "clause": "招标公告 §3.4", "text": "安全生产许可证 有效期至 2026-06-30（已过期）→ 一票否决" } ],
  "blocked_missing_data":   [ { "project_id": "ND-2025", "requirement_id": "NQ-H-007",
      "clause": "招标公告 §3.13② / 资格审查表 #11", "text": "缺少 2022/2023/2024 年度财务审计报告或银行资信证明" } ],
  "manual_review":          [ { "project_id": "ND-2025", "requirement_id": "NQ-S-001",
      "clause": "第三章 四(2)", "text": "技术标主观项待内部质量评审" } ] } }
```

### 2.2.5 人工补录与重算（`queue.html`）

**`POST /api/v1/materials`** —— 补录材料上传（multipart）：`owner_type=enterprise`，核验前 `status=pending_verification`；响应含 `material_id/version/content_hash`。

**`POST /api/v1/materials/{material_id}/verify`** —— 核验通过（`actor`、`data_owner` 必填）→ `status=active`，写审计。

**`POST /api/v1/projects/{project_id}/recalculate`** —— 补录核验后重算：以新证据版本创建新匹配 run，旧 `admission_results` 标记 `stale`；幂等（同一证据版本重复调用返回既有 run）。响应 `{job_id, run_id?}`。

**`POST /api/v1/projects/{project_id}/match`** —— 仅任务编排器（`parse.completed` 触发）或重算流程内部调用；**前端不暴露手动匹配按钮**（F020 §2.1）。

### 2.2.6 人工审核与审计（`approval.html`）

**`GET /api/v1/approvals/pending`** —— 待审批列表，**仅经营负责人**；其余角色 403 `forbidden`（对齐原型 403 模拟页）。响应：`[{approval_id, project_id, project_name, state, admission_result_ref, created_at}]`。

**`POST /api/v1/projects/{project_id}/approval/create`** —— 创建审批：仅当最新结果 `internal_admission_eligible=true`（满分）才可创建；非满分 → 403 `invalid_state_transition`（对齐原型"非满分创建被拒绝"）。

**`POST /api/v1/projects/{project_id}/approval/approve`** —— `{approver, basis, comment?}`；终态不可重复决策。

**`POST /api/v1/projects/{project_id}/approval/reject`** —— `{approver, comment, basis?}`；**`comment` 必填**，为空 → 422 `invalid_request`（对齐原型"驳回意见必填"）。

**`POST /api/v1/projects/{project_id}/waivers`** —— 豁免登记：`{authorizer, reason, evidence_refs[], valid_until, covered_items[]}`；四字段必填（对齐原型表单校验），过期自动失效回阻断 `blocked_waiver_expired`。

**`GET /api/v1/projects/{project_id}/audit`** —— 审计时间线（对齐 `PROTOTYPE.approvals[].audit`）：

```json
{ "request_id": "r-…", "items": [ { "at": "2026-08-28T10:30:00+08:00", "actor": "经营负责人",
      "action": "approve", "basis": "admission:ND-2025:v1.0.0", "outcome": "approved_for_bidding" } ] }
```

### 2.2.7 企业资料库后台（`materials.html`，数据管理员）

| 接口 | 响应字段（对齐 `PROTOTYPE.company_data`） |
|---|---|
| `GET /api/v1/enterprise/qualifications` | `[{name, level, valid_until, status, evidence, selected}]`（status：active/expired/pending_verification/archived） |
| `GET /api/v1/enterprise/safety-license` | `{name, no, valid_until, status}` |
| `GET /api/v1/enterprise/managers` | `[{id, display_name, specialty, reg_cert_type, b_cert, social_security, active_project, status, role, similar_perf}]` |
| `POST /api/v1/qualifications`、`/performances`、`/personnel`、`/evidences` | 数据管理员维护；缺证据默认 `pending_verification`；与项目匹配无关 |

角色过滤再按 F003 `permission_scope` 收紧；`enterprise_data` 明细不对投标专员/法务开放。

## 3. 请求校验与幂等

- 上传必须声明 `material_type`、`owner_type`、`classification`、`data_owner` 和 `collect_mode`。
- 文件大小、扩展名、MIME、SHA-256、来源合规状态校验失败即 4xx；不支持 `.gef/.etb` 返回 `unsupported_format`。
- `Idempotency-Key` 与内容哈希联合去重；重复请求返回既有 Material，不重复创建任务。
- 所有人工修正必须携带 actor、reason、basis；影响匹配结果时先创建新 Material/证据版本，完成核验后由重算流程创建新的匹配任务，不能由结果页隐式触发。

## 4. 任务状态

`queued → running → succeeded`；失败为 `failed`，可重试任务为 `retryable`，人工介入为 `manual_review`，取消为 `cancelled`。任务状态与项目准入状态分离：任务失败不得自动迁移业务状态。

worker 领取任务使用数据库锁和租约；超时任务由恢复器重新排队。每个任务保存输入 Material/版本、规则版本、输出引用和错误码。

## 5. 角色与权限

| 角色 | 允许操作 |
|---|---|
| 投标专员 | 搜索/查看公开事实、上传完整招标文件、查看解析与匹配结果、提交补录/复核请求 |
| 数据管理员 | 企业资料/证据导入、核验、过期处理、OCR 复核 |
| 经营负责人 | 查看完整匹配结果、审批/驳回/豁免 |
| 法务 | 数据源和合规记录只读/审核 |

权限按 F003 `permission_scope` 再过滤，不能仅依赖前端隐藏按钮。每次拒绝访问写入 `audit_events`，个人信息默认脱敏。

## 6. 错误码

至少统一：`invalid_request`、`forbidden`、`not_found`、`duplicate_material`、`hash_mismatch`、`unsupported_format`、`parse_failed`、`manual_review_required`、`invalid_state_transition`、`dependency_unavailable`、`internal_error`。

## 7. 验收与测试

- OpenAPI 文档与 F004-F010/F017 规格字段一致，错误响应结构统一。
- 四角色分别执行正向和越权用例；越权不泄露资源是否存在之外的信息。
- 重复上传、worker 重启、超时恢复和非法状态迁移均可复现且有审计。
- API 集成测试使用脱敏农大数据，禁止读取 Git 忽略目录之外的真实文件。
- 联通验收必须验证：无完整招标文件不产生解析/匹配任务；解析成功只产生一个自动匹配 run；补录核验后只产生带新证据版本的新 run；结果页 GET 请求不得隐式创建 run。

## 8. 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-08-31 | v1.0 | 新增后端 API、任务状态、幂等、RBAC 和错误处理操作规范 |
| 2026-08-31 | v1.1 | 对齐原型主流程：增加搜索/结构化推送入口，明确解析完成自动触发一次匹配、结果页只读、补录核验后重算及前后端页面/API 映射；移除投标专员逐项目选择资料和直接发起匹配。 |
| 2026-08-31 | v1.2 | 补充 §2.2 接口数据契约：按最新原型 `prototype/data.js` 字段对齐各接口请求/响应 schema（搜索推送、选择解析、自动匹配、风险缺失、补录重算、审批审计、资料库后台）；枚举对齐 STATE_META/MATCH_META/ROLES；明确驳回 comment 必填、审批创建满分门槛、结果页只读等约束。 |
| 2026-09-01 | v1.3 | 增加 F025 知识库检索/索引可观测接口与候选证据契约；禁止检索接口隐式触发匹配或返回准入判定。 |
