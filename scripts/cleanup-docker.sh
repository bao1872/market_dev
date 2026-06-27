#!/usr/bin/env bash
# 交易平台 - Docker 每周/按需清理脚本
# 用法: ./scripts/cleanup-docker.sh
# 说明: 仅清理超过 7 天的悬空镜像与构建缓存，保留近期缓存用于下次构建加速。

set -euo pipefail

DAYS=7
HOURS=$((DAYS * 24))

echo "[cleanup] 清理超过 ${DAYS} 天（${HOURS} 小时）的 Docker 悬空资源..."

# 清理超过 7 天的悬空镜像，避免磁盘堆积
docker image prune -f --filter "until=${HOURS}h"

# 清理超过 7 天的构建缓存，保留近期缓存
docker builder prune -f --filter "until=${HOURS}h"

echo "[cleanup] 清理完成"
