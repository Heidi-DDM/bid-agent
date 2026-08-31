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
#   bash scripts/setup_local_env.sh start    # 仅启动 API + worker
#   bash scripts/setup_local_env.sh stop     # 停止 API + worker
#   bash scripts/setup_local_env.sh status   # 查看进程与健康检查
#
# 幂等：重复执行安全（已装依赖/已建库会自动跳过）。
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

start_services() {
  log "启动 API（uvicorn，127.0.0.1:8000）..."
  nohup .venv/bin/uvicorn runtime.api:app --host 127.0.0.1 --port 8000 > logs/api.log 2>&1 &
  echo $! > logs/api.pid
  log "启动 worker（python -m runtime.worker）..."
  nohup .venv/bin/python -m runtime.worker > logs/worker.log 2>&1 &
  echo $! > logs/worker.pid
  sleep 3
  if [[ -f logs/api.pid ]] && kill -0 "$(cat logs/api.pid)" 2>/dev/null; then
    ok "API 进程运行中 (pid $(cat logs/api.pid))"
  else
    die "API 启动失败，见 logs/api.log"
  fi
  if [[ -f logs/worker.pid ]] && kill -0 "$(cat logs/worker.pid)" 2>/dev/null; then
    ok "worker 进程运行中 (pid $(cat logs/worker.pid))"
  else
    die "worker 启动失败，见 logs/worker.log"
  fi
}

stop_services() {
  for f in logs/api.pid logs/worker.pid; do
    if [[ -f "$f" ]]; then
      kill "$(cat "$f")" 2>/dev/null || true
      rm -f "$f"
    fi
  done
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
    if [[ -f "$f" ]] && kill -0 "$(cat "$f")" 2>/dev/null; then
      echo "$f: 运行中 (pid $(cat "$f"))"
    else
      echo "$f: 未运行"
    fi
  done
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