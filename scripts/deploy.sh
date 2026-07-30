#!/usr/bin/env bash
# 交易平台 - 生产环境统一部署脚本
# 用法: ./scripts/deploy.sh
#       CORE_ONLY=1 ./scripts/deploy.sh  (仅启动核心服务)
# 说明: 默认构建 backend/frontend/worker-capture；CORE_ONLY=1 时仅构建 backend/frontend。
#       其它 Python Worker 共享 backend 镜像。
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
if [ "${CORE_ONLY:-0}" = "1" ]; then
  echo "[deploy] CORE_ONLY=1，仅启动核心服务（postgres/redis/backend/frontend/worker-bars-scheduler/worker-strategy-batch/worker-strategy-scheduler/worker-calendar/worker-after-close）"
  echo "[deploy] CORE_ONLY 不包含 monitor/outbox/delivery/capture（按需单独启动）"
fi

# [deploy] - 描述: 验证 env 文件权限不宽于 600
ENV_PERMS=$(stat -c '%a' "${ENV_FILE}" 2>/dev/null || echo "000")
if [ "${ENV_PERMS}" != "600" ]; then
  echo "[deploy] 错误: ${ENV_FILE} 权限为 ${ENV_PERMS}，必须为 600"
  exit 1
fi

# 部署前输出磁盘与镜像状态，便于对比清理前后变化
echo "=== 部署前磁盘与镜像状态 ==="
docker system df
docker images

# [deploy] - 描述: 清理本地 TypeScript 增量缓存，确保 Docker 构建从零开始全量类型检查
# 根因: tsbuildinfo 缓存会导致本地构建"假通过"而 Docker 构建失败
find frontend -name "*.tsbuildinfo" -not -path "*/node_modules/*" -delete 2>/dev/null || true

# 构建必要的服务镜像（backend 镜像供所有 Python Worker 复用）
# CORE_ONLY=1 时只构建 backend/frontend，不构建 worker-capture
if [ "${CORE_ONLY:-0}" = "1" ]; then
  echo "[deploy] CORE_ONLY=1：仅构建 backend/frontend"
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" build backend frontend
else
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" build backend frontend worker-capture
fi

# [deploy] - 描述: 先启动基础设施服务并等待 healthy
echo "=== 启动 PostgreSQL 和 Redis ==="
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d postgres redis

# 等待 postgres healthy
echo "=== 等待 PostgreSQL healthy ==="
for i in $(seq 1 30); do
  STATUS=$(docker inspect --format='{{.State.Health.Status}}' trading-postgres 2>/dev/null || echo "starting")
  if [ "${STATUS}" = "healthy" ]; then
    echo "[deploy] PostgreSQL healthy"
    break
  fi
  echo "[deploy] 等待 PostgreSQL... (${i}/30)"
  sleep 2
done
if [ "${STATUS}" != "healthy" ]; then
  echo "[deploy] 错误: PostgreSQL 未在 60 秒内变为 healthy"
  docker logs --tail 50 trading-postgres
  exit 1
fi

# [部署] - 描述: 从 market.env 加载 DATABASE_URL 到当前 shell（供 alembic 迁移命令显式传参）
# 根因: docker compose --env-file 仅用于 compose 文件内变量替换，不导出到 shell；
#       若不 source，下方 -e DATABASE_URL="$DATABASE_URL" 会传入空值覆盖 compose 配置
if [ -z "${DATABASE_URL:-}" ]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

# [部署] - 描述: 执行数据库迁移（容器内运行，禁止 APP_ENV=development 直连生产库）
echo "=== 执行 Alembic 迁移 ==="
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" run --rm \
  --no-deps \
  -e DATABASE_URL="${DATABASE_URL}" \
  -e APP_ENV=production \
  -e CONFIG_FILE=/app/app/config.production.py \
  backend alembic upgrade head

# [deploy drain] - 描述: 检查 worker-after-close 是否有活跃的 after_close run
# 如果有，等待最多 300s（每 30s 检查一次），超时则退出（不强制重启），让用户手动决定
# 活跃 run: scheduler_job_runs.job_name='after_close_orchestrator' AND status IN ('queued','running','resume_queued')
# 通过 docker exec backend 容器查询数据库（backend 容器需在运行）
drain_after_close_worker() {
  local max_wait=300
  local check_interval=30
  local elapsed=0

  # 获取 backend 容器 ID（用于 docker exec 查询数据库）
  local backend_cid
  backend_cid=$(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps -q backend 2>/dev/null || true)

  if [ -z "${backend_cid}" ]; then
    echo "[deploy drain] backend 容器未运行，跳过 after-close drain 检查"
    return 0
  fi

  echo "[deploy drain] 检查 worker-after-close 是否有活跃的 after_close run..."

  while [ "${elapsed}" -lt "${max_wait}" ]; do
    # 通过 docker exec backend 容器查询活跃 after_close run 数量
    # 活跃: job_name='after_close_orchestrator' AND status IN ('queued','running','resume_queued')
    local active_count
    active_count=$(docker exec -i "${backend_cid}" python <<'PYEOF' 2>/dev/null || true
import asyncio
from sqlalchemy import text
from app.db import AsyncSessionLocal
async def _q():
    async with AsyncSessionLocal() as db:
        r = await db.execute(text("SELECT COUNT(*) FROM scheduler_job_runs WHERE job_name='after_close_orchestrator' AND status IN ('queued','running','resume_queued')"))
        return r.scalar() or 0
print(asyncio.run(_q()))
PYEOF
)
    active_count=$(echo "${active_count}" | tr -d '[:space:]')

    if [ -z "${active_count}" ]; then
      echo "[deploy drain] 警告: 无法查询 after_close run 状态（docker exec 失败），跳过 drain 检查"
      return 0
    fi

    if [ "${active_count}" = "0" ]; then
      echo "[deploy drain] 无活跃 after_close run，继续部署"
      return 0
    fi

    echo "[deploy drain] 检测到 ${active_count} 个活跃 after_close run，等待 ${check_interval}s 后重试（${elapsed}/${max_wait}s）"
    sleep "${check_interval}"
    elapsed=$((elapsed + check_interval))
  done

  echo "[deploy drain] 错误: after_close run 仍活跃，已等待 ${max_wait}s 未完成"
  echo "[deploy drain] 拒绝强制重启 worker-after-close，请手动确认后重试"
  exit 1
}

# [deploy drain] - 在 force-recreate worker-after-close 之前调用 drain
# 避免部署 force-recreate 中断正在执行的盘后流水线（refreshing_daily/computing_features 等）
drain_after_close_worker

# [deploy] - 描述: 启动应用服务（CORE_ONLY 模式仅启动核心服务，含策略与盘后编排链路）
# CORE_ONLY=1：postgres/redis 已在上方 up -d 启动，此处只 force-recreate 应用容器
# force-recreate 范围：backend/frontend/goaccess/worker-bars-scheduler/worker-strategy-batch/worker-strategy-scheduler/worker-calendar/worker-after-close
# 不重建：capture/monitor/outbox/delivery（按需单独启动）
# [CHANGE-20260729-005 五.3] goaccess 依赖 frontend，轻量报告服务，纳入核心部署范围
if [ "${CORE_ONLY:-0}" = "1" ]; then
  echo "=== 启动核心应用服务（force-recreate，不含 postgres/redis） ==="
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --no-build --force-recreate \
    backend frontend goaccess \
    worker-bars-scheduler worker-strategy-batch worker-strategy-scheduler worker-calendar worker-after-close
else
  echo "=== 启动全部服务 ==="
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --no-build --force-recreate --remove-orphans
fi

# 部署后输出磁盘与镜像状态，确认新镜像已生成、旧镜像保留情况
echo "=== 部署后磁盘与镜像状态 ==="
docker system df
docker images

# 日常不再清理 Docker 镜像/构建缓存；每周或磁盘不足时执行 scripts/cleanup-docker.sh

echo "[deploy] 部署完成"
