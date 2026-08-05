#!/usr/bin/env bash
# panji-verify-deploy.sh — 在 panji-prod 的 /root/web_dev_verify 内部署 V2.1 验证栈（DS-111）。
#
# [Corrective Pass 2 P0-8] 这是**远程实现脚本**，由本地控制脚本 scripts/ops/panji-verify-deploy
# 通过 panji-prod-ssh 调用。本脚本内部**不得再调用 panji-prod-ssh**（已在远程，避免自环）。
# 必须在 /root/web_dev_verify（验证代码目录，已 checkout 目标 SHA）内执行。
#
# 前置（由本地控制脚本保证）：
#   - 当前目录为 /root/web_dev_verify 且已 checkout 目标 SHA
#   - bz_stock_verify_<sha> 已创建（create_verify_database.sh）
#   - Migration 已 apply 到验证库（alembic upgrade head）
#
# 用法：scripts/deploy/panji-verify-deploy.sh <SHA> [env_file]
#   env_file 默认 market.verify.env（位于 /root/web_dev_verify 根）
#
# fail-closed：
#   - 校验 DATABASE_URL 不含 bz_stock（必须 bz_stock_verify_<sha>）
#   - 校验 APP_ENV=verification
#   - 校验运行时 SHA == 目标 SHA（部署后查 /v1/version.runtime_git_sha）
#   - 校验 /v1/health 与 /v1/health/ready 探针（P0-9 修正既有版本合同）
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

# 必须在验证代码目录内执行（P0-8）
if [ ! -f "docker-compose.verify.yml" ] || [ ! -d "backend/alembic" ]; then
  echo "panji-verify-deploy: 必须在 /root/web_dev_verify 内执行（缺少 docker-compose.verify.yml 或 backend/alembic）" >&2
  exit 8
fi

# 校验 env 文件
if [ ! -f "$ENV_FILE" ]; then
  echo "panji-verify-deploy: 环境文件 $ENV_FILE 不存在" >&2
  exit 5
fi
if grep -q "bz_stock" "$ENV_FILE" && ! grep -q "bz_stock_verify" "$ENV_FILE"; then
  echo "panji-verify-deploy: 环境文件 DATABASE_URL 指向 bz_stock，拒绝（必须 bz_stock_verify_<sha>）" >&2
  exit 6
fi
if ! grep -q "APP_ENV=verification" "$ENV_FILE"; then
  echo "panji-verify-deploy: 环境文件 APP_ENV 非 verification，拒绝" >&2
  exit 7
fi

# 先静态验证 compose 配置
docker compose -p panji-verify --env-file "$ENV_FILE" -f docker-compose.verify.yml config >/dev/null \
  || { echo "panji-verify-deploy: compose 配置静态验证失败" >&2; exit 9; }

# 启动验证栈（独立 project，不触碰正式栈）
docker compose -p panji-verify --env-file "$ENV_FILE" -f docker-compose.verify.yml up -d --build

# 运行时 SHA 校验（P0-9：使用既有版本合同 /v1/version.runtime_git_sha）
RUNTIME_SHA=$(docker compose -p panji-verify -f docker-compose.verify.yml exec -T verify-backend \
  curl -sf http://127.0.0.1:8000/v1/version.runtime_git_sha | python3 -c "import sys,json;print(json.load(sys.stdin).get('runtime_git_sha',''))" 2>/dev/null || echo "")
echo "panji-verify-deploy: 目标SHA=$SHA, 运行时SHA=$RUNTIME_SHA"
if [ "$RUNTIME_SHA" != "$SHA" ]; then
  echo "panji-verify-deploy: 运行时 SHA 不匹配（期望 $SHA，实际 $RUNTIME_SHA），部署异常" >&2
  exit 10
fi

# 健康检查（P0-9：/v1/health 与 /v1/health/ready）
docker compose -p panji-verify -f docker-compose.verify.yml exec -T verify-backend \
  curl -sf http://127.0.0.1:8000/v1/health >/dev/null || { echo "panji-verify-deploy: /v1/health 失败" >&2; exit 11; }
docker compose -p panji-verify -f docker-compose.verify.yml exec -T verify-backend \
  curl -sf http://127.0.0.1:8000/v1/health/ready >/dev/null || { echo "panji-verify-deploy: /v1/health/ready 失败" >&2; exit 12; }

echo "panji-verify-deploy: 部署完成 (DS-111). 访问经 SSH Tunnel: ssh -N -L 8080:127.0.0.1:8080 panji-prod"
