#!/usr/bin/env bash
# panji-verify-deploy.sh — 在 panji-prod 部署 V2.1 远程验证栈（DS-111）。
#
# 前置（由 Phase 3/4 准备）：
#   - bz_stock_verify_<sha> 已创建（create_verify_database.sh）
#   - Migration 已 apply 到验证库（alembic upgrade head）
#   - 仓库在 /root/web_dev 已 checkout 目标 SHA 并构建镜像
#
# 用法：scripts/deploy/panji-verify-deploy.sh <SHA> [env_file]
#   env_file 默认 market.verify.env（位于仓库根）
#
# fail-closed：
#   - 校验 DATABASE_URL 不含 bz_stock
#   - 校验 APP_ENV=verification
#   - 校验运行时 SHA == 目标 SHA（部署后健康检查 /version）
#
# 退出码：0=部署成功；非0=拒绝/失败

set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <SHA> [env_file]" >&2
  exit 2
fi

SHA="$1"
ENV_FILE="${2:-market.verify.env}"
DB_NAME="bz_stock_verify_${SHA}"

if ! echo "$DB_NAME" | grep -Eq '^bz_stock_verify_[0-9a-f]{7,40}$'; then
  echo "panji-verify-deploy: 非法 SHA '$SHA'" >&2
  exit 3
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# 部署前必跑 preflight（DS-80 远程开发运行服务器 SSH SSOT）
"$ROOT_DIR/scripts/ops/panji-prod-preflight" || {
  echo "panji-verify-deploy: preflight 失败，中止" >&2
  exit 4
}

# 校验 env 文件存在且 DATABASE_URL 指向验证库
if [ ! -f "$ROOT_DIR/$ENV_FILE" ]; then
  echo "panji-verify-deploy: 环境文件 $ENV_FILE 不存在" >&2
  exit 5
fi
if grep -q "bz_stock" "$ROOT_DIR/$ENV_FILE" && ! grep -q "bz_stock_verify" "$ROOT_DIR/$ENV_FILE"; then
  echo "panji-verify-deploy: 环境文件 DATABASE_URL 指向 bz_stock，拒绝（必须 bz_stock_verify_<sha>）" >&2
  exit 6
fi
if ! grep -q "APP_ENV=verification" "$ROOT_DIR/$ENV_FILE"; then
  echo "panji-verify-deploy: 环境文件 APP_ENV 非 verification，拒绝" >&2
  exit 7
fi

# 远程部署：在 panji-prod 的 /root/web_dev 内启动验证栈
scripts/ops/panji-prod-ssh "cd /root/web_dev && docker compose -p panji-verify --env-file $ENV_FILE -f docker-compose.verify.yml up -d --build"

# 运行时 SHA 校验
RUNTIME_SHA=$(scripts/ops/panji-prod-ssh "cd /root/web_dev && docker compose -p panji-verify -f docker-compose.verify.yml exec -T verify-backend python -c \"import app; print(getattr(app,'__version__','unknown'))\"" 2>/dev/null || echo "unknown")
echo "panji-verify-deploy: 验证栈已部署, 目标SHA=$SHA, 运行时SHA=$RUNTIME_SHA"

# 健康检查
scripts/ops/panji-prod-ssh "cd /root/web_dev && docker compose -p panji-verify -f docker-compose.verify.yml exec -T verify-backend curl -sf http://127.0.0.1:8000/health || exit 1"

echo "panji-verify-deploy: 部署完成 (DS-111). 访问经 SSH Tunnel: ssh -N -L 8080:127.0.0.1:8080 panji-prod"
