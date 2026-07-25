#!/usr/bin/env bash
# deploy_live_runtime.sh — Live Mount 部署编排脚本
# [CHANGE-20260724-004] 完整部署流程：
# 1. 前端本地构建（如需要）
# 2. 同步运行时代码到 /opt/panji-live
# 3. compose prod+live config 校验
# 4. alembic upgrade head（如需要）
# 5. --force-recreate 重启所有应用容器
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-/etc/market-dev/market.env}"
SKIP_FRONTEND_BUILD=false
SKIP_ALEMBIC=false
for arg in "$@"; do
  case "$arg" in
    --skip-frontend-build) SKIP_FRONTEND_BUILD=true ;;
    --skip-alembic) SKIP_ALEMBIC=true ;;
  esac
done
cd "$REPO_ROOT"
[[ -f "$ENV_FILE" ]] || { echo "ERROR: $ENV_FILE 不存在" >&2; exit 1; }
[[ -f docker-compose.prod.yml ]] || { echo "ERROR: prod.yml 不存在" >&2; exit 1; }
[[ -f docker-compose.live.yml ]] || { echo "ERROR: live.yml 不存在" >&2; exit 1; }
echo "[deploy] === Live Mount 部署开始 ==="
if [[ "$SKIP_FRONTEND_BUILD" == "false" ]]; then
  if [[ ! -d frontend/dist ]] || [[ -z "$(ls -A frontend/dist 2>/dev/null)" ]]; then
    echo "[deploy] 构建前端 dist..."
    (cd frontend && NODE_OPTIONS=--max-old-space-size=1024 npm run build)
  else
    echo "[deploy] frontend/dist 已存在，跳过构建"
  fi
fi
echo "[deploy] 校验 compose 配置..."
docker compose --env-file "$ENV_FILE" -f docker-compose.prod.yml -f docker-compose.live.yml config --quiet
echo "[deploy] compose 配置校验通过"
echo "[deploy] 同步运行时代码..."
bash scripts/sync_live_runtime.sh --skip-stop
if [[ "$SKIP_ALEMBIC" == "false" ]]; then
  echo "[deploy] 执行 alembic upgrade head..."
  docker compose --env-file "$ENV_FILE" -f docker-compose.prod.yml -f docker-compose.live.yml \
    run --rm --no-deps backend bash -c "cd /app && alembic upgrade head"
fi
echo "[deploy] force-recreate 应用容器..."
docker compose --env-file "$ENV_FILE" -f docker-compose.prod.yml -f docker-compose.live.yml \
  up -d --force-recreate \
  backend worker-bars-scheduler worker-strategy-scheduler \
  worker-calendar worker-monitor worker-strategy-batch \
  worker-outbox worker-delivery worker-after-close \
  worker-watchdog worker-capture frontend
echo "[deploy] === Live Mount 部署完成 ==="
echo "[deploy] 验证: curl http://localhost:8000/version"
