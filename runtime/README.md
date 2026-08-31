# R018 运行时技术基线与本地部署

> 对应规格：`docs/03-功能规格/F018-运行时技术基线与本地部署.md`（v1.0，2026-08-31）
> 依赖需求：R019（数据库持久化）、R020（API/任务编排/权限）
> 约束：仅手动触发（测试期 `manual_trigger`）；不自动投标/报价；企业资料与密钥不入 Git。

## 1. 组件选型

| 组件 | 选型 | 状态 |
|---|---|---|
| HTTP 服务 | FastAPI + Uvicorn | 依赖安装后可运行 |
| 数据访问 | SQLAlchemy + Alembic / PostgreSQL | 迁移已就绪，需本机 PostgreSQL |
| 对象存储 | 本地受控目录（内网 MinIO 兼容） | 纯逻辑已实现 |
| 解析/OCR | pdftotext、tesseract（F017 复用） | 依赖环境 |
| 任务执行 | `analysis_jobs` 表 + 单 worker | 已实现 |
| 模型适配器 | 内网/本地，结构化 JSON，外发拒绝 | 已实现 |

## 2. 目录结构

```text
runtime/
  api.py                     FastAPI 应用（/healthz、/readyz、启动迁移检查）
  worker.py                  单 worker 任务循环入口（python -m runtime.worker）
  core/
    config.py                环境配置（F018 §3 字段清单）
    db.py                    数据库连接探测（socket，纯标准库）
    jobs.py                  analysis_jobs 状态机（纯逻辑，无依赖）
    objects.py               对象存储（SHA-256 / 校验 / 原子移动 / 病毒检查钩子）
    model.py                 模型适配器（内网检查 + 结构化 JSON）
    logging_utils.py         日志脱敏（身份证/手机号）
  db/
    models.py                SQLAlchemy 模型（analysis_jobs）
    worker_service.py        任务领取/心跳/完成/失败（SQLAlchemy 会话层）
    alembic/                 Alembic 迁移（env.py + 0001 初始迁移）
  tests/
    test_jobs_state_machine.py   状态机确定性测试
    test_objects.py              对象存储测试
    test_logging_and_model.py    脱敏与模型内网检查测试
    test_api.py                  /healthz、/readyz 测试（需要 fastapi+httpx）
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
bash scripts/setup_local_env.sh        # 安装+建库+迁移+启动+健康检查
# 常用管理：
bash scripts/setup_local_env.sh start  # 仅启动 API + worker
bash scripts/setup_local_env.sh stop   # 停止
bash scripts/setup_local_env.sh status # 状态与健康检查
```

脚本等价于手工执行以下步骤（F018 §7）：

```bash
# 1) 虚拟环境与依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# 2) 配置（复制模板，填写本机路径；.env 不入 Git）
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
curl http://127.0.0.1:8000/readyz    # 数据库/对象根目录/OCR/模型可用性
```

`/readyz` 在缺少数据库、OCR 或模型时返回 503 并逐项说明失败原因（F018 §8）。

## 4. 任务状态机

```text
pending -> running -> completed
             |  \-> retryable -> pending（attempts < max_attempts）
             |            \-> failed
             \-> cancelled
```

- 幂等键 `kind + input_ref + project_id` 唯一（F019 §3 `analysis_jobs`），重复提交返回既有任务。
- `running` 超过 `JOB_RUNNING_TIMEOUT_SECONDS` 无心跳 → `retryable`（F018 §4.4），**不直接推进业务状态**。
- 终态（completed/failed/cancelled）不可迁移；失败任务不自动重跑。
- 所有异常携带 `request_id`/`job_id`，日志只记录元数据与错误摘要（F018 §4.5），身份证号/手机号自动脱敏。

## 5. 对象存储流程（F018 §4.3 / F019 §4）

```
临时目录 -> SHA-256 -> 格式/大小校验 -> 病毒检查钩子（可选） -> 原子移动 -> 不可变路径
对象路径：owner_type/material_id/version/sha256
```

- 内容重复上传返回既有版本；内容变化必须新建版本（F019 §4）。
- 哈希复核失败时任务阻断（`verify_object`）。
- 禁止扩展名（exe/dll/html/js 等）直接拒绝。
- 临时文件由 `clean_tmp_files` 清理（F018 §7.6）。

## 6. 安全与合规

- 开发环境默认绑定 `127.0.0.1`（`BIND_HOST`），内网部署再配置反向代理与 TLS（F018 §6）。
- 模型适配器必须通过内网检查：`MODEL_BASE_URL` 缺失、外网域名或公网 IP 一律拒绝调用（`runtime/core/model.py`）。
- 日志脱敏：身份证号、手机号 → `[REDACTED]`（`RecordSensitiveFilter`）。
- 企业资料、`.env`、真实路径、对象文件一律不提交 Git（见根 `.gitignore`）。

## 7. 验收与测试

| 命令 | 说明 |
|---|---|
| `python -m pytest runtime/tests -v` | 状态机/对象存储/脱敏/模型内网检查/API 测试（CI 同款） |
| `python scripts/check_r018_runtime.py` | 无依赖环境纯逻辑编译检查（本机离线可跑） |
| `python -m unittest discover -s scripts/tests -v` | 既有脚本测试（回归） |
| `python scripts/matching/run_golden_lab.py` | 黄金样本回归（R009 既有） |

F018 §8 验收项对应：

- 新机器按本文档可完成启动；依赖版本可复现（`requirements-dev.txt`）。
- API/worker 任一进程重启不丢任务、不产生重复 Material（幂等键 + 状态机 + 超时恢复）。
- 缺少数据库、OCR 或模型时 `/readyz` 明确失败；业务任务进入可解释错误状态（`error_code`）。
- CI 与本地命令一致（`.github/workflows/quality.yml`）。

## 8. 已知限制（如实记录）

- 本机开发环境无外网，`pip install` 需在内网镜像或 CI 执行；PostgreSQL 需本机/内网实例。
- R018 只提供任务骨架与状态机；具体执行器（解析/匹配/准入）由 R021-R024 接入 `_execute`。
- `model.health` 对未启用模型返回 `enabled=False`，`/readyz` 视为"模型未启用"而非失败。
- 迁移仅建 `analysis_jobs` 表；R019 其余表在后续迁移追加。