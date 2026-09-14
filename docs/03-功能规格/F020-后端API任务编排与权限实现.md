# F020-后端 API、任务编排与权限实现

- **需求来源**：R020 ｜ **状态**：定稿草案 v1.13（2026-09-08）
- **关联**：F003-F010、F017-F019、F021-F025、ADR-001、ADR-002

## 1. 目标

把已冻结的目标态 API 变为可调用服务，并用统一任务模型串联上传、解析、导入、匹配和审批。接口不改变业务字段和状态机定义。

## 2. API 分组

| 分组 | 最小接口 |
|---|---|
| 材料 | `POST/GET /api/v1/materials`、`GET /{id}/versions`、`POST /{id}/verify` |
| 搜索/推送 | `POST /api/v1/intake/announcement/search`、`GET /api/v1/intake/announcement/search/{job_id}`（测试期 `manual_trigger`；真实抓取已启用 L1 源列表页 → 候选事实卡）、`POST /api/v1/intake/announcement/candidates/{candidate_id}/import`、`GET /api/v1/intake/announcement/candidates/{candidate_id}`（候选确认→详情原文入库） |
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

**`POST /api/v1/intake/announcement/search`** —— 测试期 `manual_trigger`，遵守限频/robots/透明 UA；由 worker 真实抓取已启用 L1 源列表页 → 候选落库（`announcement_candidates`）。**搜索只产生候选，不创建 Project、不触发解析/匹配/准入**；候选确认入库后（见下）才建档。

> v1.7（09-优化方案 §3.1）：「种类」「规模」**不再是搜索请求的采集过滤条件**——联网检索先返回完整候选集；种类/规模只在候选结果内做列表筛选（见下 `candidates` 接口与前端筛选栏）。请求体若仍携带 `category`/`scale`，服务端忽略且不写入任务（不缩小联网检索范围）。
>
> v1.10（源覆盖与匹配增强）：① 关键词过滤改**确定性多词 AND + 全半角/空白归一**（`registry.keyword_matches`：关键词按空白切词，归一后各词在标题中连续子串命中才入选；空关键词恒命中）——减少「词没写全整串失败」，仍是确定性匹配不做模型推断；② 源注册表扩展惠招标同平台**服务/货物/非招标分区**（`hebtig_service`/`hebtig_goods`/`hebtig_nzb`，同域同结构实测）；③ 带 `search_param` 的源在有关键词时抓**平台站内检索结果页**（`?keyword=…`，robots 预检仍按默认列表路径，query 不改变 robots 路径匹配），无关键词回落默认列表页——一次任务 1 次抓取覆盖服务端关键字过滤结果，本地多词 AND 兜底。

```json
// 请求（只定义联网检索范围：关键词/地区/来源）
{ "keyword": "房屋建筑施工", "region": "河北省",
  "sources": ["hebtig"], "collect_mode": "manual_trigger" }
// 响应（异步，先返回 job_id；sources 缺省=全部已启用源；未注册源 400 invalid_request）
{ "request_id": "r-…", "job_id": "jb-…", "created": true }
```

`region` 为**源级覆盖过滤**（源注册表 `registry.py`）：源是省域平台（如惠招标覆盖河北省，含省内 11 地级市/雄安新区等下辖行政区）时，`region` 为空、`河北省` 或省内市级行政区均视为覆盖并抓取列表页。平台覆盖范围之外（如 `北京市`）不再整源跳过：仍抓取已启用源的列表页并做确定性关键词过滤，摘要标记 `region_recall=true`/“放宽召回”，候选地区保持待核实；这只是扩大召回范围，不代表公告属于所请求地区。列表页不逐条标注地区：候选卡片 `region` 一律 `null`=待补、不推断，精确地区待详情回源/人工确认（禁止编造红线）。策略中的 `infer_region` 仅为历史兼容配置，最多输出 `source_scope_only` 来源元数据，不能把平台覆盖范围写入公告实际地区。

**`GET /api/v1/intake/announcement/search/{job_id}`** —— 轮询任务结果，返回任务状态 + 候选公告卡片数组（v1.7 候选卡 DTO；列表页未标注字段一律 `null`=待补，不推断）与逐源执行摘要（限频拒绝如实展示；robots 为**预检留痕不阻断**，见 ADR-003）：

```json
{ "request_id": "r-…", "status": "completed",
  "items": [ { "candidate_id": "c-…", "project_id": null,
      "title": "某地房屋建筑施工总承包招标公告", "publish_date": "2026-09-03",
      "source_name": "惠招标（河北交投）", "source_url": "https://ebidding.hebtig.com/jyxx/…",
      "source_category": "工程类", "region": null,
      "source_region_scope": "河北省", "region_inferred": false,
      "region_provenance": "source_scope_only",
      "project_type": null, "scale": null, "fact_status": "list_fact_only",
      "missing_fields": ["region", "project_type", "scale"],
      "region_recall": false, "import_status": "pending" } ],
  "summary": [ { "source_id": "hebtig", "name": "惠招标（河北交投）", "status": "ok",
                 "count": 3, "pages_requested": 5, "pages_fetched": 5,
                 "note": "平台检索页命中 10 条，抓取 5 页、去重后 10 条，关键词过滤后 3 条" } ],
  "error_code": null }
```

候选卡 DTO 语义（09-优化方案 §3.1.3）：`title/publish_date/source_name/source_url/source_category/region` 为列表页可确证事实；v1.12：`publish_date` 由列表页**发布日期明文（span）或详情 URL 日期段**确定性提取（无则 `null`=待详情页确认），不再恒 `null`；`project_type`/`scale` 列表页未标注一律 `null`（**显示“待详情页确认”，禁止推断**）；`fact_status=list_fact_only` 表明仅列表事实；`missing_fields[]` 如实列出待详情回源的字段；`import_status` ∈ pending/imported/failed。v1.13 补充地区来源元数据：`region` 仍只表示公告事实，列表页未逐条确认时必须为 `null`；`source_region_scope` 表示注册表的平台覆盖范围，`region_inferred=false`，`region_provenance=source_scope_only` 仅说明来源范围，不得当作项目地区。v1.9 增 `region_recall`（bool）：检索地区超出源覆盖 → 放宽二次召回所得，地区待核实（不推断归属），前端候选卡标「地区待核实」。搜索状态不泛化为“测试期不联网”——前端按 `job.status` + `summary[].status` + `error_code` 区分：命中 / 零命中 / 合规跳过（robots/限频，原因打全命中规则与可重试时刻）/ 抓取失败 / worker 未运行（job 长期 pending 且健康检查 worker 不可用）。region 超范围不再产生“合规跳过空结果”，而是放宽召回（摘要 note 注明 `放宽召回…地区待核实`）。

**`GET /api/v1/intake/announcement/search/{job_id}/candidates`** —— 对**已保存候选**的分页筛选（只过滤本任务落库候选；不触发任何外网请求、不改变原搜索任务与候选事实）。查询参数：`project_type`（按候选 `category` 字段相等或标题确定性关键词匹配）、`scale`（候选无规模事实时仅接受 `unknown`=规模待确认组；具体范围无法证实则返回空并在 `note` 说明，不推断）、`limit`/`offset`。响应 `{items, total, note}`，item 同候选卡 DTO。

**`GET /api/v1/intake/announcement/searches`** —— 最近搜索任务列表（F020 §2.2.1 / 09-方案 §3.1.7）：`{items:[{job_id, status, keyword, region, sources, created_at, summary, candidates_count}], total}`；仅返回未软删除且至少有候选的搜索任务，供页面默认展示最近任务、重新打开候选（候选不只在临时 DOM 中）。零候选任务保留用于诊断但不进入首页历史列表。

**`DELETE /api/v1/intake/announcement/search/{job_id}`** —— 删除最近搜索任务（软删除）：要求 `announcement:write`；仅允许 `announcement.search`/`announcement.manual_entry`，写入 `deleted_at` 和 `announcement.search.delete` 审计事件，保留任务、候选及原文事实；重复删除幂等返回 `deleted=true`。已删除任务的状态/候选读取返回 `404 not_found`。

**`GET /api/v1/intake/announcement/health`** —— 搜索前健康检查（匿名可读，与 /readyz 同级诊断，不返回业务数据）：`{api:"ok", db:{available}, worker:{available|unknown, reason}, sources:[{source_id,name,enabled,region_scope,admin_subregions,next_allowed_at}]}`。worker 判定：最近 60 秒内有任务心跳 → available；从未有任务 → unknown（前端显示“任务已创建，等待处理服务”+任务 ID，不假装零结果）。v1.8：`sources[]` 增 `region_scope`（源覆盖地区，前端提交前预检）与 `next_allowed_at`（进程级限频器查询的该源下次可抓时刻，纯查询不计数——健康检查不占用采集配额）。v1.9：`sources[]` 再增 `admin_subregions`（省域平台下辖行政区数组，前端地区预检与服务端 `covers_region` 同口径）。

**`POST /api/v1/intake/announcement/candidates/manual`** —— Agent 聊天等外部线索转人工登记为候选（v1.8 / 09-优化方案 §3.1.9）。请求 `{title, url, note?}`（title ≤500、url 仅 http/https 且归一化、note ≤500）；RBAC `announcement:write`（投标专员）。语义：网页合规采集空结果 ≠ 外部渠道无线索——人工转录 Agent 找到的公告标题/链接为候选（只登记原文链接事实，不抓取、不推断地区/日期等字段），之后走既有「选择深入」合规链（robots+限频+详情原文净化固化+Project 建档）。登记幂等：同一 URL 重复提交返回既有候选（`created=false`）。容器 job `kind=announcement.manual_entry`、`status=completed`（终态，worker 不领取执行），在 `GET /announcement/searches` 列表中以 `manual=true` + `sources=["Agent 线索（人工转录）"]` 展示，可打开候选继续处理。

**`POST /api/v1/intake/announcement/candidates/{candidate_id}/import`** —— 投标专员确认候选公告：投递 `announcement.import_detail` 任务（worker 执行 robots 预检 + 限频 → 抓详情原文 → 净化文本（`.txt`，对象库禁 `.html`，F019 §4）→ `material` 固化（public/raw 不可覆盖/哈希去重）→ `Project` 建档）。同源 ≤1 次/5 分钟（红线），紧邻搜索后确认会限频重试属合规预期。已入库候选重复确认 → 400。响应 `{ request_id, job_id, candidate_id }`。

**`GET /api/v1/intake/announcement/candidates/{candidate_id}`** —— 轮询候选导入状态：`candidate`（同候选卡 DTO + `job_status` + `error_message`，导入成功后 `project_id` 非空）。前端轮询到 `import_status=imported` 后携带 `project_id` 跳转 `import.html?project_id=…`（不再使用固定演示项目）。

**`GET /api/v1/projects`** —— 结构化推送列表（项目卡片）；`state` 取 13 态之一。候选确认导入后自动出现在此列表。

### 2.2.2 选择与解析（`import.html`）

**`POST /api/v1/intake/tender-document`** —— multipart 上传完整招标文件（docx/pdf）。必填：`file`、`project_id`、`material_type=tender_document`、`source_type`、`data_owner`、`Idempotency-Key`。**未上传完整文件（缺 file 或空文件）→ 400 `invalid_request`，不创建解析任务**；`.gef/.etb` → 400 `unsupported_format`（提示“不支持且不会绕过加密保护”）；sha256 重复 → 200 返回既有版本（`created=false`），不重复建任务。响应：

```json
{ "request_id": "r-…", "job_id": "jb-…", "material_id": "MAT-ND-001",
  "version": 1, "content_hash": "9c42…77be", "created": true }
```

v1.7 校验与幂等（09-优化方案 §3.2）：① 前后端均校验后缀、魔数（PDF=`%PDF`、DOCX=ZIP `PK`）、空文件与大小上限（100 MB）；② `Idempotency-Key` 由客户端在**每次新文件/新项目/新用户提交时生成新值**（推荐：`project_id+文件名+大小+修改时间+操作者`的稳定哈希），仅“同一文件、同一项目、同一用户的重复提交”复用；③ 上传失败必须如实分类提示：未登录（401）、权限不足（403）、API 不可达/跨域（网络错误）、worker 未运行（任务排队不处理），禁止把失败写成“已选择招标文件正文”。

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

**`POST /api/v1/materials`** —— 补录材料上传（multipart）：`owner_type=enterprise`，核验前 `status=pending_verification`；响应含 `material_id/version/content_hash`。v1.7 扩展（09-优化方案 §3.4/§3.5）：可选表单字段 `material_subtype`（资质/业绩/人员/项目经理/财务信用/项目专用证明/其他）、`intake_mode`（`supplement_evidence` 单份补录证明 / `enterprise_ledger` 台账资料）、`requirement_ids`（JSON 数组，回链缺失要求）；`requirement_ids` 落 `material.evidence_refs`（前缀 `requirement:`），`material_subtype`/`intake_mode` 写入审计 basis，禁止使用无语义默认 ID（如 `MAT-ND-SUPPLEMENT` 空泛默认）。**补录入口**与**招标文件入口** accept 规则分离：补录/证据支持 `.pdf,.docx,.xlsx,.xls,.csv`；图片型证据待 OCR 方案确认后开放（当前不开放 `.jpg/.jpeg/.png`）。上传成功后材料须先进入 `pending_verification`，数据管理员核验后才可重算（既有流程不变）。

**`POST /api/v1/materials/{material_id}/verify`** —— 核验通过（`actor`、`data_owner` 必填）→ `status=active`，写审计。

**`POST /api/v1/projects/{project_id}/recalculate`** —— 补录核验后重算：以新证据版本创建新匹配 run，旧 `admission_results` 标记 `stale`；幂等（同一证据版本重复调用返回既有 run）。响应 `{job_id, run_id?}`。

**`GET /api/v1/projects/{project_id}/queues`**（v1.7 队列 DTO 扩展）—— 每项在 `{project_id, requirement_id, clause, text}` 基础上增加：`clause_ref`（条款号）、`missing_field`（缺失字段名，来自 match item 的 `missing_items`，无则 `null`）、`reason`（缺因文本：无 active 证据/未核验/待补录，不编造）、`purpose`（“补来做什么”：该要求用途说明或 `null` 待补录人确认）、`recommended_material_types`（仅当规则 `evidence_required` 可如实映射时给出；否则 `null`=由补录人按材料类型选择，禁止推断）、`owner_role`（责任人：投标专员上传/数据管理员核验，测试期口径）、`due_at`（投标截止或动作截止，无则 `null`）。前端“补录材料”按钮把整项上下文（requirement_id/clause_ref/missing_field/reason/purpose）带入补录表单预填。

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

### 5.1 正式认证（R024，替换开发期 X-Role/X-Actor）

- 身份来源：`POST /api/v1/auth/login`（`{username, password}`）签发 Bearer token；业务路由一律从 `Authorization: Bearer <token>` 解析角色与操作者。开发期 `X-Role/X-Actor` 头仅当 `AUTH_DEV_HEADERS=true` 时作为回退（仅限开发/测试环境），正式环境必须移除该配置（fail-closed：未配置即不信任任何头）。
- Token：HMAC-SHA256 签名（`AUTH_TOKEN_SECRET`），载荷含 `sub`（登录名）、`role`、`name`（显示名）、`iat`、`exp`（默认 8h）。验签失败/过期一律按匿名拒绝并审计，不区分错误细节（避免探测）。
- 账号：`AUTH_USERS` 环境变量配置（`login:pbkdf2$迭代$salt$hash:role:显示名`，`;` 分隔；显示名不含 `:`/`;`），密码仅存 pbkdf2-sha256 哈希，不入 Git（`runtime/.env`）。
- 最小权限与越权审计：RBAC 矩阵（§5 表）保持为唯一授权依据；`require_role` 的审计 actor 一律取 token 登录名（开发回退取 X-Actor），拒绝访问写入 `audit_events`（`auth.*`/`forbidden`）。`GET /api/v1/auth/me` 供前端初始化当前身份；登录成功/失败均留审计。
- 认证端点本身不校验角色（任何人可尝试登录）；其余业务端点未认证按 `anonymous` 拒绝（403 + 审计）。

## 6. 错误码

至少统一：`invalid_request`、`unauthorized`（R024 登录/未认证）、`forbidden`、`not_found`、`duplicate_material`、`hash_mismatch`、`unsupported_format`、`parse_failed`、`manual_review_required`、`invalid_state_transition`、`dependency_unavailable`、`internal_error`。v1.8 增 `rate_limited`（HTTP 429，R004/schema §5.2 单源/全局限频拒绝；错误体 `detail.retry_after_seconds` 携带红线窗口剩余秒数，前端倒计时展示——不提供任何绕过红线通道）。

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
| 2026-09-02 | v1.4 | §5.1 正式认证（R024）：Bearer token 登录替换开发期 X-Role/X-Actor 头（AUTH_DEV_HEADERS 显式开关回退）；账号 pbkdf2 哈希存 AUTH_USERS、HMAC token、匿名拒绝留审计；`/auth/login`、`/auth/me` 契约。 |
| 2026-09-03 | v1.5 | §2.2.1 搜索契约改为真实采集（R004 接线）：worker 执行 `announcement.search`（robots 预检+单源限频+透明 UA，抓已启用 L1 源列表页）→ 候选落库；POST 不再预建 Project（响应 `{request_id, job_id}`）；GET 返回候选事实卡数组 + 逐源 summary（限频/robots 拒绝如实展示）；新增候选确认入库 `candidates/{id}/import`（详情原文净化文本 `.txt` 固化 + Project 建档，raw 不可覆盖）与状态轮询端点。源注册表见 `runtime/collecting/registry.py`，启用登记见 `docs/合规数据源清单.md` §4。 |
| 2026-09-03 | v1.6 | §2.2.1 明确 `region` 覆盖语义（验收暴露修复）：省域平台（覆盖河北省）对省内市级行政区（11 地级市/雄安新区等）检索视为覆盖并抓取，平台范围外（北京市）才 `skipped`；候选 `region` 列表页未标注一律 `null`=待补。实现：`registry.py` 新增 `admin_subregions`。 |
| 2026-09-04 | v1.7 | 按 `docs/09-网页功能测试技术优化方案-20260904.md` 冻结：① 搜索请求只接收关键词/地区/来源（`category`/`scale` 不再作为搜索前采集过滤，服务端忽略）；新增候选卡 DTO（title/publish_date/source_name/source_url/source_category/fact_status/missing_fields[] 等，`project_type`/`scale` 列表页未标注=null 待详情确认，禁止推断）；② 新增 `search/{job_id}/candidates`（已保存候选分页筛选，不触发外网）、`announcement/searches`（历史任务列表+候选重开）、`announcement/health`（API/DB/worker/源健康检查，worker 心跳判定）三接口；③ 上传契约补魔数/大小上限校验与 Idempotency-Key 生成语义（同文件同项目同用户才复用）；④ §2.2.5 补录队列 DTO 扩展（missing_field/reason/purpose/recommended_material_types/owner_role/due_at/clause_ref），`POST /materials` 增 `material_subtype`/`intake_mode`/`requirement_ids[]`（回链 requirement，落 evidence_refs，禁止无语义默认 ID），补录入口 accept 分离（pdf/docx/xlsx/xls/csv）。 |
| 2026-09-04 | v1.8 | ① `announcement/health` 落实 v1.7「匿名可读」契约（代码曾误加 RBAC 门禁，data_admin 等角色登录后健康检查 403 伪失败——移除，与 /readyz 同级）；`sources[]` 增 `region_scope`/`next_allowed_at`（提交前地区覆盖预检 + 源级限频窗口展示，健康检查不占采集配额）。② 新增 `POST /announcement/candidates/manual`（Agent 线索人工转录候选，登记幂等、容器 job 终态不执行、不推断字段，详情仍走合规链），`GET /announcement/searches` 增 `manual` 标识。③ 限频语义：`ComplianceError` 携带 `retry_after_seconds`，worker 对限频失败终态 `failed`（不快速 3 连败），候选 `error_message` 附可重试时刻，错误码表增 `rate_limited`（429）。 |
| 2026-09-04 | v1.9 | ① 搜索 region 语义升级：超出源覆盖（如检索“北京市”）不再整源 `skipped`——执行**放宽二次召回**（仍抓列表 + 关键词过滤，候选落库），候选卡 DTO 增 `region_recall`（地区待核实，列表页不标注不推断，详情回源确认）；`announcement/health` `sources[]` 增 `admin_subregions`（省域平台下辖行政区，前端地区预检与服务端 `covers_region` 同口径，消除“石家庄市被误报超范围”）；robots 拒绝原因打全命中规则（`Disallow:` 前缀，RFC 9309 Allow 优先）。 |
| 2026-09-04 | v1.10 | ① 关键词过滤改确定性多词 AND + 全半角/空白归一（`registry.keyword_matches`，空关键词恒命中，词序不敏感可审计）；② 惠招标源扩为同平台四分区（工程/服务/货物/非招标，同域同结构实测，限频按域共享）；③ 带 `search_param` 的源有关键词时抓平台站内检索结果页（`?keyword=`，robots 按默认列表路径预检），无关键词回落默认列表；④ 解析器通用化（`parse_announce_list`，历史 `parse_hebtig_list` 保留别名）。数据源清单 §2.2/§4.1 同步：ccgp-hebei/cebpubservice robots 全站禁止（依法不接入）、ggzy 不可达、szj.hbjyzx 列表 JS 动态（待评估）。 |
| 2026-09-07 | v1.11 | robots 由阻断红线改为**预检留痕不阻断**（ADR-003）：搜索/详情导入的 robots 检查不再跳过源、不再抛 ComplianceError，改为摘要/审计记录 `robots_status`（allowed/disallowed/missing/unreachable）+ 命中规则；ccgp-hebei/cebpubservice 由「禁用」改「已冻结（预检留痕）」，移入待实测队列。 |
| 2026-09-07 | v1.12 | 惠招标修复闭环：① 列表解析器提取**发布日期**（行内「发布日期：」明文 span 优先、详情 URL 日期段 `/YYYYMMDD/` 回退；均无可证 → `null` 待详情回源，不推断）——候选卡 `publish_date` 不再恒 `null` 待补；② 候选卡 DTO 示例与 `missing_fields[]` 口径同步（publish_date 可提取即展示，region/project_type/scale 仍待详情回源）；③ 合规清单 §4.1 惠招标四分区标注由「接线未达标」校正为「已接线（2026-09-07 修复闭环）」，P0 验收（≥10 条真实公告）待用户本机实测。 |
| 2026-09-08 | v1.13 | ① 引入 `runtime/config/collection_policy.yml` 的 production/staging/development/mock 策略口径，明确 production 单源 300 秒/全平台 200 次红线不可放宽、仍仅 `manual_trigger`；② 搜索摘要增加 `pages_requested/pages_fetched`，分页受策略与源双重上限控制，同一源同一搜索批次只计一次限频事件；③ `SourceSpec` 增 `page_param/max_pages/date_param` 与分页 URL 构造规则；④ 候选卡增加 `source_region_scope/region_inferred/region_provenance`，明确平台覆盖范围不填充公告 `region`。 |
| 2026-09-10 | v1.15 | ① **B2 推送两周窗口收紧**：`search/{job_id}/candidates` 默认严格「两周内且有日期」——`>14 天`照旧剔除；发布日缺失候选不再默认展示，折叠为 `date_window{days,pending}` 计数（前端「N 条日期待确认，可展开」），逐卡增 `in_date_window`；② **C1 地区三态**：`SourceSpec.parent_region` + `region_coverage()`（direct/broader/none）——省级检索覆盖地市源不标待核实；地市检索覆盖同市源 + 省/全国源（候选卡增 `region_note` 区分文案）；`covers_region` 保留布尔兼容；③ **C4/D1 候选卡**：region 优先级=详情回填/detail_summary（announcement_fact）>标题命中（title_fact）>平台范围（source_scope_only），`missing_fields` 相应收缩；project_type 由标题确定性抽取（施工/EPC总承包/监理/设计/勘察/货物/服务，`project_type_provenance=标题`）；④ **E1** 新增 `GET /announcement/candidates/by-project/{project_id}`（按项目反查最近候选，只读）；⑤ **C3** `announcement/health` 增 `region_options` + `sources[].parent_region`（前端地区下拉由注册表生成，无源城市不出现）。 |
| 2026-09-11 | v1.16 | §5 权限矩阵变更（用户决策）：**business_head（经营负责人）增加 `announcement:write`**（原只读）——经营负责人可执行搜索提交、候选确认入库、搜索任务删除等公告写操作；与投标专员同过 RBAC 门。契约测试同步（data_admin/legal 仍 403）。演示账号体系同日调整：`toubiao/123456`（投标专员）、`jingying/123456`（经营负责人）、`data_admin/data_admin`、`legal/legal`；`admin` 账号移除（此前被误配为 business_head 造成角色混淆）。 |
| 2026-09-11 | v1.17 | **新增 `GET /intake/announcement/candidates/{candidate_id}/text`**（docs/10 §5 P1-5 溯源定位）：返回候选公告最新版本的固化净化正文全文 + `content_hash`（直读 material_versions 反查对象库，只读）——前端"在原文中定位"按 detail_summary 的 quote/start/end 高亮，hash 不一致走重定位兜底。权限=announcement:read（与候选读同门）。detail_summary 字段 schema 扩展（quote/start/end/enum/truncated/review/content_hash）见 F004 v1.6。 |
