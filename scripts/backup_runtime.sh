#!/usr/bin/env bash
# R019/F019 §5：备份与恢复（数据库 + 对象存储清单成对保存）
# 用法：
#   bash scripts/backup_runtime.sh [备份目录]     # 默认 runtime/backups/YYYYmmdd-HHMMSS
# 恢复（人工执行，需数据库可写）：
#   pg_restore -U bid_agent -d bid_agent --clean --if-exists <备份>/db.dump
#   # 对象存储：将 <备份>/objects.manifest 所列文件从对象根目录恢复（清单含 sha256 可校验）
# 说明：
#   - 备份文件沿用私有资料权限，保留期限由法务确认（F019 §5.4）；
#   - 恢复后须执行 runtime/db 哈希复核（verify_material）与黄金样本回归（F019 §5.2）。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_ROOT="${1:-$PROJECT_DIR/runtime/backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_ROOT/$STAMP"

# 环境变量：DATABASE_URL / OBJECT_STORE_ROOT 由 .env 或环境注入（复用 runtime/core/config.py 的字段名）
: "${DATABASE_URL:?需要 DATABASE_URL（postgresql+psycopg://bid_agent:...@127.0.0.1:5432/bid_agent）}"
: "${OBJECT_STORE_ROOT:?需要 OBJECT_STORE_ROOT}"

mkdir -p "$DEST"

# 从 URL 提取 psql 连接参数（postgresql+psycopg://user:pass@host:port/db）
URL="${DATABASE_URL#*://}"
USER_PASS="${URL%%@*}"
HOST_PORT="${URL#*@}"
HOST="${HOST_PORT%%:*}"
PORT="${HOST_PORT#*:}"
PORT="${PORT%%/*}"
DB_NAME="${HOST_PORT##*/}"
PG_USER="${USER_PASS%%:*}"
PG_PASS="${USER_PASS#*:}"
export PGPASSWORD="$PG_PASS"

echo "[backup] 数据库 $DB_NAME@$HOST:$PORT -> $DEST/db.dump"
pg_dump -h "$HOST" -p "$PORT" -U "$PG_USER" -d "$DB_NAME" -Fc -f "$DEST/db.dump"

echo "[backup] 对象存储清单 -> $DEST/objects.manifest"
(cd "$OBJECT_STORE_ROOT" && find . -type f | sort) > "$DEST/objects.manifest"

echo "[backup] 完成: $DEST（清单 $(wc -l < "$DEST/objects.manifest") 个对象）"
echo "[backup] 注意：备份文件属私有资料，请按 F019 §5.4 控制访问与保留期"