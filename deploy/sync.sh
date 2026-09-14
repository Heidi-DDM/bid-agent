#!/usr/bin/env bash
# 一键同步更新到阿里云演示环境（docs/11-部署方案-阿里云演示环境.md §7）
#
# 用法（本机仓库根目录或任意位置）：
#   bash deploy/sync.sh              # 同步代码 → 构建镜像 → 迁移 → 重启 → 健康检查
#   bash deploy/sync.sh --no-build   # 跳过构建（仅改了 prototype/ 前端或 nginx 配置时）
#
# 首次部署前置（只需一次）：
#   1. 服务器上创建 ~/bid-agent/deploy/.env（复制 app/deploy/.env.example 填写，chmod 600）
#   2. 完成种子数据导入（docs/11 §6；脚本 deploy/seed/export_demo_enterprise.py 在本机执行）
set -euo pipefail

SERVER="${SERVER:-qykj_lqf@8.163.107.248}"
REMOTE_ROOT="${REMOTE_ROOT:-/home/qykj_lqf/bid-agent}"
NO_BUILD=0
[[ "${1:-}" == "--no-build" ]] && NO_BUILD=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "==> 同步代码 ${ROOT} → ${SERVER}:${REMOTE_ROOT}/app/"
rsync -az --delete --exclude-from="${ROOT}/deploy/rsync-exclude.txt" \
    --rsync-path="mkdir -p ${REMOTE_ROOT} && rsync" \
    "${ROOT}/" "${SERVER}:${REMOTE_ROOT}/app/"

echo "==> 服务器端构建/重启（NO_BUILD=${NO_BUILD}）"
ssh "$SERVER" "NO_BUILD=${NO_BUILD} REMOTE_ROOT=${REMOTE_ROOT} bash -s" <<'REMOTE'
set -euo pipefail
cd "${REMOTE_ROOT}"
if [[ ! -f deploy/.env ]]; then
  echo "!! 缺少 ~/bid-agent/deploy/.env —— 复制 app/deploy/.env.example 填写后重试" >&2
  exit 1
fi
mkdir -p data/objects backups
sudo chown -R 10001:10001 data/objects

cd "${REMOTE_ROOT}/app"
COMPOSE=(sudo docker compose -f deploy/docker-compose.yml --env-file ../deploy/.env)

if [[ "$NO_BUILD" != "1" ]]; then
  "${COMPOSE[@]}" build
fi
# migrate 一次性容器先跑（alembic upgrade head），成功后 api/worker 才启动
"${COMPOSE[@]}" up -d --remove-orphans
sleep 3
"${COMPOSE[@]}" ps
echo "==> 健康检查"
for i in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:$(grep -E '^PUBLIC_PORT=' ../deploy/.env | cut -d= -f2 || echo 2000)/healthz" >/dev/null 2>&1; then
    echo "healthz OK"
    exit 0
  fi
  sleep 3
done
echo "!! healthz 20 次探测失败，查看日志：" >&2
"${COMPOSE[@]}" logs --tail=50 api migrate
exit 1
REMOTE
