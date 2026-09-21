# R018 运行时技术基线与本地部署

> 对应规格：`docs/03-功能规格/F018-运行时技术基线与本地部署.md`（v1.0，2026-08-31）
> 依赖需求：R019（数据库持久化）、R020（API/任务编排/权限）
> 约束：仅手动触发（测试期 `manual_trigger`）；不自动投标/报价；企业资料与密钥不入 Git。

## 1. 组件选型

| 组件 | 选型 | 状态 |
|---|---|---|
| HTTP 服务 | FastAPI + Uvicorn | 依赖安装后可运行 |
| 数据访问 | SQLAlchemy + Alembic / PostgreSQL | 迁移已就绪（0001-0017；0017 为 ADR-004 Iteration 1 工作流）；本机隔离 PostgreSQL 已演练，远端部署仍待验 |
| 向量索引 | pgvector（`knowledge_embeddings`，可重建） | 迁移已就绪；未启用时索引/检索可解释降级 |
| 对象存储 | 本地受控目录（内网 MinIO 兼容） | 纯逻辑已实现 |
| 解析/OCR | pdftotext、tesseract（F017 复用） | 依赖环境 |
| 任务执行 | `analysis_jobs` 表 + 单 worker | 已实现（knowledge_index / match.run / match.recalculate 真实执行） |
| 模型适配器 | 内网/本地，结构化 JSON，外发拒绝 | 已实现（DeepSeek public-only 门禁 + 本地 embedding） |
| 规则引擎 | `scripts/matching/engine.py`（确定性，F008） | 已接入匹配执行器（可注入） |

## 2. 目录结构

```text
runtime/
  api.py                     FastAPI 应用（/healthz、/readyz、F020 业务路由挂载、统一异常处理）
  worker.py                  单 worker 任务循环入口（解析完成先索引后触发首次匹配；knowledge_index/match 真实执行）
  core/
    config.py                环境配置（F018 §3 字段清单 + R025 RAG/DeepSeek 参数）
    db.py                    数据库连接探测（socket，纯标准库）
    jobs.py                  analysis_jobs 状态机（纯逻辑，无依赖）
    objects.py               对象存储（SHA-256 / 校验 / 原子移动 / 病毒检查钩子）
    model.py                 模型适配器（内网检查 + 结构化 JSON + 用途拆分与 public-only 门禁）
    matching.py              匹配接入纯逻辑（证据快照/候选约束核验/规则引擎接入，方案 §3.5）
    identity.py              项目身份字段比较与三态冲突判定（ADR-004 P0-02）
    logging_utils.py         日志脱敏（身份证/手机号）
    errors.py                F020 统一错误码与错误响应（11 类 + knowledge_not_ready + request_id）
    rbac.py                  F020 四角色 RBAC（角色→操作→数据权限矩阵，越权审计）
    orchestration.py         F020 任务编排规则（完整文件前置/首次匹配一次/重算幂等/结果页只读/索引前置）
  rag/
    schemas.py               F025 Schema（SearchRequest/SearchResponse/KnowledgeChunkDTO/ChunkDraft）
    chunker.py               文档/章节/条款/表格分片（页码/段落保留）
    normalizer.py            金额/日期/等级/单位规范化（中文大写归一，确定性核验输入）
    filters.py               权限/项目/标段/as_of/核验状态可见性（fail-closed）
    indexer.py               索引流水线（幂等 upsert、版本失效、可注入 embed_fn，不伪造成功）
    retriever.py             混合检索（BM25 + pgvector + RRF，SQL 可见性先于召回）
    verification.py          结构化核验纯逻辑（金额/日期/等级/有效期/哈希三分）
    service.py               索引/检索/回放事务编排（幂等键、retrieval_runs 快照）
  routers/
    deps.py                  公共依赖（request_id / X-Role / 数据库会话）
    knowledge.py             RAG 检索 API（search / indexes / retrieval-runs，candidate_only）
    materials.py             材料（POST/GET /materials、/{id}/versions、/{id}/verify）
    intake.py                搜索推送/公告入库/招标文件上传/任务状态/重解析
    projects.py              项目列表/详情/requirements/match-runs/matrix/admission/queues
    match.py                 匹配触发与补录重算（仅编排器调用，前端不暴露）
    approvals.py             待审/创建/批准/驳回/豁免/审计
    enterprise.py            企业资料库后台（资质/安许/经理/业绩/人员/证据）
  db/
    models.py                SQLAlchemy 模型（analysis_jobs + F019 全表 + RAG + worker_heartbeats/project_identities）
    worker_service.py        任务领取/进程心跳/完成/失败（SQLAlchemy 会话层）
    identity_service.py      公告/文件身份事实汇集、落库与人工确认（ADR-004）
    lifecycle_service.py     截止过期、身份冲突与正式操作门禁（ADR-004）
    material_service.py      材料导入/版本化/元数据更新/哈希复核（F019 §4）
    api_service.py           F020 API 服务层（项目/审批/豁免/审计/准入/队列/矩阵）
    alembic/                 Alembic 迁移（0001-0017；0014-0016 为 ADR-004 P0 门禁/精确截止，0017 为 Iteration 1 工作流）
  core/
    versioning.py            版本决策/有效期状态/不可覆盖保护（纯逻辑，无依赖）
  tests/
    test_jobs_state_machine.py   状态机确定性测试
    test_objects.py              对象存储测试
    test_versioning.py           版本管理纯逻辑测试
    test_logging_and_model.py    脱敏与模型内网检查测试
    test_rbac.py                 F020 RBAC 权限矩阵测试
    test_errors.py               F020 统一错误码测试
    test_orchestration.py        F020 任务编排规则测试
    test_api.py                  /healthz、/readyz 测试（需要 fastapi+httpx）
    test_api_contracts.py        F020 API 契约测试（需要 python-multipart）
    test_material_service.py     材料持久化集成测试（需要真实 PostgreSQL，integration 标记）
    test_api_integration.py      R020 集成测试（需要真实 PostgreSQL，integration 标记）
    test_rag_verification.py     RAG 结构化核验纯逻辑测试（rag 标记）
    test_matching.py             匹配接入纯逻辑测试（rag 标记）
    test_worker_match.py         worker 匹配执行器链路测试（sqlite 内存库，rag 标记）
    test_p0_gates.py             ADR-004 身份/截止/心跳/证据降级单元与路由契约
    test_p0_gates_integration.py ADR-004 PostgreSQL + HTTP + worker 门禁薄切片（integration 标记）
alembic.ini                  Alembic 配置（连接串从 DATABASE_URL 读取）
```

## 3. 快速开始（新机器验收路径）

> 注意：本机在 Cursor 沙盒内运行 Agent 时**无任何网络能力**（沙盒网络策略，连回环 socket 都被禁止），
> 因此"安装 PostgreSQL / pip 装依赖"这类联网操作必须在**沙盒外的系统终端**执行。
> 已提供一键脚本，在项目根目录执行一次即可完成全部安装：
>
> 若本机 5432 端口已被其他 PostgreSQL 实例占用（如 Docker 容器/旧实例），脚本会自动
> 把本项目数据库切到 **5433 端口**，不影响现有服务，日志会明确提示。

```bash
# ⚠️ 在系统终端（Terminal.app / iTerm，非 Cursor Agent）执行：
bash scripts/setup_local_env.sh        # 安装+建库+pgvector扩展+迁移+启动+健康检查
# 常用管理：
bash scripts/setup_local_env.sh start  # 仅启动 API + worker
bash scripts/setup_local_env.sh stop   # 停止
bash scripts/setup_local_env.sh status # 状态与健康检查
```

> 一键脚本会在迁移前自动安装 pgvector（Homebrew）并在 `bid_agent` 库创建
> `vector` 扩展——迁移 0004 的向量列依赖该扩展（F025 §7；Homebrew postgresql@17 不内置）。

本地 embedding 服务（可选但推荐，RAG 向量索引/检索依赖）：

```bash
# 首次安装：依赖与模型下载均为显式步骤（请求和 start 不会隐式联网下载）
cd embedding_serve
bash start.sh setup
bash start.sh download-models  # BAAI/bge-m3 + BAAI/bge-reranker-v2-m3，需联网、数 GB
bash start.sh start
# 常用管理：
bash start.sh status   # GET http://127.0.0.1:8001/health；embedding/reranker 资产状态均可见
bash start.sh stop     # 停止
cd ..
```

> embedding_serve 提供 OpenAI 兼容 `POST /v1/embeddings`（本地 `bge-m3-local`）和
> `POST /v1/rerank`（本地 `bge-reranker-v2-m3-local`，仅排序）。`runtime/.env` 的
> `MODEL_BASE_URL` 指向 `http://127.0.0.1:8001`，`EMBEDDING_MODEL=bge-m3-local`；仅在 reranker
> 模型资产可用后设置 `RERANKER_ENABLED=true`。embedding 服务未启动时索引任务 retryable；
> 重排运行失败时保留可审计的基础排序，均不伪造成功（F025 §8.1）。注意：开发/CI 会话若
> 注入了 `PYTHONPATH`（如 Hermes），启动脚本已自动 `unset`。

脚本等价于手工执行以下步骤（F018 §7）：

```bash
# 1) 虚拟环境与依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# 2) 配置（复制模板，填写本机路径；.env 不入 Git；API/worker 启动时自动加载，
#    已有环境变量优先，无需手工 source）
cp runtime/.env.example runtime/.env

# 3) 启动 PostgreSQL 并执行迁移（F018 §7.3）
brew install postgresql@17 && brew services start postgresql@17
# 应用账号 bid_agent（密码 bid_agent_dev，仅本地开发）由一键脚本自动创建；
# 手工环境可自行执行：
#   psql -d postgres -c "CREATE ROLE bid_agent LOGIN PASSWORD 'bid_agent_dev';"
#   createdb -O bid_agent bid_agent
.venv/bin/alembic upgrade head

# 4) 启动 API 与单 worker（F018 §7.4）
.venv/bin/uvicorn runtime.api:app --host 127.0.0.1 --port 8000
.venv/bin/python -m runtime.worker

# 5) 健康检查
curl http://127.0.0.1:8000/healthz   # 进程存活
curl http://127.0.0.1:8000/readyz    # 数据库/对象根目录/OCR/模型/知识库/异步 worker 心跳可用性
```

`/readyz` 在缺少数据库、OCR 或模型时返回 503 并逐项说明失败原因（F018 §8）。
**ADR-004 §2.6（2026-09-16）**：新增 `checks.worker`——worker 进程以守护线程周期写入 `worker_heartbeats`，
最近心跳超过 `WORKER_HEARTBEAT_STALE_SECONDS`（默认 60s）或从未写入即报告不可用并使整体 503
（API 起了不代表异步采集/解析/匹配/重算可用）；响应另含 `impacts[]` 说明各不可用项的影响范围，
原型顶栏据此分项显示 API / 数据库 / 异步任务 / 模型 / 检索。**部署必须同时启动 worker**。

## 4. 任务状态机

```text
pending -> running -> completed
             |  \-> retryable -> pending（attempts < max_attempts）
             |            \-> failed
             \-> cancelled
```

- 幂等键 `kind + input_ref + project_id` 唯一（F019 §3 `analysis_jobs`），重复提交返回既有任务。
- `running` 超过 `JOB_RUNNING_TIMEOUT_SECONDS` 无心跳 → `retryable`（F018 §4.4），**不直接推进业务状态**。
- ADR-004 服务端门禁（项目过投标截止 `overdue` / 身份冲突 `identity_conflict`）拦截的 gate 匹配任务
  **终态失败不重试**（`error_code=gate_overdue / gate_identity_conflict`），纠正数据后由人工重新发起。
- 终态（completed/failed/cancelled）不可迁移；失败任务不自动重跑。
- 所有异常携带 `request_id`/`job_id`，日志只记录元数据与错误摘要（F018 §4.5），身份证号/手机号自动脱敏。

## 5. 对象存储流程（F018 §4.3 / F019 §4）

```
临时目录 -> SHA-256 -> 格式/大小校验 -> 病毒检查钩子（可选） -> 原子移动 -> 不可变路径
对象路径：owner_type/material_id/version/sha256
```

- 内容重复上传返回既有版本；内容变化必须新建版本（F019 §4）。
- 哈希复核失败时任务阻断（`verify_material` 抛 `HashMismatchBlocked` 并写审计事件）。
- 禁止扩展名（exe/dll/html/js 等）直接拒绝。
- 临时文件由 `clean_tmp_files` 清理（F018 §7.6）。

## 6. 数据分层（F019 §2/§3）

同一 PostgreSQL 实例的四个 schema，角色按需授权（`scripts/setup_rbac.sql`）：

| schema | 内容 | 写权限 |
|---|---|---|
| `public_data` | 公告/招标文件/项目主卡/事实卡/条款引用（projects、materials、material_versions、field_traces） | 采集/入库服务 |
| `enterprise_data` | 资质/业绩/人员/项目经理/私有证据元数据（qualifications、performances、personnel、managers、evidence_files） | 数据管理员（匹配服务经受控视图只读） |
| `admission_data` | 规则快照/匹配运行/准入结果/审批/豁免（rule_sets、requirements、match_runs、match_items、admission_results、approvals、waivers） | 匹配/准入服务 |
| `audit_data` | 仅追加审计事件（audit_events），不提供 UPDATE/DELETE 应用路径 | 各服务追加 |
| `knowledge_data` | RAG 分片、embedding、检索运行（knowledge_chunks、knowledge_embeddings、retrieval_runs）；仅作候选证据入口，不替代事实表 | 索引/检索服务 |

不可变约束：`materials` 的 `content_hash`/`version` 只增不改；`material_versions` 无更新应用路径；
`rule_sets` 版本不可覆盖；`admission_results` 为不可变快照；缺证据默认 `pending_verification`。

## 7. 安全与合规

- 开发环境默认绑定 `127.0.0.1`（`BIND_HOST`），内网部署再配置反向代理与 TLS（F018 §6）。
- 模型适配器必须通过内网检查：`MODEL_BASE_URL` 缺失、外网域名或公网 IP 一律拒绝调用（`runtime/core/model.py`）。
- F025 约束：`DEEPSEEK_PUBLIC_ONLY=true`；DeepSeek 仅处理 public 数据，企业 embedding 只走本地/内网；`PGVECTOR_ENABLED=false` 时不得创建“成功”的索引或检索结果。
- 日志脱敏：身份证号、手机号 → `[REDACTED]`（`RecordSensitiveFilter`）。
- 企业资料、`.env`、真实路径、对象文件一律不提交 Git（见根 `.gitignore`）。

## 7.1 RAG 运行链路（R025/F025，docs/07 方案 §3.5）

```text
解析完成（parse.tender_document）
  → 编排器先创建 knowledge_index 任务（幂等 index:... 键，worker 执行 indexer.index_material）
  → 再创建一次 match.run 任务（幂等，仅首次；结果页 GET 只读不创建 run）
match.run / match.recalculate 执行器（worker._execute_match_run）：
  1. 读取项目最新 RuleSet/Requirement；缺规则集或 as_of → 任务 retryable（不伪造成功）
  2. 构建截至 as_of 的企业资料 active 快照（matching.build_enterprise_evidence，缺字段不推断）
  3. 每条要求以条款文本检索 L2/L3 候选（retriever.hybrid_search；索引未就绪 → 可解释降级）
  4. 候选按 material_id 关联结构化记录做约束核验（verification.verify_candidates）：
     blocked（无法判定）→ 从判定输入剔除；failed（明确不满足）→ 保留给引擎判 not_satisfied
  5. 核验通过的证据交给 F008 确定性规则引擎（scripts/matching/engine.py，可注入）
  6. MatchRun 落库快照：retrieval_run_id / index_version / candidate_chunk_ids /
     structured_verification / evidence_snapshot_hash；MatchItem 逐条带 retrieval_run_id 与证据引用
```

- 向量分数/LLM 置信度只影响候选发现排序，不进入评分与准入公式（F025 §6）。
- 召回不到证据 → `unverifiable`/`blocked_missing_data`；证据不满足 → `not_satisfied`/硬性阻断。
- 同一证据快照重复执行返回一致 `evidence_snapshot_hash`，检索运行按 query/filter/index/as_of 复用（可回放）。

## 8. 验收与测试

| 命令 | 说明 |
|---|---|
| `python -m pytest runtime/tests -v` | 状态机/对象存储/版本管理/脱敏/模型内网检查/API 测试/RBAC/编排（CI 同款；集成测试与缺 python-multipart 的 API 契约测试自动跳过）。注意：若 shell 注入了外部 `PYTHONPATH` 请先 `unset PYTHONPATH`；`test_api.py` 另需 `unset DATABASE_URL`（fixture 用 `setdefault`，避免命中真实库） |
| `export DATABASE_URL="postgresql+psycopg://bid_agent:bid_agent_dev@127.0.0.1:5432/bid_agent" && python -m pytest runtime/tests/test_material_service.py -m integration -v` | 材料持久化集成测试（真实 PostgreSQL，需先 `alembic upgrade head`；`bid_agent_dev` 为一键脚本 `setup_local_env.sh` 创建的本地默认密码，若已改密请替换） |
| `export DATABASE_URL="postgresql+psycopg://bid_agent:bid_agent_dev@127.0.0.1:5432/bid_agent" && python -m pytest runtime/tests/test_api_integration.py -m integration -v` | R020 集成测试（9 项：首次匹配只触发一次、重算幂等、非满分拒绝审批、驳回 comment 必填、豁免过期回阻断、越权审计留痕） |
| `export DATABASE_URL="postgresql+psycopg://bid_agent:bid_agent_dev@127.0.0.1:5432/bid_agent" && python -m pytest runtime/tests/test_rag_integration.py -m integration -v` | R025 RAG 真库集成测试（5 项：索引幂等不误标 stale、版本变更旧 chunk 失效、无索引检索 409、L2/L3 缺项目上下文拒绝、项目隔离 + retrieval_runs 复用；需 pgvector 扩展与迁移 0004，`setup_local_env.sh` 自动安装扩展） |
| `python -m pytest runtime/tests/test_iteration1_workflow.py runtime/tests/test_worker_match.py -q` | ADR-004 Iteration 1 本地回归：快速预核、准备立项、身份 warning 阻断、任务权限/脱敏/自动关闭、worker 自动任务投影及失败审计。 |
| `export DATABASE_URL=<隔离验证库> && python -m pytest runtime/tests/test_iteration1_workflow_integration.py -m integration -v` | ADR-004 Iteration 1 PostgreSQL HTTP 薄切片：0017 迁移后的快速预核、身份 warning 阻断、准备立项、补证状态和跨角色证据引用脱敏；**不得指向共享演示库或生产库**。 |
| `python -m pytest runtime/tests -m rag -v` | R025 RAG 测试（32 项，rag 标记）：结构化核验三分、匹配接入纯逻辑、worker 匹配执行器链路（sqlite 内存库）；检索/权限/引用/索引幂等集成项待真库 |
| `export DATABASE_URL=... && python scripts/verify_rag_e2e.py` | R025 真库端到端回归（插材料→真实索引（bge-m3）→hybrid/vector 检索→断言 cosine 排序/项目隔离/run 复用→TRUNCATE 清理）；需 embedding_serve 运行中（§3） |
| `.venv/bin/pip install -r requirements-dev.txt` | 安装 `python-multipart`（F020 multipart 上传必需）后，`test_api_contracts.py` 自动恢复执行（越权 403/空文件/非法格式/非法 collect_mode/OpenAPI 路由注册） |
| `python scripts/check_r018_runtime.py` | 无依赖环境纯逻辑编译检查（本机离线可跑） |
| `python -m unittest discover -s scripts/tests -v` | 既有脚本测试（回归） |
| `python scripts/matching/run_golden_lab.py` | 黄金样本回归（R009 既有） |
| `psql -h /tmp -U <管理员> -d bid_agent -f scripts/setup_rbac.sql && bash scripts/verify_rbac.sh` | F019 §6 验收③：四层 RBAC 隔离验证（管理员为 Homebrew PostgreSQL 超级用户，默认当前系统用户；脚本以管理员连接后 `SET ROLE` 切换 NOLOGIN 角色验证） |
| `bash scripts/setup_local_env.sh start` 后按下方演练步骤执行 | F018 §8：进程重启不丢任务演练（幂等键去重 + 状态机恢复） |

F018 §8 验收项对应：

- 新机器按本文档可完成启动；依赖版本可复现（`requirements-dev.txt`）。
- API/worker 任一进程重启不丢任务、不产生重复 Material（幂等键 + 状态机 + 超时恢复）。

进程重启不丢任务演练步骤（F018 §8）：

```bash
bash scripts/setup_local_env.sh start                     # 1. 启动 API + worker
.venv/bin/python scripts/verify_restart_drill.py submit   # 2. 投递任务，预期 created=True
kill "$(cat logs/worker.pid)"                             # 3. 杀 worker（模拟崩溃；API 不受影响）
.venv/bin/python scripts/verify_restart_drill.py submit   # 4. 重复投递，预期 created=False（幂等键去重，不产生重复任务）
bash scripts/setup_local_env.sh start                     # 5. 重启 worker
.venv/bin/python scripts/verify_restart_drill.py status   # 6. 任务仍在库中（completed，不丢失）
```
- 缺少数据库、OCR 或模型时 `/readyz` 明确失败；业务任务进入可解释错误状态（`error_code`）。
- CI 与本地命令一致（`.github/workflows/quality.yml`；CI 无数据库，仅跑 `-m "not integration"`）。

F019 §6 验收项对应（需要真实 PostgreSQL 的项由集成测试 + 手工命令覆盖）：

- Material 15 字段、F006/F007/F008/F009 对象均可创建、查询、版本化（`test_material_service.py`）。
- raw 内容更新请求被拒绝；新版本可查询且旧版本仍可回放（`update_metadata` 仅改元数据；`ensure_version_immutable` 拒绝覆盖）。
- `public_data` 角色无法读取 `enterprise_data` 明细；审批数据仅 approver 视图可见（`scripts/setup_rbac.sql` + `scripts/verify_rbac.sh`）。
- 随机修改对象文件后哈希校验失败，匹配任务不启动（`test_verify_detects_tampering`）。
- 备份恢复后农大项目的规则、证据、匹配和审计引用完整（`scripts/backup_runtime.sh` + 恢复后 `verify_material`）。

## 9. 已知限制（如实记录）

- 本机开发环境无外网，`pip install` 需在内网镜像或 CI 执行；PostgreSQL 需本机/内网实例。
- R020 已提供全部 F020 接口与编排骨架；`knowledge_index` / `match.run` / `match.recalculate` 已接入真实执行（RAG→核验→规则引擎→快照）；解析/准入执行器仍由 R021-R024 接入。
- 匹配执行器的 RAG 候选检索依赖知识索引就绪；索引未就绪时降级为结构化 active 快照判定（`rag_degraded` 如实记录，不伪造成功）。
- 本地 embedding 已真实接入（`runtime/core/model.py` → `embedding_serve/` bge-m3，OpenAI 兼容 `/v1/embeddings`）；embedding 服务未启动时索引任务 retryable、检索降级关键词，`/readyz` 对应项如实报不可用。
- `performances` 表补 `verified_at`（迁移 0005，2026-09-02）：业绩证据可通过 `engine.evidence_is_valid` 已核验检查，`build_enterprise_evidence` 恢复类似业绩客观项（NQ-S-003/004）时点快照判定；未核验（verified_at 为空）记录仍保守剔除（缺失阻断，不推断）。
- 匹配判定依赖项目规则集（`rule_sets`/`requirements`）与 `as_of`（不得默认当前时间）；规则集数据由后续 F023 数据导入或人工录入提供，缺省时匹配任务 `retryable` 并如实报错。
- 身份认证为开发期契约：`X-Role`/`X-Actor` 请求头；正式认证接入 R024（auth）时替换，RBAC 矩阵不变。
- 企业资料明细（enterprise_data）对投标专员/法务 fail-closed（F003 §6.2），越权一律 403 + 审计。
- `model.health` 对未启用模型返回 `enabled=False`，`/readyz` 视为"模型未启用"而非失败。
- 集成测试（`-m integration`）、API 契约测试（需 python-multipart）与 RBAC 验证需沙盒外的真实 PostgreSQL 环境执行，命令见本文件 §8。
