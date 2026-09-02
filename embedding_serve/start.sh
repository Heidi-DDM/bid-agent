#!/usr/bin/env bash
# 本地 embedding 服务管理（bge-m3，OpenAI 兼容 /v1/embeddings，127.0.0.1:8001）
# 用法: bash start.sh {setup|start|stop|status}
# 说明: 服务只监听 127.0.0.1（企业向量化不出内网，F025 §5）；
#       模型默认取 HF 标准缓存中的 BAAI/bge-m3（首次约 2.3GB，需联网下载）。
set -euo pipefail
cd "$(dirname "$0")"
# 服务必须使用本 venv 的依赖：清除外部 PYTHONPATH（如 Hermes 会话注入），
# 否则 python3.14 会 import 到其他解释器版本的包（ABI 崩溃）。
unset PYTHONPATH

VENV=.venv
PORT="${EMBED_PORT:-8001}"
BASE="http://127.0.0.1:${PORT}"
# 默认模型目录 = 本机 HF 标准缓存中的 BAAI/bge-m3 snapshot（与 server.py 默认一致）
DEFAULT_MODEL_PATH="$HOME/.cache/huggingface/hub/models--BAAI--bge-m3/snapshots/5617a9f61b028005a4858fdac845db406aefb181"
MODEL_DIR="${BGE_M3_PATH:-$DEFAULT_MODEL_PATH}"

check_model() {
  if [[ ! -d "$MODEL_DIR" ]]; then
    echo "[embed][err] 模型目录不存在: $MODEL_DIR" >&2
    echo "[embed][err] 请先下载模型（约 2.3GB，需联网）：" >&2
    echo "  hf download BAAI/bge-m3 --include pytorch_model.bin --include '*.json' --include sentencepiece.bpe.model --include '1_Pooling/*'" >&2
    echo "[embed][err] 或通过环境变量指定已有模型目录：export BGE_M3_PATH=<模型目录> 后重试" >&2
    exit 1
  fi
}

log() { printf '\033[1;34m[embed]\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m[embed]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[embed]\033[0m %s\n' "$*" >&2; exit 1; }

setup() {
  if [[ ! -x "$VENV/bin/python" ]]; then
    log "创建虚拟环境 $VENV ..."
    PY="$(command -v python3.14 || command -v python3 || echo python3)"
    "$PY" -m venv "$VENV"
  fi
  log "安装依赖（sentence-transformers/fastapi/uvicorn，需联网，约 2-5 分钟）..."
  "$VENV/bin/pip" install --upgrade pip >/dev/null
  "$VENV/bin/pip" install -q sentence-transformers "fastapi" "uvicorn[standard]"
  ok "依赖安装完成"
}

start() {
  check_model
  if lsof -iTCP:"$PORT" -sTCP:LISTEN -P >/dev/null 2>&1; then
    if curl -s -m 2 "$BASE/health" >/dev/null 2>&1; then
      ok "已在运行（端口 ${PORT}）"
      status
      return
    fi
    # 端口被占但 /health 不可达：旧进程正在优雅关闭/僵死 → 清理后重新启动，
    # 避免 stop 刚 kill 完、start 紧跟执行时误判"已在运行"而漏启动。
    log "端口 ${PORT} 被占用但 /health 不可达，清理残留进程后重启 ..."
    stop >/dev/null 2>&1 || true
    sleep 1
  fi
  [[ -x "$VENV/bin/uvicorn" ]] || setup
  log "启动（uvicorn 127.0.0.1:${PORT}），模型加载约 30-90 秒 ..."
  nohup "$VENV/bin/python" -m uvicorn server:app --host 127.0.0.1 --port "$PORT" > server.log 2>&1 &
  echo $! > server.pid
  for _ in $(seq 1 150); do
    if curl -s -m 2 "$BASE/health" >/dev/null 2>&1; then
      ok "就绪: $BASE/health"
      curl -s "$BASE/health"; echo
      return
    fi
    sleep 1
  done
  die "启动超时，日志尾部：$(tail -20 server.log 2>/dev/null || true)"
}

stop() {
  if [[ -f server.pid ]] && kill -0 "$(cat server.pid)" 2>/dev/null; then
    kill "$(cat server.pid)"
    rm -f server.pid
    ok "已停止"
  else
    # 兜底：按端口找
    local p
    p="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN -P 2>/dev/null | head -1 || true)"
    if [[ -n "$p" ]]; then kill "$p"; ok "已停止（pid ${p}）"; else ok "未在运行"; fi
  fi
}

status() {
  if curl -s -m 2 "$BASE/health" >/dev/null 2>&1; then
    ok "运行中: $(curl -s "$BASE/health")"
  else
    ok "未运行"
  fi
}

case "${1:-status}" in
  setup)  setup ;;
  start)  start ;;
  stop)   stop ;;
  status) status ;;
  *) die "未知参数: $1（支持: setup/start/stop/status）" ;;
esac
