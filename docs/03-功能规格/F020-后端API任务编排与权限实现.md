# F020-后端 API、任务编排与权限实现

- **需求来源**：R020 ｜ **状态**：定稿草案 v1.1（2026-08-31）
- **关联**：F003-F010、F017-F019、ADR-001

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
| 匹配 | `POST /projects/{id}/match`、`GET /projects/{id}/match-runs/{run_id}`、`GET /projects/{id}/match-runs/latest`（`POST match` 仅由任务编排器或重算流程调用） |
| 结果/队列 | `GET /api/v1/projects/{id}/matrix`、`GET /api/v1/projects/{id}/admission`、`GET /api/v1/projects/{id}/queues` |
| 审批 | `GET /api/v1/approvals/pending`、`POST /api/v1/projects/{id}/approval/approve`、`/reject`、`/waivers`、`GET /api/v1/projects/{id}/audit` |

所有响应包含 `request_id`；异步接口返回 `job_id`，通过 `GET` 查询结果，不长连接等待大文件处理。

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
