#!/usr/bin/env bash
# 交易平台 - 生产环境统一部署脚本
# 用法: ./scripts/deploy.sh
# 说明: 仅构建 backend/frontend/worker-capture，其它 Python Worker 共享 backend 镜像，
#       禁止日常使用 docker image prune / docker builder prune 清理缓存。

set -euo pipefail

ENV_FILE="/etc/market-dev/market.env"
COMPOSE_FILE="docker-compose.prod.yml"

# 若未外部传入，则自动从当前仓库与 UTC 时间生成版本信息
GIT_SHA="${GIT_SHA:-$(git rev-parse --short HEAD)}"
BUILD_TIME="${BUILD_TIME:-$(date -u +"%Y-%m-%dT%H:%M:%SZ")}"
export GIT_SHA BUILD_TIME

echo "[deploy] 使用环境文件: ${ENV_FILE}"
echo "[deploy] 使用 Compose 文件: ${COMPOSE_FILE}"
echo "[deploy] GIT_SHA=${GIT_SHA}, BUILD_TIME=${BUILD_TIME}"

# 部署前输出磁盘与镜像状态，便于对比清理前后变化
echo "=== 部署前磁盘与镜像状态 ==="
docker system df -v
docker images

# [deploy] - 描述: 清理本地 TypeScript 增量缓存，确保 Docker 构建从零开始全量类型检查
# 根因: tsbuildinfo 缓存会导致本地构建"假通过"而 Docker 构建失败
find frontend -name "*.tsbuildinfo" -not -path "*/node_modules/*" -delete 2>/dev/null || true

# 构建必要的服务镜像（backend 镜像供所有 Python Worker 复用）
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" build backend frontend worker-capture

# 使用已构建镜像重新创建并启动所有服务，清理孤立容器
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --no-build --force-recreate --remove-orphans

# 部署后输出磁盘与镜像状态，确认新镜像已生成、旧镜像保留情况
echo "=== 部署后磁盘与镜像状态 ==="
docker system df -v
docker images

# 日常不再清理 Docker 镜像/构建缓存；每周或磁盘不足时执行 scripts/cleanup-docker.sh

echo "[deploy] 部署完成"
