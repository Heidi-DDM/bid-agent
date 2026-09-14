# 技术更改方案：RAG 三层知识库运行时实施

- **版本**：v1.1
- **日期**：2026-09-02
- **实施范围**：R025 / F025，以及 F018-F024、F011 的运行时联动
- **试点**：河北农业大学东校区研究生宿舍建设项目施工（农大）
- **前置决策**：[ADR-002-RAG三层知识库与模型边界.md](06-产品决策记录/ADR-002-RAG三层知识库与模型边界.md)

## 1. 方案结论

本次改动把 RAG 建成主要的信息资料入口和证据发现系统，但不把 RAG 当作判定器：

```text
原文/文件 → 解析与分片 → 三层索引 → 权限过滤的混合检索
                                  ↓ 候选条款/候选证据
                         结构化字段与证据核验
                                  ↓
                         F008/F023 确定性规则引擎
                                  ↓
                         准入结果 → 人工审批
```

本次不改变旧 PRD 历史基线、ADR-001 状态机、满分定义、审批人职责和“不得自动投标/报价/签章”红线。

## 2. 需求与规格基线

| 来源文件 | 本次使用的契约 | 实施影响 |
|---|---|---|
| `docs/01-需求池.md` R025 | 三层知识库、混合检索、Recall/exact-match、权限零泄漏 | 需求和退出条件 |
| `docs/06-产品决策记录/ADR-002-RAG三层知识库与模型边界.md` | RAG 候选证据边界、DeepSeek public-only、本地 embedding | 架构与安全门禁 |
| `docs/03-功能规格/F025-RAG三层知识库与证据检索.md` | KnowledgeChunk、过滤字段、索引版本、检索回放 | 新增核心实现契约 |
| `F018-运行时技术基线与本地部署.md` | PostgreSQL + pgvector、M5 16GB 配置、`/readyz` | 基础设施和配置 |
| `F019-数据库与原文证据持久化.md` | `knowledge_data`、不可变 Material、备份恢复 | 数据库迁移和持久化 |
| `F020-后端API任务编排与权限实现.md` | `knowledge/search`、索引幂等、GET 不触发匹配 | API 和任务编排 |
| `F021-招标材料入库解析与模型分析运行时.md` | L2 条款解析/索引、DeepSeek 公开数据 | 招标文件流水线 |
| `F022-企业资料导入OCR与核验运行时.md` | L3 active/as_of 证据、本地 embedding | 企业资料流水线 |
| `F023-匹配评分与准入运行时服务.md` | RAG 候选 → 结构化核验 → 规则判定 | 匹配输入和快照 |
| `F024-结果产出审批与审计运行时.md` | `retrieval_run_id`、索引版本、候选引用可回放 | 结果和审计 |
| `F011-端到端薄切片与真实项目验收.md` | 农大端到端和质量指标 | 最终验收 |

## 3. 文件级技术变更清单

### 3.1 配置与模型适配

**基线文件**：`runtime/core/config.py`、`runtime/core/model.py`、`runtime/.env.example`、`docs/03-功能规格/F018-运行时技术基线与本地部署.md`

**更改内容**：

- 增加 `PGVECTOR_ENABLED`、`EMBEDDING_MODEL`、`EMBEDDING_DIM`、`RERANKER_ENABLED`、`RAG_TOP_K`、`RAG_CHUNK_SIZE`、`RAG_CHUNK_OVERLAP`。
- 增加 `DEEPSEEK_ENABLED`、`DEEPSEEK_PUBLIC_ONLY=true`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`、`DEEPSEEK_API_KEY`。
- 将模型用途分为 `public_extraction`、`local_embedding`、`local_reranker`；企业数据进入外部模型前必须拒绝。
- `/readyz` 检查 pgvector、embedding 服务和 public-only 开关；未就绪时不创建成功索引/检索任务。

**预期效果**：配置可以明确控制数据出域、向量索引和本地模型容量；MacBook Air M5 16GB 以单 worker、批量 4-8 的测试配置运行，不承诺生产并发。

**验收标准**：

- `.env.example` 包含全部参数且无真实密钥；
- `DEEPSEEK_PUBLIC_ONLY=false` 或 DeepSeek 地址不合规时 `/readyz` 失败；
- 企业资料调用外部模型的单元测试返回拒绝；
- pgvector/embedding 不可用时任务为 `retryable`/`manual_review`，不能为 `succeeded`。

### 3.2 数据库与迁移

**基线文件**：`runtime/db/models.py`、`runtime/db/alembic/versions/`、`F019-数据库与原文证据持久化.md`

**更改内容**：新增 Alembic 迁移（编号接续当前最新迁移），建立 `knowledge_data` schema 和：

| 表 | 关键要求 |
|---|---|
| `knowledge_chunks` | `chunk_id`、L1/L2/L3、Material/version/hash、project/lot、文本、页码/段落、权限、核验状态、index_version |
| `knowledge_embeddings` | chunk、模型、维度、向量、index_version；唯一键防重复索引 |
| `retrieval_runs` | query_hash、过滤条件、as_of、top_k、候选 chunk IDs、索引版本、耗时 |

**预期效果**：原文、结构化事实和向量索引分离；索引可重建，检索可回放，旧版本可追溯且不能驱动新审批。

**验收标准**：

- `alembic upgrade head` 可重复执行；
- 同一 `material_id + version + embedding_model + index_version` 不产生重复 chunk/embedding；
- 修改原文或哈希不一致时索引/匹配阻断；
- 数据库备份恢复后候选引用、规则、匹配和审计外键完整；
- 企业角色无法读取不应可见的 L3 明细。

### 3.3 文档分片与索引流水线

**基线文件**：`F004`、`F005`、`F017`、`F021`、`F022`、`runtime/db/worker_service.py`、`runtime/worker.py`

**建议新增文件**：

```text
runtime/rag/schemas.py       # KnowledgeChunk、检索请求/响应 Schema
runtime/rag/chunker.py       # 文档/章节/条款/表格分片
runtime/rag/indexer.py       # embedding、批量 upsert、幂等和失效
runtime/rag/filters.py       # permission/project/lot/as_of/status 过滤
runtime/rag/retriever.py     # BM25 + pgvector + reranker
runtime/rag/service.py       # 索引/检索服务编排
```

**更改内容**：

- L1 公告、L2 招标文件、L3 企业资料分别标记 `knowledge_layer`，禁止跨层无条件混检。
- PDF/DOCX 保留页码、段落和表头；OCR 保留页码、坐标和字段置信度。
- 默认分片 400-800 中文字、重叠 50-100 字，实际参数以配置记录；金额、日期、等级和单位必须与上下文同片。
- 索引前执行权限和核验状态检查；L3 匹配索引只允许 `active` 且不晚于 `as_of` 的证据。
- 材料新版本、澄清/补遗、证据核验变更或索引版本变化时，旧索引标记 `stale`。

**预期效果**：解析完成后能得到可引用、可过滤、可重建的三层知识库；索引失败不会伪造解析成功。

**验收标准**：

- 农大公告、招标文件和最小企业资料分别生成 L1/L2/L3 chunk；
- 每个 chunk 可回跳 `material_id/version/content_hash/page_no`；
- 表格金额/日期/等级抽样无上下文丢失；
- 重复事件只创建一个索引任务；
- 索引失败进入可解释失败或复核队列，原文仍可查看。

### 3.4 检索 API 与权限

**基线文件**：`runtime/routers/`、`runtime/core/rbac.py`、`runtime/core/orchestration.py`、`F020`、`F025`

**建议新增/修改**：新增 `runtime/routers/knowledge.py`，注册以下接口：

```text
POST /api/v1/knowledge/search
GET  /api/v1/knowledge/indexes/{material_id}/{version}
GET  /api/v1/retrieval-runs/{retrieval_run_id}
```

**更改内容**：

- 查询必须显式提供 `knowledge_layers`、`permission_scope`；L2/L3 还必须提供 `project_id`、`as_of`，有标段时必须提供 `lot_id`。
- 返回 `candidate_only=true`，包含 chunk、Material、版本、哈希、页码、权限、核验状态和引用；禁止返回准入结论。
- GET/POST 检索接口不得创建 match run 或改变项目状态。
- 所有检索写入 `retrieval_runs`，记录过滤条件和候选 ID；越权请求返回 403 并写审计。

**预期效果**：RAG 可以被匹配服务可靠调用，也可以被人工复核查看，但不能绕过 F023 或 F024。

**验收标准**：

- 投标专员查询 L3 明细返回 403 或脱敏结果；
- 跨项目、跨标段、过期和 pending_verification 证据不会召回；
- 响应永远包含 `candidate_only=true` 和完整引用；
- 页面刷新、重复 GET 不新增检索/匹配任务；
- OpenAPI、RBAC、审计集成测试通过。

### 3.5 匹配引擎接入

**基线文件**：`runtime/db/worker_service.py`、`runtime/routers/match.py`、`F023`、`F008`、`scripts/matching/engine.py`

**更改内容**：

1. 解析完成后由编排器创建一次 retrieval/index 任务，再创建首次 match 任务。
2. 匹配服务读取 L2 条款候选和 L3 企业证据候选，同时读取截至 `as_of` 的结构化 active 快照。
3. 先做单位、金额、日期、等级、有效期、版本和哈希核验。
4. 只有结构化核验通过的证据才能交给 F008/F023 规则引擎。
5. 保存 `retrieval_run_id`、候选 chunk IDs、结构化核验结果和规则快照。

**预期效果**：RAG 真正参与资料发现和证据组织，严格比对仍由确定性引擎完成；数字错误、语义误召回和缺失证据不会被模型放行。

**验收标准**：

- 向量分数和 LLM 置信度不进入评分/准入公式；
- 召回不到证据 → `unverifiable`/`blocked_missing_data`；明确证据不满足才可 → `not_satisfied`/硬性阻断；
- 农大 20 条规则 `declared=20/executed=20/complete=true`；
- NQ-H-007 缺失时仍为 `blocked_missing_data`，不能由语义相似证据满足；
- 同一规则/证据/索引快照可重放，旧结果 `stale` 不得审批。

### 3.6 结果、审批与审计

**基线文件**：`runtime/routers/projects.py`、`runtime/routers/approvals.py`、`F024`、`F009`

**更改内容**：

- 匹配结果 JSON/HTML 增加 `retrieval_run_id`、索引版本、候选引用和过滤摘要。
- 审批创建前验证结果未过期、引用完整、权限校验通过且 `internal_admission_eligible=true`。
- 检索、索引、结构化修正、重算和审批均追加审计事件。

**预期效果**：人工审批看到的不只是结论，还能回放“哪条招标条款、哪条企业证据、哪个版本、哪个检索运行”产生了结果。

**验收标准**：

- `result_id` 对应的 JSON/HTML 与数据库快照一致；
- 缺少候选引用、哈希不一致或索引 `stale` 时不能创建审批；
- 农大缺失财务材料时无审批入口；
- 审批通过仍只产生 `approved_for_bidding`，不产生投标平台、报价或签章动作。

## 4. 执行顺序与交付物

| 阶段 | 依赖 | 交付物 | 退出条件 |
|---|---|---|---|
| A 契约/配置 | ADR-002、F018/F025 | 配置、Schema、public-only 检查 | 配置测试和 `/readyz` 门禁通过 |
| B 数据库 | A | Alembic 迁移、三张知识库表、备份校验 | 迁移可重复、权限/哈希/幂等通过 |
| C 索引流水线 | B、F021/F022 | chunker、indexer、worker 任务 | 农大 L1/L2/L3 索引可回链 |
| D 检索 API | C、F020 | knowledge router、retrieval_runs、RBAC | 权限负例 0 泄漏、候选契约通过 |
| E 匹配接入 | D、F023 | RAG→结构化核验→规则引擎 | 20 条规则全量可重放 |
| F 结果验收 | E、F024/F011 | 报告、审计、黄金检索集 | Recall/exact-match/端到端门禁通过 |

禁止跳过阶段：没有结构化事实、权限过滤和版本快照时，不得接入准入判定。

## 5. 最终验收标准

### 功能

- 农大从手动公告/文件入库到检索、匹配、补录重算、审批审计完整跑通。
- 三层知识库可独立查询，L2/L3 跨层检索必须带项目、标段、权限和 `as_of` 上下文。
- 原文不覆盖，Material/索引/检索/匹配均可按版本重放。

### 质量

- L2 条款 Recall@5 ≥ 0.90；
- L3 企业证据 Recall@10 ≥ 0.90；
- 金额、日期、等级字段 exact match ≥ 0.95；
- 引用完整率 100%；权限泄漏率 0；
- 召回失败、模型关闭、pgvector 不可用均进入可解释降级，不产生假成功。

### 合规与安全

- DeepSeek 只接收公开数据；企业原文、OCR、embedding 不出内网；
- 企业个人信息按角色脱敏；越权访问 403 并审计；
- Git 不提交企业资料、招标文件、向量数据库、密钥、日志和构建产物。

### 回归命令

```bash
python -m pytest runtime/tests -v
python -m pytest runtime/tests -m rag -v
python -m pytest runtime/tests -m integration -v
python scripts/matching/run_golden_lab.py
git diff --check
```

R025 运行时代码已接通本地 query embedding、RRF 候选池与本地 reranker。2026-09-09 已验证 PostgreSQL/API/worker 与本地 BGE-M3 embedding 服务：`/readyz`=ready，安全 E2E 覆盖真实向量索引、混合/向量召回、项目隔离和 run 幂等。`BAAI/bge-reranker-v2-m3` 仍需显式下载；模型资产缺失时应保持 `RERANKER_ENABLED=false`，不得把基础排序误报为重排成功；启用该开关后 `/readyz` 必须检查 reranker。

## 6. 变更管理

- 任何新增字段、状态、API 或判定语义必须先更新对应 F 规格和 ADR，再改代码。
- 本方案实施中发现的字段差异、黄金样本误判或召回失败，登记 `docs/04-修改日志.md`，并回写 F025/F008/F019。
- 旧 PRD 保持历史基线，不因引入 RAG 直接改写。

## 7. 开发者执行方案

### 7.1 工具链与环境

| 类别 | 工具/版本基线 | 用途 |
|---|---|---|
| 运行时 | Python 3.11、FastAPI、Uvicorn | API 与 worker |
| 数据库 | PostgreSQL 16 + pgvector、SQLAlchemy、Alembic | 事实、知识分片、向量和检索快照 |
| 文档 | `pdftotext`、PyMuPDF/Docling、`python-docx` | PDF/DOCX 文本、页码和表格解析 |
| OCR | Tesseract 5.5+ `chi_sim`（必要时 PaddleOCR） | 扫描件和图片型 PDF |
| Embedding | 本地/内网 BGE-M3 量化模型 | L2/L3 向量化，不出内网 |
| 检索 | PostgreSQL FTS/BM25 + pgvector cosine + RRF + 本地 cross-encoder | 混合检索；`BAAI/bge-reranker-v2-m3` 重排候选池后再截取 top-k |
| 外部模型 | DeepSeek API | 仅 public 抽取，结构化 JSON |
| 测试 | pytest、pytest-cov、现有 golden lab、PostgreSQL 集成测试 | 单元、契约、回归和验收 |
| 质量 | `ruff`/`black --check`（若纳入 CI）、`py_compile`、GitHub Actions | 静态检查和 CI 门禁 |

开发环境使用当前 `runtime/.env.example`；不得把真实企业资料、API key、数据库 dump 或向量文件写入 Git。所有依赖先加入锁定依赖文件，再执行 `pip install -r requirements-dev.txt`。

### 7.2 实施顺序（最短安全路径）

1. **先写 Schema 和失败测试**：先建立 RAG 请求/响应、KnowledgeChunk、过滤和幂等测试，再实现业务代码。
2. **先完成 L2 再扩展 L3**：用农大招标文件验证页码/条款召回；L3 只接入最小已核验资料，避免同时处理两类数据质量问题。
3. **先用混合检索，不先上复杂 Agent/GraphRAG**：BM25 解决编号/金额/证书号，向量解决同义表达，RRF 组合结果；规则判断不依赖召回分数。
4. **先做同步纯逻辑，再接 worker**：`chunker/filter/retriever` 先实现可测试纯函数；索引和重算再接现有 `analysis_jobs`。
5. **每个阶段都保留降级**：模型、embedding、pgvector 关闭时仍保存原文；任务进入 `retryable`/`manual_review`，不返回假成功。

### 7.3 分支与提交

建议按以下提交边界开发，便于回滚和审查：

```text
docs: 冻结 RAG 运行时契约
feat: 增加 knowledge_data 迁移和模型
feat: 增加分片与本地 embedding 索引
feat: 增加混合检索与权限过滤 API
feat: 接入匹配快照与检索审计
test: 增加黄金检索集和权限负例
```

每个提交必须通过 `git diff --check`、单元测试和敏感文件检查；不把运行数据库或真实样本提交到远端。

## 8. 代码级实现清单

### 8.1 新增模块与公开函数

```text
runtime/rag/__init__.py
runtime/rag/schemas.py       # Pydantic: SearchRequest/SearchResponse/KnowledgeChunkDTO
runtime/rag/chunker.py       # chunk_document(material, parsed_pages) -> list[ChunkDraft]
runtime/rag/normalizer.py    # normalize_amount/date/level/unit
runtime/rag/filters.py       # build_visibility_predicate(actor, request)
runtime/rag/indexer.py       # index_material(session, material_ref) -> IndexResult
runtime/rag/retriever.py     # hybrid_search(session, request) -> RetrievalResult
runtime/rag/service.py       # index/retrieve/replay 事务编排
runtime/routers/knowledge.py # 三个知识库 API
runtime/db/alembic/versions/0004_r025_knowledge.py
```

必须实现的函数行为：

```python
def index_material(material_id: str, version: int, *, index_version: str) -> IndexResult:
    # 1. 读取不可变 Material/MaterialVersion
    # 2. 解析并生成带 location 的 chunk
    # 3. 校验 content_hash 与权限继承
    # 4. embedding 批量 upsert（唯一键幂等）
    # 5. 返回 created/skipped/failed，不吞异常

def hybrid_search(request: SearchRequest, actor: Actor) -> RetrievalResult:
    # 1. 校验 knowledge_layers/project_id/lot_id/as_of
    # 2. 先执行 SQL visibility predicate（不能先向量召回再过滤）
    # 3. FTS top_k 与 vector top_k 各取候选
    # 4. 用 RRF 合并，reranker 只排序
    # 5. 写 retrieval_runs，返回 candidate_only=True
```

### 8.2 混合检索算法

对同一查询分别取得 BM25 排名 `r_b` 和向量排名 `r_v`，使用 Reciprocal Rank Fusion：

```text
rrf(chunk) = 1 / (60 + r_b(chunk)) + 1 / (60 + r_v(chunk))
```

取 RRF 前 `RAG_TOP_K` 条；启用 reranker 时只改变显示顺序，不改变可见性和规则输入。SQL 过滤必须包含：

```sql
knowledge_layer IN (:layers)
AND permission_scope IN (:scopes)
AND (project_id IS NULL OR project_id = :project_id)
AND (lot_id IS NULL OR lot_id = :lot_id)
AND verification_status = 'active'
AND (valid_from IS NULL OR valid_from <= :as_of)
AND (valid_until IS NULL OR valid_until >= :as_of)
AND content_hash = :recorded_hash
```

L1 可按公开权限跨项目检索；L2/L3 查询缺少 `project_id` 或 `as_of` 直接返回 422；L3 结果默认脱敏，明细仅授权角色可见。

### 8.3 worker 任务与幂等键

扩展现有 `analysis_jobs.kind`：

```text
knowledge_index   # 材料版本 → chunk + embedding
knowledge_search  # 查询 → retrieval_run（可选异步）
match             # retrieval_run + structured snapshot → match_run
```

幂等键必须由以下字段拼接并 SHA-256：

```text
index:{material_id}:{version}:{content_hash}:{embedding_model}:{index_version}
search:{query_hash}:{filters_hash}:{index_version}:{as_of}
match:{project_id}:{tender_version}:{rule_set_version}:{evidence_snapshot_hash}:{retrieval_run_id}
```

任务失败只能写 `error_code/error_message` 并进入 `retryable` 或 `manual_review`；禁止把“无向量结果”当成空证据成功完成。

## 9. API 可执行契约

### 9.1 检索请求

```http
POST /api/v1/knowledge/search
Content-Type: application/json
X-Role: bid_specialist
X-Actor: user-001
```

```json
{
  "query": "建筑工程施工总承包二级及以上",
  "knowledge_layers": ["L2_tender", "L3_enterprise"],
  "project_id": "ND-2025",
  "lot_id": null,
  "as_of": "2025-10-30T09:00:00+08:00",
  "top_k": 20,
  "retrieval_mode": "hybrid"
}
```

### 9.2 检索响应

```json
{
  "request_id": "req-001",
  "retrieval_run_id": "rr-001",
  "candidate_only": true,
  "insufficient_evidence": false,
  "items": [{
    "chunk_id": "CH-001",
    "knowledge_layer": "L2_tender",
    "material_id": "MAT-ND-TENDER",
    "material_version": 2,
    "content_hash": "sha256...",
    "text": "...",
    "location": {"page_no": 42, "paragraph": 3},
    "verification_status": "active",
    "retrieval_score": 0.031,
    "citation": "MAT-ND-TENDER:v2:p42"
  }]
}
```

HTTP 规则：缺少必要上下文 `422`；越权 `403`；索引未就绪 `409 knowledge_not_ready`；服务异常 `503 retryable`。响应不得出现 `satisfied`、`qualified`、`approved_for_bidding` 等判定字段。

## 10. 测试代码与数据集

### 10.1 测试文件

```text
runtime/tests/test_rag_schemas.py       # 请求/响应 Schema 和枚举
runtime/tests/test_rag_chunker.py       # 页码、表头、重叠、哈希
runtime/tests/test_rag_filters.py       # 四角色、项目/标段/as_of/状态
runtime/tests/test_rag_indexer.py       # 幂等、版本失效、模型失败
runtime/tests/test_rag_retriever.py     # BM25/vector/RRF、candidate_only
runtime/tests/test_rag_api.py           # 422/403/409/503/OpenAPI
runtime/tests/test_rag_integration.py   # PostgreSQL + pgvector + 审计
tests/fixtures/rag/l2_gold.json         # 脱敏条款和相关 chunk ID
tests/fixtures/rag/l3_gold.json         # 脱敏企业证据和相关 chunk ID
tests/fixtures/rag/negative_acl.json    # 越权、跨项目、过期、跨标段
pytest.ini                              # 注册 rag 标记并保留 integration 标记
```

### 10.2 测试执行

```bash
alembic upgrade head
python -m pytest runtime/tests/test_rag_* -q
python -m pytest runtime/tests/test_rag_integration.py -m integration -q
python -m pytest runtime/tests -m "not integration" -q
python -m pytest --cov=runtime/rag --cov-fail-under=85 runtime/tests/test_rag_*.py
```

黄金集只提交脱敏文本、chunk ID 和标签，不提交企业原文件、向量数据库或 API key。

## 11. 可计算验收指标

以下指标必须写入 `reports/rag_acceptance_<run_id>.json`（该目录按现有规则忽略，不入 Git），并保留查询集版本、索引版本和计算脚本版本。

### 11.1 检索质量

| 指标 | 计算公式 | 样本要求 | 通过线 |
|---|---|---:|---:|
| L2 Recall@5 | `命中至少一个黄金条款 chunk 的查询数 / L2 查询总数` | ≥50 条查询，人工标注相关 chunk | ≥0.90 |
| L3 Recall@10 | `命中至少一个黄金证据 chunk 的查询数 / L3 查询总数` | ≥50 条查询，双人标注 | ≥0.90 |
| MRR@10 | `Σ(1/首个相关 chunk 排名) / 查询数` | 同上 | ≥0.80 |
| 引用完整率 | `含 material/version/hash/location 且可回跳的结果数 / 返回结果总数` | ≥200 条结果 | 1.00 |
| 权限泄漏率 | `不应可见但实际返回的 chunk 数 / 返回 chunk 总数` | 每角色≥30 条越权查询 | 0 |

### 11.2 结构化与规则质量

| 指标 | 计算公式 | 样本要求 | 通过线 |
|---|---|---:|---:|
| 数字/日期/等级 exact match | `规范化后与人工金标准完全一致的字段数 / 可判定字段总数` | ≥200 个字段，含金额/日期/等级各≥50 | ≥0.95 |
| 条款覆盖率 | `有有效 L2 候选且可回链的要求数 / 20 条农大要求` | 农大 20 条 | 1.00 |
| 规则结论一致率 | `与双人复核结论一致的 match_item / 已复核 match_item` | 农大 20 条 + 10 个负例 | ≥0.95 |
| 缺失阻断准确率 | `缺失样本正确映射 blocked_missing_data 的数量 / 缺失样本总数` | ≥10 个缺失负例 | 1.00 |
| 审批越权阻断率 | `非 eligible/过期/stale 结果被拒绝创建审批的数量 / 此类请求总数` | ≥20 个负例 | 1.00 |

### 11.3 可靠性、安全和性能

| 指标 | 测量方式 | 样本/环境 | 通过线 |
|---|---|---|---:|
| 索引幂等重复率 | 重复投递同一幂等键后新增 chunk/embedding 数 | 100 次重复事件 | 0 |
| 检索幂等重复率 | 重复相同 query/filter 后新增 retrieval_run 数 | 100 次重复请求 | 0（允许复用既有 run） |
| 版本失效阻断率 | 旧 chunk/结果驱动审批的次数 | 新版本/澄清/证据变更各10次 | 0 次放行 |
| 审计完整率 | `有 retrieval/index/match/approval 事件的操作数 / 操作总数` | ≥200 次操作 | 1.00 |
| 检索延迟 | API `p95`（不含首次模型加载） | ≤50,000 chunks、本地 PostgreSQL | ≤2 秒 |
| 农大索引时长 | 从任务开始到 L1/L2/L3 索引完成 | M5 16GB、单 worker、batch 4-8 | ≤30 分钟，超时可恢复 |
| 峰值内存 | worker/API RSS 最高值 | M5 16GB，完整农大样本 | ≤12 GB，无 OOM |
| 重启恢复率 | 重启后保持终态且不重复执行的任务数 / running 任务数 | ≥20 个任务 | 1.00 |

### 11.4 端到端硬门禁

农大验收必须同时满足：

```text
declared=20 AND executed=20 AND complete=true
AND L2 Recall@5 >= 0.90
AND L3 Recall@10 >= 0.90
AND exact_match >= 0.95
AND citation_completeness = 1.00
AND permission_leakage = 0
AND NQ-H-007 => blocked_missing_data
AND non_eligible_approval_requests => rejected
AND no_auto_bid_or_quote_action = true
```

任一硬门禁失败，R025 或薄切片不得标记 `done`；失败项必须进入人工复核、缺陷单或规格变更记录。

## 12. 开发完成定义（Definition of Done）

- 代码、迁移、配置、OpenAPI、测试和运行 README 同步提交；
- 所有新增字段均能回链 F025/F019，所有判定仍回链 F008/F023；
- 单元、契约、集成和黄金集测试通过，覆盖率达到方案阈值；
- `reports/rag_acceptance_<run_id>.json` 可由命令重新生成；
- 安全负例、版本重放、失败降级、重启恢复均有测试证据；
- 经营负责人确认业务结论，法务确认 DeepSeek 出域和个人信息处理；
- 未满足上述条件时，只能标记 R025 `in_progress`，不得宣称生产可用。

### 7.4 2026-09-09 重排闭环补充

- 自动匹配 worker 调用 `hybrid_search` 时注入 `runtime.rag.indexer._embed`，不再遗漏查询向量而退化为关键词检索。
- 本地模型服务新增 `/v1/rerank`，默认模型为 `BAAI/bge-reranker-v2-m3`；服务仅绑定 `127.0.0.1`，下载需显式执行 `bash embedding_serve/start.sh download-models`，不会在请求时联网下载。
- `RERANKER_ENABLED=true` 时 `/readyz` 检查模型服务及 reranker 资产；故障会使就绪检查失败，运行中单次重排异常则保留 RRF 基础排序并将检索运行标为 `*_rerank_degraded`。
- 检索分数只用于候选排列。候选仍须通过结构化字段、证据版本/有效期核验及 F008/F023 确定性规则，不能用 reranker 结果代替资格或准入判定。
