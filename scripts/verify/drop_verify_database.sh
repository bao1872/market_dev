#!/usr/bin/env bash
# drop_verify_database.sh — 验收完成后删除远程验证库（DS-110）。
#
# 仅删除 bz_stock_verify_<sha>；禁止删除 bz_stock 或任何其他库。
# fail-closed：库名必须匹配 bz_stock_verify_<7-40位SHA>；禁止连接 bz_stock。
#
# 用法：scripts/verify/drop_verify_database.sh <SHA>
# 退出码：0=删除成功；非0=拒绝/失败

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <SHA>" >&2
  exit 2
fi

SHA="$1"
DB_NAME="bz_stock_verify_${SHA}"

if ! echo "$DB_NAME" | grep -Eq '^bz_stock_verify_[0-9a-f]{7,40}$'; then
  echo "drop_verify_database: 非法库名 '$DB_NAME'" >&2
  exit 3
fi

POSTGRES_CONTAINER="trading-postgres"
PG_USER="bz"

# 确认存在
EXISTS=$(scripts/ops/panji-prod-ssh "docker exec $POSTGRES_CONTAINER psql -U $PG_USER -d postgres -tAc \"SELECT 1 FROM pg_database WHERE datname='$DB_NAME'\"" || true)
if [ "$EXISTS" != "1" ]; then
  echo "drop_verify_database: 验证库 '$DB_NAME' 不存在，无需删除" >&2
  exit 0
fi

# 终止连接后删除（不删 bz_stock）
scripts/ops/panji-prod-ssh "docker exec $POSTGRES_CONTAINER psql -U $PG_USER -d postgres -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DB_NAME' AND pid <> pg_backend_pid()\""
scripts/ops/panji-prod-ssh "docker exec $POSTGRES_CONTAINER psql -U $PG_USER -d postgres -c \"DROP DATABASE $DB_NAME\""

echo "drop_verify_database: 验证库已删除: $DB_NAME"
