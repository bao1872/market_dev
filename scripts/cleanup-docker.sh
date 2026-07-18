#!/usr/bin/env bash
# 交易平台 - Docker 清理脚本（CHANGE-20260718-003 修订）
# 用法: ./scripts/cleanup-docker.sh
#
# 修订要点（CHANGE-20260718-003）：
#   1. 镜像保留策略从「最近 3 个 + 7 天」改为「当前 + 1 个 rollback」（KEEP_VERSIONS=2），
#      不再等 7 天才处理同日多次构建产生的无引用 SHA 标签。
#   2. 悬空镜像与 build cache 立即清理（移除 until=7d 过滤），处理同日累积。
#   3. 绝不删除：在用镜像（被运行容器引用）、基础镜像（python/node/nginx/postgres/redis/playwright）、
#      卷（volumes）、Capture 镜像（当前在用 rollback 除外）。
#   4. 禁止 docker system prune -a / volume prune / 删除生产数据。

set -euo pipefail

# 当前 + 1 个 rollback（ KEEP_VERSIONS=2）
KEEP_VERSIONS=2

# 基础镜像保护名单（绝不删除，按 repo 前缀匹配）
PROTECTED_REPO_PREFIXES=(
    "python"
    "node"
    "nginx"
    "postgres"
    "redis"
    "mcr.microsoft.com/playwright"
    "docker.1ms.run/library/python"
)

echo "[cleanup] Docker 清理开始（保留策略：当前 + ${KEEP_VERSIONS} rollback）"

# 1. 清理悬空镜像（dangling，无标签）——立即清理，不等 7 天
echo "[cleanup] 清理悬空镜像（dangling）..."
docker image prune -f 2>&1 | sed 's/^/[cleanup]   /' || true

# 2. 清理未使用的 build cache——立即清理，处理同日多次构建累积
echo "[cleanup] 清理未使用 build cache..."
docker builder prune -f 2>&1 | sed 's/^/[cleanup]   /' || true

# 3. 删除旧的 market-dev-* SHA 标签，仅保留当前 + ${KEEP_VERSIONS} rollback
echo "[cleanup] 清理旧版本镜像标签，保留最近 ${KEEP_VERSIONS} 个（当前 + 1 rollback）..."

for REPO in market-dev-backend market-dev-frontend market-dev-capture; do
    echo "[cleanup] 处理 ${REPO}..."
    # 列出所有 SHA 标签（排除 latest/<none>），按创建时间倒序
    TAGS=$(docker images --format "{{.Tag}}\t{{.CreatedAt}}" "${REPO}" 2>/dev/null \
        | grep -v "latest" | grep -v "<none>" \
        | sort -t$'\t' -k2 -r | awk -F'\t' '{print $1}')
    if [ -z "$TAGS" ]; then
        echo "[cleanup]   ${REPO}: 无 SHA 标签可清理"
        continue
    fi
    COUNT=0
    for TAG in $TAGS; do
        COUNT=$((COUNT + 1))
        if [ $COUNT -le $KEEP_VERSIONS ]; then
            echo "[cleanup]   ${REPO}:${TAG} 保留（第 ${COUNT} 个）"
        else
            # 安全检查：跳过被运行容器使用的镜像
            IN_USE=$(docker ps --format "{{.Image}}" | grep -F "${REPO}:${TAG}" || true)
            if [ -n "$IN_USE" ]; then
                echo "[cleanup]   ${REPO}:${TAG} 跳过（正被运行容器使用）"
                continue
            fi
            echo "[cleanup]   ${REPO}:${TAG} 删除（第 ${COUNT} 个）"
            if ! docker rmi "${REPO}:${TAG}" 2>&1 | sed 's/^/[cleanup]     /'; then
                echo "[cleanup]     删除失败（可能正被容器使用中，跳过）"
            fi
        fi
    done
done

# 4. 再次清理因删除 SHA 标签而产生的悬空镜像
echo "[cleanup] 清理残留悬空镜像..."
docker image prune -f 2>&1 | sed 's/^/[cleanup]   /' || true

echo "[cleanup] 清理完成"
echo "[cleanup] 当前 market-dev 镜像列表："
docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}" | grep -E "market-dev|REPOSITORY"
echo "[cleanup] Docker 磁盘占用："
docker system df 2>&1