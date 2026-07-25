#!/usr/bin/env bash
# deploy_live_runtime.sh — Live Mount 部署编排脚本
# [CHANGE-20260724-004] 完整部署流程：
# 1. 前端本地构建（默认执行，--skip-frontend-build 才跳过）
# 2. compose prod+live config 校验
# 3. 同步运行时代码到 /opt/panji-live
# 4. alembic upgrade head（如需要，--skip-alembic 跳过）
# 5. canary recreate backend 验证 → 再 recreate 全部应用容器
#
# 用法:
#   ./scripts/deploy_live_runtime.sh                      # 完整部署
#   ./scripts/deploy_live_runtime.sh --skip-frontend-build  # 跳过前端构建
#   ./scripts/deploy_live_runtime.sh --skip-alembic        # 跳过 alembic
#   ./scripts/deploy_live_runtime.sh --skip-canary         # 跳过 canary 直接 recreate 全部
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-/etc/market-dev/market.env}"
SKIP_FRONTEND_BUILD=false
SKIP_ALEMBIC=false
SKIP_CANARY=false
for arg in "$@"; do
  case "$arg" in
    --skip-frontend-build) SKIP_FRONTEND_BUILD=true ;;
    --skip-alembic) SKIP_ALEMBIC=true ;;
    --skip-canary) SKIP_CANARY=true ;;
  esac
done
cd "$REPO_ROOT"
[[ -f "$ENV_FILE" ]] || { echo "ERROR: $ENV_FILE 不存在" >&2; exit 1; }
[[ -f docker-compose.prod.yml ]] || { echo "ERROR: prod.yml 不存在" >&2; exit 1; }
[[ -f docker-compose.live.yml ]] || { echo "ERROR: live.yml 不存在" >&2; exit 1; }

COMPOSE_CMD="docker compose --env-file $ENV_FILE -f docker-compose.prod.yml -f docker-compose.live.yml"

echo "[deploy] === Live Mount 部署开始 ==="
echo "[deploy] ENV_FILE=$ENV_FILE"
echo "[deploy] REPO_ROOT=$REPO_ROOT"

# 1. 前端本地构建（默认执行，--skip-frontend-build 才跳过）
# 不因旧 dist 存在而跳过，保证部署最新前端代码
if [[ "$SKIP_FRONTEND_BUILD" == "false" ]]; then
  echo "[deploy] 本地构建前端 dist..."
  cd "$REPO_ROOT/frontend"
  if [[ -x "./node_modules/.bin/vite" ]]; then
    NODE_OPTIONS=--max-old-space-size=1024 ./node_modules/.bin/vite build
  else
    echo "[deploy] WARN: ./node_modules/.bin/vite 不存在，回退到 npm run build"
    NODE_OPTIONS=--max-old-space-size=1024 npm run build
  fi
  cd "$REPO_ROOT"
  echo "[deploy] 前端构建完成"
else
  echo "[deploy] --skip-frontend-build 已指定，跳过前端构建"
fi

# 2. compose config 校验
echo "[deploy] 校验 compose 配置..."
$COMPOSE_CMD config --quiet
echo "[deploy] compose 配置校验通过"

# 3. 同步运行时代码（--skip-stop，稍后统一 force-recreate）
echo "[deploy] 同步运行时代码..."
bash scripts/sync_live_runtime.sh --skip-stop

# 4. alembic upgrade head（使用 --no-build 避免触发镜像构建）
if [[ "$SKIP_ALEMBIC" == "false" ]]; then
  echo "[deploy] 执行 alembic upgrade head..."
  $COMPOSE_CMD run --rm --no-deps --no-build backend bash -c "cd /app && alembic upgrade head"
fi

# 5. canary recreate backend 验证 → 再 recreate 全部应用容器
# 首次先 canary 单个 backend 容器，验证 Live Mount 配置正确
# 避免一次性 force-recreate 全部容器导致服务全部不可用
if [[ "$SKIP_CANARY" == "false" ]]; then
  echo "[deploy] === Canary: 重建 backend 容器验证 Live Mount ==="
  $COMPOSE_CMD up -d --force-recreate --no-build backend
  echo "[deploy] 等待 backend 启动..."
  sleep 8
  # 验证 backend 健康
  for i in 1 2 3 4 5; do
    if curl -sf http://localhost:8000/health > /dev/null 2>&1; then
      echo "[deploy] backend canary 健康"
      break
    fi
    if [[ $i -eq 5 ]]; then
      echo "[deploy] ERROR: backend canary 健康检查失败" >&2
      docker logs trading-backend --tail 30 2>&1 || true
      exit 1
    fi
    echo "[deploy] 等待 backend 启动 ($i/5)..."
    sleep 4
  done
  # 验证 runtime SHA
  RUNTIME_SHA=$(curl -sf http://localhost:8000/version | python3 -c "import sys,json; print(json.load(sys.stdin).get('runtime_git_sha','unknown'))" 2>/dev/null || echo "unknown")
  MAIN_SHA=$(git rev-parse HEAD)
  if [[ "$RUNTIME_SHA" != "$MAIN_SHA" ]]; then
    echo "[deploy] WARN: runtime_git_sha=$RUNTIME_SHA != main HEAD=$MAIN_SHA" >&2
  else
    echo "[deploy] canary runtime SHA 验证通过: $RUNTIME_SHA"
  fi
fi

# 6. recreate 全部应用容器
echo "[deploy] === force-recreate 全部应用容器 ==="
$COMPOSE_CMD up -d --force-recreate --no-build \
  backend worker-bars-scheduler worker-strategy-scheduler \
  worker-calendar worker-monitor worker-strategy-batch \
  worker-outbox worker-delivery worker-after-close \
  worker-watchdog worker-capture frontend

echo "[deploy] === Live Mount 部署完成 ==="
echo "[deploy] 验证: curl http://localhost:8000/version"
echo "[deploy] 验证: curl http://localhost:8000/health"
