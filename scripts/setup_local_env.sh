#!/usr/bin/env bash
# =============================================================================
# R018 本地环境一键安装与启动脚本
#
# ⚠️ 必须在【沙盒外】的系统终端执行（Terminal.app / iTerm），原因：
#   Cursor Agent 沙盒网络策略禁止一切网络操作（DNS、TCP/UDP、socket 均被禁），
#   无法在沙盒内完成 brew 下载、pip 安装；本脚本即"沙盒外执行"的载体。
#
# 用法（在项目根目录）：
#   bash scripts/setup_local_env.sh          # 安装 + 建库 + 迁移 + 启动 + 健康检查
#   bash scripts/setup_local_env.sh start    # 启动 embedding(8001) + API + worker
#   bash scripts/setup_local_env.sh stop     # 停止 embedding + API + worker
#   bash scripts/setup_local_env.sh status   # 查看进程与健康检查
#
# 幂等：重复执行安全（已装依赖/已建库会自动跳过）。
# 2026-09-09：embedding 服务（BGE-M3，127.0.0.1:8001）已纳入一键管辖——否则 8001 未起时
#   runtime /readyz 报 knowledge.embedding Connection refused → readyz=false → 前端误判
#   "API 不可达"。可用 EMBED_SKIP=1 单独跳过 embedding（不影响 API/worker）。
# =============================================================================
set -euo pipefail

# Homebrew 静默化（减少联网安装时的交互与噪音；装依赖的确认提示用 yes 自动应答）
export HOMEBREW_NO_AUTO_UPDATE=1
export HOMEBREW_NO_INSTALL_CLEANUP=1
export HOMEBREW_NO_ENV_HINTS=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# 本地开发数据库账号：bid_agent / bid_agent_dev（仅本机开发默认值，可用环境变量覆盖）
# 该角色与密码由 setup_postgres 自动创建并同步
export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://bid_agent:bid_agent_dev@127.0.0.1:5432/bid_agent}"
export BID_AGENT_DB_PASSWORD="${BID_AGENT_DB_PASSWORD:-bid_agent_dev}"
export APP_ENV=dev
export LOG_LEVEL=INFO
export OBJECT_STORE_ROOT="${OBJECT_STORE_ROOT:-$ROOT/runtime/objects}"

PG_BIN="$(brew --prefix postgresql@17 2>/dev/null || true)/bin"
[[ -n "$PG_BIN" && -d "$PG_BIN" ]] && export PATH="$PG_BIN:$PATH"

mkdir -p logs runtime/objects

log()  { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[err]\033[0m %s\n' "$*" >&2; exit 1; }

setup_pgvector() {
  # R025：pgvector 扩展（knowledge_embeddings 向量列依赖；Homebrew postgresql@17 不内置）。
  # 必须在 alembic 迁移（0004 使用 vector 类型）之前执行；Homebrew pgvector 装到独立 prefix，
  # 需把 vector.so 与扩展 SQL 软链到 PostgreSQL 目录，再以管理员在应用库 CREATE EXTENSION。
  local ph="$1"
  if ! brew list pgvector >/dev/null 2>&1; then
    log "安装 pgvector（Homebrew，需联网，约 1 分钟）..."
    yes | brew install pgvector
  else
    ok "pgvector 已安装"
  fi
  local PGLIB PGSHARE PGVEC so f PG_MAJOR
  PGLIB="$(pg_config --pkglibdir)"
  PGSHARE="$(pg_config --sharedir)/extension"
  PGVEC="$(brew --prefix pgvector)"
  PG_MAJOR="$(pg_config --version | sed -E 's/PostgreSQL ([0-9]+).*/\1/')"
  # Homebrew pgvector 的库产物：macOS 为 vector.dylib、Linux 为 vector.so。
  # 任一存在即视为已链接，避免误判缺失后重复链接/误报 die。
  if [[ ! -f "$PGLIB/vector.so" && ! -f "$PGLIB/vector.dylib" ]]; then
    # 限定本机 PG 大版本目录（pgvector 同时提供 @17/@18 产物，避免链错版本）
    so="$(find "$PGVEC" -type f \( -name 'vector.so' -o -name 'vector.dylib' \) -path "*postgresql@${PG_MAJOR}*" 2>/dev/null | head -1 || true)"
    if [[ -n "$so" ]]; then
      ln -sf "$so" "$PGLIB/$(basename "$so")"
    else
      die "未找到 pgvector 的库文件 vector.so/vector.dylib（$PGVEC），请检查 brew install pgvector 输出"
    fi
  fi
  for f in "$PGVEC"/share/postgresql@*/extension/vector* "$PGVEC"/share/postgresql/extension/vector*; do
    [[ -f "$f" ]] && ln -sf "$f" "$PGSHARE/$(basename "$f")"
  done
  psql $ph -w -d bid_agent -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null \
    || die "CREATE EXTENSION vector 失败，请检查 vector.so 链接与 PG 日志"
  psql $ph -w -d bid_agent -tAc "SELECT 1 FROM pg_extension WHERE extname='vector'" | grep -q 1 \
    || die "pgvector 扩展创建失败"
  ok "pgvector 扩展就绪（bid_agent 库，vector 类型可用于 0004 迁移）"
}

setup_postgres() {
  if command -v psql >/dev/null 2>&1; then
    ok "PostgreSQL 已安装: $(psql --version)"
  else
    log "安装 PostgreSQL 17（Homebrew，需联网，约 1-3 分钟）..."
    yes | brew install postgresql@17
    export PATH="$(brew --prefix postgresql@17)/bin:$PATH"
  fi

  # ---- 启动服务（幂等）：若 Homebrew 实例已在运行则直接复用 ----
  local PGDATA="/opt/homebrew/var/postgresql@17"
  brew services start postgresql@17 >/dev/null 2>&1 || true

  # ---- 确定可用 socket：优先 5432；若 5432 的 socket 始终未出现（端口被他人占用），切 5433 ----
  local PG_PORT=5432 sock="/tmp/.s.PGSQL.5432" i
  for i in $(seq 1 30); do
    [[ -S "$sock" ]] && break
    sleep 1
  done
  if [[ ! -S "$sock" ]]; then
    log "5432 的 socket 未就绪，检查端口占用情况："
    lsof -iTCP:5432 -sTCP:LISTEN -P -n 2>/dev/null | tail -n +2 | head -5 || true
    log "将 Homebrew postgresql@17 切换到 5433 端口（原配置备份为 postgresql.conf.bak）..."
    cp "$PGDATA/postgresql.conf" "$PGDATA/postgresql.conf.bak" 2>/dev/null || true
    if grep -qE '^#?port[[:space:]]*=' "$PGDATA/postgresql.conf"; then
      sed -i '' 's/^#\?port[[:space:]]*=[[:space:]]*[0-9]*/port = 5433/' "$PGDATA/postgresql.conf"
    else
      echo "port = 5433" >> "$PGDATA/postgresql.conf"
    fi
    grep -qE '^port[[:space:]]*=[[:space:]]*5433' "$PGDATA/postgresql.conf" \
      || die "端口修改未生效，请手动检查 $PGDATA/postgresql.conf"
    brew services restart postgresql@17 >/dev/null 2>&1 || true
    PG_PORT=5433
    sock="/tmp/.s.PGSQL.5433"
    for i in $(seq 1 30); do
      [[ -S "$sock" ]] && break
      sleep 1
    done
  fi
  if [[ ! -S "$sock" ]]; then
    echo "---- 诊断：brew services 状态 ----"
    brew services info postgresql@17 2>/dev/null || true
    echo "---- 诊断：服务日志尾部 ----"
    for lg in /opt/homebrew/var/log/postgresql@17.log /opt/homebrew/var/log/postgresql@17/*.log; do
      if [[ -f "$lg" ]]; then
        echo ">>> $lg"
        tail -30 "$lg" || true
      fi
    done
    die "PostgreSQL 未就绪（socket: $sock），诊断信息见上，请检查日志后重试"
  fi
  # 端口以实际出现的 socket 为准
  PG_PORT="${sock##*.PGSQL.}"
  export PGPORT="$PG_PORT"
  export DATABASE_URL="postgresql+psycopg://bid_agent:${BID_AGENT_DB_PASSWORD}@127.0.0.1:${PG_PORT}/bid_agent"
  ok "PostgreSQL 就绪（socket ${sock}，端口 ${PG_PORT}）"

  # 应用角色与数据库（幂等）
  # Homebrew 默认 socket 连接为 trust，管理操作一律经 socket 执行并加 -w 禁止交互密码提示；
  # 应用走 TCP 使用独立角色 bid_agent（最小权限，仅 LOGIN）。
  local pw="$BID_AGENT_DB_PASSWORD" PH=""
  # socket 目录：Homebrew 默认 /tmp；以实际 socket 路径的目录为准
  PH="-h $(dirname "$sock")"
  if ! psql $PH -w -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='bid_agent'" | grep -q 1; then
    psql $PH -w -d postgres -v ON_ERROR_STOP=1 -c "CREATE ROLE bid_agent LOGIN PASSWORD '${pw}'" >/dev/null \
      || die "创建角色 bid_agent 失败（socket 连接异常，请检查: brew services info postgresql@17）"
    ok "角色 bid_agent 已创建"
  else
    psql $PH -w -d postgres -v ON_ERROR_STOP=1 -c "ALTER ROLE bid_agent WITH LOGIN PASSWORD '${pw}'" >/dev/null
    ok "角色 bid_agent 已存在（密码已同步）"
  fi
  if ! psql $PH -w -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='bid_agent'" | grep -q 1; then
    createdb $PH -w -O bid_agent bid_agent || die "创建数据库 bid_agent 失败"
    ok "数据库 bid_agent 已创建（属主 bid_agent，端口 ${PG_PORT}）"
  else
    ok "数据库 bid_agent 已存在（端口 ${PG_PORT}）"
  fi

  # pgvector 扩展必须在 alembic 迁移（0004）之前就绪
  setup_pgvector "$PH"
}

setup_python() {
  # 沙盒中创建的 .venv 可能不完整（沙盒文件系统受限），校验后按需重建
  if [[ ! -x .venv/bin/python || ! -x .venv/bin/pip ]]; then
    log "重建虚拟环境 .venv ..."
    rm -rf .venv
    python3 -m venv .venv
  fi
  log "安装 Python 依赖（pip，需联网，约 1-2 分钟）..."
  .venv/bin/pip install --upgrade pip >/dev/null
  .venv/bin/pip install -r requirements-dev.txt
  ok "Python 依赖安装完成"
}

run_migration() {
  log "执行数据库迁移（alembic upgrade head）..."
  .venv/bin/alembic upgrade head
  ok "迁移完成，当前版本: $(.venv/bin/alembic current 2>/dev/null | tail -1)"
}

worker_is_running() {
  local pid state command
  [[ -f logs/worker.pid ]] || return 1
  pid="$(tr -d '[:space:]' < logs/worker.pid)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # kill -0 对 macOS 上尚未回收的僵尸进程也可能返回成功；同时校验状态
  # 和命令行，避免旧 PID 或被复用的 PID 阻止 worker 重启。
  state="$(ps -p "$pid" -o stat= 2>/dev/null | tr -d '[:space:]')"
  [[ -n "$state" && "$state" != Z* ]] || return 1
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ "$command" == *"runtime.worker"* ]]
}

# ── 本地 embedding 服务（BGE-M3，127.0.0.1:8001）整合 ─────────────────────────
# 背景（2026-09-09）：8001 embedding 由独立的 embedding_serve/start.sh 管理，不在
# setup_local_env.sh 管辖。若 8001 未起，runtime /readyz 的 knowledge.embedding 检查
# 报 Connection refused → readyz=false → 前端误判"API 不可达"。这里把 8001 纳入本脚本
# start/stop/status 一键管辖，避免"只能显示 127.0.0.1:8000 无法连接"的运维坑。
EMBED_DIR="$ROOT/embedding_serve"
EMBED_SCRIPT="$EMBED_DIR/start.sh"
EMBED_PORT=8001

embedding_is_running() {
  curl -s -m 2 "http://127.0.0.1:${EMBED_PORT}/health" >/dev/null 2>&1
}

embedding_start() {
  [[ -x "$EMBED_SCRIPT" ]] || { ok "跳过 embedding（无 $EMBED_SCRIPT，不影响 API/worker）"; return 0; }
  if [[ "${EMBED_SKIP:-0}" == "1" ]]; then
    ok "跳过 embedding（EMBED_SKIP=1 显式跳过）"
    return 0
  fi
  if embedding_is_running; then
    ok "embedding 已在运行（127.0.0.1:${EMBED_PORT}），跳过启动"
  else
    log "启动 embedding（$EMBED_DIR/start.sh start，127.0.0.1:${EMBED_PORT}）..."
    bash "$EMBED_SCRIPT" start >/dev/null 2>&1 \
      || die "embedding 启动失败（见 $EMBED_DIR/server.log）；请先 bash $EMBED_SCRIPT download-models 下载模型"
    ok "embedding 已启动（127.0.0.1:${EMBED_PORT}）"
  fi
}

embedding_stop() {
  [[ -x "$EMBED_SCRIPT" ]] || return 0
  if embedding_is_running; then
    bash "$EMBED_SCRIPT" stop >/dev/null 2>&1 || true
    ok "embedding 已停止"
  else
    ok "embedding 未在运行"
  fi
}

embedding_status() {
  if embedding_is_running; then
    echo "embedding: 运行中 (127.0.0.1:${EMBED_PORT})"
  else
    echo "embedding: 未运行"
  fi
}

start_services() {
  # 进程须使用项目 .venv 依赖：清除外部 PYTHONPATH（如 Hermes/CI 会话注入），
  # 否则 python3.14 会 import 到其他解释器版本的包（ABI 崩溃，API 起不来）。
  unset PYTHONPATH
  # 先拉起 embedding（8001）：供 runtime /readyz 的 knowledge.embedding 检查通过，
  # 否则 readyz=false → 前端误判"API 不可达"。
  embedding_start
  # API：以 8000 端口监听为准（pid 文件可能因启动失败残留而与实际进程不符）
  local api_pid
  api_pid="$(lsof -tiTCP:8000 -sTCP:LISTEN -P 2>/dev/null | head -1 || true)"
  if [[ -n "$api_pid" ]] && kill -0 "$api_pid" 2>/dev/null; then
    echo "$api_pid" > logs/api.pid
    ok "API 已在运行 (pid $api_pid)，跳过启动"
  else
    log "启动 API（uvicorn，127.0.0.1:8000）..."
    nohup .venv/bin/uvicorn runtime.api:app --host 127.0.0.1 --port 8000 > logs/api.log 2>&1 &
    echo $! > logs/api.pid
  fi
  if worker_is_running; then
    ok "worker 已在运行 (pid $(cat logs/worker.pid))，跳过启动"
  else
    log "启动 worker（python -m runtime.worker）..."
    nohup .venv/bin/python -m runtime.worker > logs/worker.log 2>&1 &
    echo $! > logs/worker.pid
  fi
  sleep 3
  if [[ -f logs/api.pid ]] && kill -0 "$(cat logs/api.pid)" 2>/dev/null; then
    ok "API 进程运行中 (pid $(cat logs/api.pid))"
  else
    die "API 启动失败，见 logs/api.log"
  fi
  if worker_is_running; then
    ok "worker 进程运行中 (pid $(cat logs/worker.pid))"
  else
    die "worker 启动失败，见 logs/worker.log"
  fi
}

stop_services() {
  # API：杀 8000 端口监听进程（pid 文件可能与实际进程脱节）
  local api_pid
  api_pid="$(lsof -tiTCP:8000 -sTCP:LISTEN -P 2>/dev/null | head -1 || true)"
  if [[ -n "$api_pid" ]]; then
    kill "$api_pid" 2>/dev/null || true
    # 等待端口释放（uvicorn 优雅关闭需要时间），避免 start 误判"已在运行"
    for _ in $(seq 1 10); do
      lsof -tiTCP:8000 -sTCP:LISTEN -P >/dev/null 2>&1 || break
      sleep 0.5
    done
  fi
  if worker_is_running; then
    local worker_pid
    worker_pid="$(tr -d '[:space:]' < logs/worker.pid)"
    kill "$worker_pid" 2>/dev/null || true
    # 等待优雅退出完成，避免紧接着 start 时旧 PID 仍被误认为存活。
    for _ in $(seq 1 20); do
      worker_is_running || break
      sleep 0.25
    done
  fi
  rm -f logs/api.pid logs/worker.pid
  embedding_stop
  ok "服务已停止"
}

health_check() {
  log "健康检查..."
  printf 'GET /healthz  -> '; curl -s -m 5 http://127.0.0.1:8000/healthz || echo "失败"
  printf '\nGET /readyz   -> '; curl -s -m 5 http://127.0.0.1:8000/readyz || echo "失败"
  echo
  ok "启动完成。日志: logs/api.log / logs/worker.log；数据: PostgreSQL(bid_agent) + runtime/objects"
}

status_check() {
  for f in logs/api.pid logs/worker.pid; do
    local running=1
    if [[ "$f" == logs/worker.pid ]]; then
      worker_is_running && running=0
    elif [[ -f "$f" ]] && kill -0 "$(cat "$f")" 2>/dev/null; then
      running=0
    fi
    if [[ "$running" -eq 0 ]]; then
      echo "$f: 运行中 (pid $(cat "$f"))"
    else
      echo "$f: 未运行"
    fi
  done
  embedding_status
  health_check || true
}

case "${1:-setup}" in
  start)  start_services; health_check ;;
  stop)   stop_services ;;
  status) status_check ;;
  setup|"")
    setup_postgres
    setup_python
    run_migration
    stop_services || true
    start_services
    health_check
    ;;
  *) die "未知参数: $1（支持: setup/start/stop/status）" ;;
esac
