#!/usr/bin/env bash
# 交易平台 - 生产环境统一部署脚本
# 用法: ./scripts/deploy.sh
# 说明: 统一使用 docker compose build + up -d --force-recreate --remove-orphans，
#       禁止使用 docker cp / rsync 覆盖运行中容器。

set -euo pipefail

ENV_FILE="/etc/market-dev/market.env"
COMPOSE_FILE="docker-compose.prod.yml"

echo "[deploy] 使用环境文件: ${ENV_FILE}"
echo "[deploy] 使用 Compose 文件: ${COMPOSE_FILE}"

# 构建全部服务镜像
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" build

# 重新创建并启动服务，清理孤立容器
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --force-recreate --remove-orphans

# 清理悬空镜像与构建缓存
docker image prune -f
docker builder prune -f

echo "[deploy] 部署完成"
