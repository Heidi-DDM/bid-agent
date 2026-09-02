# F025-RAG 三层知识库与证据检索

- **需求来源**：R025 ｜ **状态**：定稿草案 v1.0（2026-09-01）
- **关联**：F003、F004、F005、F006、F007、F008、F017、F018-F024、ADR-001、ADR-002

## 1. 目标与边界

本规格定义 RAG 统一信息资料库的三层数据、索引、检索、权限和验收契约。RAG 是主要的信息资料入口和证据发现系统，但不是唯一事实存储，也不是资格、评分或准入判定器。

```text
L1 公开可投标信息：公告、项目主卡、事实子卡、来源和时间线
L2 招标文件知识库：招标文件、澄清、补遗、条款、评分办法、动作要求
L3 企业能力与证据库：资质、人员、业绩、证书、财务及其证据
```

非目标：不使用向量相似度直接判定满足/不满足；不以模型生成内容覆盖原文；不把企业私有资料发送给外部模型；不建设自动投标、自动报价或自动签章路径。

## 2. 设计原则

1. **双轨存储**：原文/解析片段不可变保存；结构化字段进入 PostgreSQL；向量只作为检索索引，不能替代结构化字段。
2. **候选证据而非结论**：RAG 输出候选条款、候选证据及其引用，交由结构化核验和 F008 确定性规则引擎判定。
3. **混合检索**：默认使用 BM25/关键词 + 向量检索 + metadata filter + 可选 reranker；不得只依赖向量近似度。
4. **权限先于召回**：查询前按 `permission_scope`、`owner_type`、`project_id`、`lot_id`、`status`、`as_of` 过滤；无法确认权限时 fail-closed。
5. **可回放**：每次索引和检索记录模型/索引版本、输入快照、过滤条件、结果 ID 和耗时；文档或证据版本变化使相关结果 `stale`。
6. **原文可回跳**：每个 chunk 必须回链 `material_id`、`version`、`content_hash`、页码/段落或坐标；无引用的生成文本不得作为事实入库。

## 3. 索引对象与三层约束

### 3.1 统一 KnowledgeChunk

| 字段 | key | 必填 | 说明 |
|---|---|---:|---|
| 分片 ID | `chunk_id` | 是 | 不可变主键 |
| 层级 | `knowledge_layer` | 是 | `L1_public` / `L2_tender` / `L3_enterprise` |
| 材料引用 | `material_id` / `material_version` | 是 | 回链 F003 Material |
| 原文哈希 | `content_hash` | 是 | 与材料版本一致 |
| 项目/标段 | `project_id` / `lot_id` | 否 | L2/L1 必须按适用范围过滤 |
| 文本 | `text` | 是 | 原文或解析片段，不写模型臆造内容 |
| 结构化字段 | `field_refs` | 否 | 已核验字段引用，格式由 F005/F006 定义 |
| 页码定位 | `location` | 是 | 页码、段落或 OCR 坐标 |
| 权限 | `owner_type` / `permission_scope` / `classification` | 是 | 继承材料权限，不得提升 |
| 核验状态 | `verification_status` | 是 | `pending_verification` / `active` / `expired` / `invalid` |
| 索引版本 | `embedding_model` / `index_version` | 是 | 可重建、可回放 |

L1 只允许 `public` 数据；L2 允许公开招标文件及其澄清版本，不得混入企业证据；L3 只允许企业私有资料和证据元数据，匹配时仅读取截至 `as_of` 的 `active` 版本。

### 3.2 分片与父子关系

- 保留文档、章节、条款三层父子关系；条款分片不得脱离所属材料和章节。
- 表格按行/列保留表头上下文；金额、日期、等级、单位不得在切块时丢失。
- 默认分片目标 400-800 中文字，重叠 50-100 字；实际值作为配置记录，不作为业务契约。
- OCR 分片必须保留页码和字段置信度；低置信度只可进入复核队列。

## 4. 检索契约

### 4.1 查询输入

```json
{
  "query": "建筑工程施工总承包二级及以上",
  "knowledge_layers": ["L2_tender", "L3_enterprise"],
  "project_id": "ND-2025",
  "lot_id": null,
  "as_of": "2025-10-30T09:00:00+08:00",
  "permission_scope": "restricted",
  "top_k": 20,
  "retrieval_mode": "hybrid"
}
```

### 4.2 检索输出

每条结果至少返回 `chunk_id`、`material_id`、`material_version`、`content_hash`、`text`、`location`、`knowledge_layer`、`permission_scope`、`retrieval_score`、`verification_status` 和 `citation`。检索结果必须标注 `candidate_only=true`，不得返回 `satisfied`、`not_satisfied` 或 `qualified` 等判定字段。

检索层应支持：

- L2 条款 → L3 企业证据的受控双层检索；
- 同义词/中文等级词归一化后的关键词检索；
- `project_id`/`lot_id`/版本/有效期/权限过滤；
- 召回结果不足时显式返回 `insufficient_evidence=true`，不能补写。

## 5. 模型与数据出域边界

| 能力 | 模型 | 数据范围 | 允许输出 |
|---|---|---|---|
| 公告/公开招标文件初步抽取 | DeepSeek API（经批准） | `permission_scope=public_read` 的文本 | 候选字段、条款分类、规则草案 |
| 企业资料 embedding | 本地/内网 embedding（如 BGE-M3） | L3 企业资料 | 向量和索引元数据，不出内网 |
| 企业资料 OCR | 本地 Tesseract/PaddleOCR | L3 扫描件 | 页码、候选字段、置信度 |
| 严格比对 | Python/SQL/确定性规则引擎 | 已核验结构化字段 | 四类结论和状态 |

DeepSeek 或任何生成模型不得：读取未授权 L3 原文、生成企业不存在的人员/证书/业绩、直接输出匹配结论、计算最终金额/日期/等级结论、生成报价或投标决定。模型调用需记录模型名、版本、提示模板版本、输入/输出哈希和耗时；日志不得保存完整私有原文。

## 6. 与确定性匹配引擎的边界

```text
RAG 检索候选条款/证据
  → 结构化字段解析与单位/日期/等级归一化
  → 证据有效性、版本、哈希、as_of 校验
  → F008/F023 确定性规则执行
  → match_result + clause_ref + evidence_ref + audit
```

金额使用 Decimal，日期统一 ISO 8601，单位和中文等级使用显式映射表。向量相似度、LLM 置信度和 reranker 分数只能用于排序或人工复核优先级，不能作为满足条件的依据。召回不到证据映射为 `unverifiable`/`blocked_missing_data`，明确证据不满足才映射为 `not_satisfied` 或硬性阻断。

## 7. 持久化与失效

F019 的 PostgreSQL 增加 `knowledge_chunks`、`knowledge_embeddings`、`retrieval_runs` 三类表（向量列使用 pgvector；没有 pgvector 时任务不得假装成功）。向量索引记录 `material_id + version + content_hash + embedding_model + index_version` 唯一键。材料新版本、澄清/补遗、企业证据核验变更或模型索引版本变化时，旧 chunk/检索运行保留但标记 `stale`，不得驱动新的审批。

## 8. 验收与测试

### 8.1 必过安全与一致性门禁

- 权限泄漏率 = 0：投标专员检索不得返回 L3 明细，跨项目/跨标段证据不得召回。
- 引用完整率 = 100%：每条返回结果可定位材料、版本、哈希和页码/段落。
- 原文不可变：原文或已索引版本被修改时哈希校验失败并阻断使用。
- 幂等：同一 `material_id + version + embedding_model + index_version` 不重复建索引；重复事件不重复产生 retrieval run。
- 关闭 DeepSeek 或 embedding 服务时，原文仍可入库；解析/索引任务进入可解释 `manual_review`/`retryable`，不得伪造成功。

### 8.2 质量黄金样本

- L2 条款召回 `Recall@5 >= 0.90`；L3 企业证据召回 `Recall@10 >= 0.90`，样本和标注集入 Git 只保存脱敏文本与 ID。
- 结构化数字/日期/等级字段 exact match `>= 0.95`；低于阈值进入人工复核，不自动放行。
- RAG 结果到规则结果的引用闭环率 `100%`；规则结果可用相同快照重放。
- 农大 20 条要求全部有条款候选；缺失 NQ-H-007 时仍输出 `blocked_missing_data`，不得因语义相似证据放行。

### 8.3 运行时验收路径

L1 公告入库 → L2 招标文件解析并索引 → L3 企业资料核验并索引 → 受权限过滤的混合检索 → 结构化核验 → F023 匹配 → F024 结果/审批。验收报告必须记录 Recall、exact match、权限负例、引用回跳、重启/幂等和失败降级结果。

## 9. 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-09-01 | v1.0 | 新增 RAG 三层知识库、混合检索、权限过滤、模型出域边界、结构化核验和验收门禁；明确 RAG 只产生候选证据，确定性规则引擎负责判定。 |
