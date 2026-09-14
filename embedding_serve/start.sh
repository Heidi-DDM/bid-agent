#!/usr/bin/env bash
# 完全本地 RAG 模型服务管理：BGE-M3 embedding + BGE reranker（127.0.0.1:8001）。
# 用法: bash start.sh {setup|download-models|start|stop|status}
# 说明: 企业资料不会离开本机/内网。模型下载必须显式调用 download-models，
#       首次下载需要联网且会占用数 GB 磁盘；start/请求路径绝不隐式下载。
set -euo pipefail
cd "$(dirname "$0")"
unset PYTHONPATH

VENV=.venv
PORT="${EMBED_PORT:-8001}"
BASE="http://127.0.0.1:${PORT}"
EMBED_CACHE_ROOT="$HOME/.cache/huggingface/hub/models--BAAI--bge-m3/snapshots"
RERANK_CACHE_ROOT="$HOME/.cache/huggingface/hub/models--BAAI--bge-reranker-v2-m3/snapshots"
MODEL_DIR="${BGE_M3_PATH:-}"
RERANKER_DIR="${BGE_RERANKER_PATH:-}"

# runtime/.env 是普通 KEY=VALUE 文件；只读取此开关，不 source 其中密钥。
if [[ -z "${RERANKER_ENABLED+x}" && -f ../runtime/.env ]]; then
  RERANKER_ENABLED="$(sed -nE 's/^RERANKER_ENABLED[[:space:]]*=[[:space:]]*([^#[:space:]]+).*/\1/p' ../runtime/.env | tail -1 || true)"
fi
RERANKER_ENABLED="${RERANKER_ENABLED:-false}"
RERANKER_ENABLED_NORMALIZED="$(printf '%s' "$RERANKER_ENABLED" | tr '[:upper:]' '[:lower:]')"

log() { printf '\033[1;34m[local-rag]\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m[local-rag]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[local-rag]\033[0m %s\n' "$*" >&2; exit 1; }

first_snapshot() {
  local cache_root="$1"
  [[ -d "$cache_root" ]] || return 1
  find "$cache_root" -mindepth 1 -maxdepth 1 -type d -print -quit 2>/dev/null
}

embedding_asset() {
  [[ -n "$MODEL_DIR" && -d "$MODEL_DIR" ]] && { printf '%s\n' "$MODEL_DIR"; return 0; }
  first_snapshot "$EMBED_CACHE_ROOT"
}

reranker_asset() {
  [[ -n "$RERANKER_DIR" && -d "$RERANKER_DIR" ]] && { printf '%s\n' "$RERANKER_DIR"; return 0; }
  first_snapshot "$RERANK_CACHE_ROOT"
}

check_assets() {
  embedding_asset >/dev/null || die "缺少 BAAI/bge-m3；请先执行：bash start.sh download-models"
  if [[ "$RERANKER_ENABLED_NORMALIZED" =~ ^(1|true|yes|on)$ ]]; then
    reranker_asset >/dev/null || die "RERANKER_ENABLED=true 但缺少 BAAI/bge-reranker-v2-m3；请先执行：bash start.sh download-models"
  fi
}

setup() {
  if [[ ! -x "$VENV/bin/python" ]]; then
    log "创建模型服务虚拟环境 $VENV ..."
    PY="$(command -v python3.14 || command -v python3 || echo python3)"
    "$PY" -m venv "$VENV"
  fi
  log "安装本地模型服务依赖（torch/sentence-transformers/fastapi，需联网）..."
  "$VENV/bin/pip" install --upgrade pip >/dev/null
  "$VENV/bin/pip" install -q 'torch>=2.4,<3' 'sentence-transformers>=3,<6' 'fastapi>=0.115,<1' 'uvicorn[standard]>=0.30,<1'
  ok "依赖安装完成"
}

download_models() {
  [[ -x "$VENV/bin/python" ]] || setup
  log "下载 BAAI/bge-m3 与 BAAI/bge-reranker-v2-m3 到本机 Hugging Face 缓存（需联网，数 GB）..."
  log "可用 HF_ENDPOINT=https://hf-mirror.com 走镜像；HF 直连与镜像均失败时自动回退 ModelScope（modelscope.cn）"
  "$VENV/bin/python" - <<'PY'
import sys
from pathlib import Path
from huggingface_hub import snapshot_download

REPOS = ("BAAI/bge-m3", "BAAI/bge-reranker-v2-m3")
# ModelScope 官方镜像文件清单（两个 repo 的必需权重/tokenizer/config）
MS_FILES = {
    "BAAI/bge-m3": [
        "config.json", "config_sentence_transformers.json", "modules.json",
        "sentence_bert_config.json", "special_tokens_map.json",
        "tokenizer.json", "tokenizer_config.json", "sentencepiece.bpe.model",
        "pytorch_model.bin",
        "1_Pooling/config.json",
    ],
    "BAAI/bge-reranker-v2-m3": [
        "config.json", "special_tokens_map.json", "tokenizer.json",
        "tokenizer_config.json", "sentencepiece.bpe.model", "model.safetensors",
    ],
}


def download_from_modelscope(repo: str) -> None:
    """HF 直连/镜像均不可达时的回退：从 ModelScope resolve 端点下载到标准 HF 缓存快照目录。"""
    import urllib.request

    snap = Path.home() / ".cache" / "huggingface" / "hub" / f"models--{repo.replace('/', '--')}" / "snapshots" / "main"
    snap.mkdir(parents=True, exist_ok=True)
    base = f"https://modelscope.cn/models/{repo}/resolve/master"
    for name in MS_FILES[repo]:
        dest = snap / name
        if dest.exists() and dest.stat().st_size > 0:
            print(f"  [modelscope] 已存在，跳过 {name}", flush=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"  [modelscope] 下载 {name} ...", flush=True)
        urllib.request.urlretrieve(f"{base}/{name}", dest)


for repo in REPOS:
    print(f"[local-rag] downloading {repo} ...", flush=True)
    try:
        snapshot_download(repo_id=repo)
    except Exception as exc:
        print(f"[local-rag] HF 下载失败（{type(exc).__name__}: {exc}），回退 ModelScope ...", flush=True)
        download_from_modelscope(repo)
PY
  ok "模型下载完成；可执行 bash start.sh start"
}

start() {
  check_assets
  if lsof -iTCP:"$PORT" -sTCP:LISTEN -P >/dev/null 2>&1; then
    if curl -s -m 2 "$BASE/health" >/dev/null 2>&1; then
      ok "已在运行（端口 ${PORT}）"
      status
      return
    fi
    log "端口 ${PORT} 被占用但 /health 不可达，清理残留进程后重启 ..."
    stop >/dev/null 2>&1 || true
    sleep 1
  fi
  [[ -x "$VENV/bin/uvicorn" ]] || setup
  log "启动（uvicorn 127.0.0.1:${PORT}）；模型首次请求时才加载 ..."
  nohup "$VENV/bin/python" -m uvicorn server:app --host 127.0.0.1 --port "$PORT" > server.log 2>&1 &
  echo $! > server.pid
  for _ in $(seq 1 30); do
    if curl -s -m 2 "$BASE/health" >/dev/null 2>&1; then
      ok "服务已就绪: $BASE/health"
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
    local pid
    pid="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN -P 2>/dev/null | head -1 || true)"
    if [[ -n "$pid" ]]; then kill "$pid"; ok "已停止（pid ${pid}）"; else ok "未在运行"; fi
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
  setup)           setup ;;
  download-models) download_models ;;
  start)           start ;;
  stop)            stop ;;
  status)          status ;;
  *) die "未知参数: $1（支持: setup/download-models/start/stop/status）" ;;
esac
