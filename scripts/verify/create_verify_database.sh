#!/usr/bin/env bash
# create_verify_database.sh — 在 panji-prod 的现有 PostgreSQL 容器内创建远程验证库。
#
# [rules/80 DS-110] 唯一允许的临时数据库：bz_stock_verify_<40位SHA>。
# 不新建 PostgreSQL 容器或 Volume；不触碰 bz_stock。
#
# 用法：scripts/verify/create_verify_database.sh <SHA>
#   SHA 必须是待验收的 origin/dev 完整 40 位 commit。
#
# fail-closed：
#   - SHA 不符合 bz_stock_verify_<7-40位SHA> 命名 → 拒绝
#   - 目标库已存在 → 拒绝（避免误覆盖，需先 drop）
#   - 任何指向 bz_stock 的操作 → 拒绝
#
# 退出码：0=创建成功；非0=拒绝/失败

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <SHA>" >&2
  exit 2
fi

SHA="$1"
DB_NAME="bz_stock_verify_${SHA}"

# 命名校验（DS-110）
if ! echo "$DB_NAME" | grep -Eq '^bz_stock_verify_[0-9a-f]{40}$'; then
  echo "create_verify_database: 非法库名 '$DB_NAME'（必须 bz_stock_verify_<40位SHA>）" >&2
  exit 3
fi

# 复用正式 SSH 入口与 postgres 容器名（来自 docker-compose.prod.yml）
POSTGRES_CONTAINER="trading-postgres"
PG_USER="bz"

# 通过 panji-prod-ssh 在容器内执行（禁止连接 bz_stock，仅建验证库）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# 校验目标库是否已存在（fail-closed：已存在则拒绝）
EXISTS=$(scripts/ops/panji-prod-ssh "docker exec $POSTGRES_CONTAINER psql -U $PG_USER -d postgres -tAc \"SELECT 1 FROM pg_database WHERE datname='$DB_NAME'\"" || true)
if [ "$EXISTS" = "1" ]; then
  echo "create_verify_database: 验证库 '$DB_NAME' 已存在，拒绝重复创建（如需重建请先 drop_verify_database.sh）" >&2
  exit 4
fi

# 创建验证库（明确指定 encoding/collation 与 bz_stock 一致）
scripts/ops/panji-prod-ssh "docker exec $POSTGRES_CONTAINER psql -U $PG_USER -d postgres -c \"CREATE DATABASE $DB_NAME WITH OWNER $PG_USER ENCODING='UTF8' LC_COLLATE='C' LC_CTYPE='C' TEMPLATE template0\""

# 连接校验：确认 current_database() == 验证库，且不等于 bz_stock
VERIFY=$(scripts/ops/panji-prod-ssh "docker exec $POSTGRES_CONTAINER psql -U $PG_USER -d $DB_NAME -tAc 'SELECT current_database()'")
if [ "$VERIFY" != "$DB_NAME" ]; then
  echo "create_verify_database: 连接校验失败，current_database='$VERIFY' 期望 '$DB_NAME'" >&2
  exit 5
fi
if [ "$VERIFY" = "bz_stock" ]; then
  echo "create_verify_database: 严重错误——连接到了 bz_stock，立即中止" >&2
  exit 6
fi

echo "create_verify_database: 验证库已创建并校验通过: $DB_NAME"
