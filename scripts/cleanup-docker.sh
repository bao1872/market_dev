#!/usr/bin/env bash
# 交易平台 - Docker 每周/按需清理脚本
# 用法: ./scripts/cleanup-docker.sh
# 说明: 
#   1. 清理超过 7 天的悬空镜像与构建缓存
#   2. 删除旧的 market-dev-backend/frontend/capture SHA 标签，仅保留当前和最近 3 个版本
#   3. 禁止 pull 本地构建镜像，禁止日常 prune

set -euo pipefail

DAYS=7
HOURS=$((DAYS * 24))
KEEP_VERSIONS=3

echo "[cleanup] 清理超过 ${DAYS} 天（${HOURS} 小时）的 Docker 悬空资源..."

# 1. 清理超过 7 天的悬空镜像，避免磁盘堆积
docker image prune -f --filter "until=${HOURS}h"

# 2. 清理超过 7 天的构建缓存，保留近期缓存
docker builder prune -f --filter "until=${HOURS}h"

# 3. 删除旧的 market-dev-* SHA 标签，仅保留当前和最近 ${KEEP_VERSIONS} 个版本
echo "[cleanup] 清理旧版本镜像标签，保留最近 ${KEEP_VERSIONS} 个版本..."

for REPO in market-dev-backend market-dev-frontend market-dev-capture; do
    echo "[cleanup] 处理 ${REPO}..."
    # 列出所有 SHA 标签（排除 latest），按创建时间倒序排序
    TAGS=$(docker images --format "{{.Tag}}\t{{.CreatedAt}}" "${REPO}" 2>/dev/null | grep -v "latest" | grep -v "<none>" | sort -t$'\t' -k2 -r | awk -F'\t' '{print $1}')
    if [ -z "$TAGS" ]; then
        echo "[cleanup]   ${REPO}: 无 SHA 标签可清理"
        continue
    fi
    # 保留前 KEEP_VERSIONS 个（最新），删除其余
    COUNT=0
    for TAG in $TAGS; do
        COUNT=$((COUNT + 1))
        if [ $COUNT -le $KEEP_VERSIONS ]; then
            echo "[cleanup]   ${REPO}:${TAG} 保留（第 ${COUNT} 个）"
        else
            echo "[cleanup]   ${REPO}:${TAG} 删除（第 ${COUNT} 个）"
            # 不静默吞错：输出失败原因，但不中断脚本（镜像可能正被使用中）
            if ! docker rmi "${REPO}:${TAG}" 2>&1 | sed 's/^/[cleanup]     /'; then
                echo "[cleanup]     删除失败（可能正被容器使用中，跳过）"
            fi
        fi
    done
done

echo "[cleanup] 清理完成"
echo "[cleanup] 当前镜像列表："
docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}" | grep -E "market-dev|REPOSITORY"
