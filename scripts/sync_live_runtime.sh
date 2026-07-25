#!/usr/bin/env bash
# sync_live_runtime.sh — 同步运行时代码到 /opt/panji-live
#
# [CHANGE-20260724-004] Live Mount 同步脚本：
# 使用 rsync --delete 将运行必需文件同步到 /opt/panji-live，
# 同步期间先停止应用容器，避免服务读取半写入文件。
#
# 用法:
#   ./scripts/sync_live_runtime.sh [--skip-stop]
#   --skip-stop: 跳过停止/重启容器（仅同步文件，手动重启）
#
# 退出码:
#   0 = 成功
#   1 = 参数错误/前置检查失败
#   2 = rsync 失败

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIVE_ROOT="/opt/panji-live"
SKIP_STOP=false

# 解析参数
if [[ "${1:-}" == "--skip-stop" ]]; then
  SKIP_STOP=true
fi

# 前置检查
if [[ ! -d "$REPO_ROOT/backend/app" ]]; then
  echo "ERROR: $REPO_ROOT/backend/app 不存在" >&2
  exit 1
fi

if [[ ! -d "$REPO_ROOT/backend/alembic" ]]; then
  echo "ERROR: $REPO_ROOT/backend/alembic 不存在" >&2
  exit 1
fi

if [[ ! -f "$REPO_ROOT/backend/alembic.ini" ]]; then
  echo "ERROR: $REPO_ROOT/backend/alembic.ini 不存在" >&2
  exit 1
fi

# 获取当前 git SHA
RUNTIME_SHA="$(cd "$REPO_ROOT" && git rev-parse HEAD)"
echo "[sync] RUNTIME_SHA=$RUNTIME_SHA"

# 创建目标目录
mkdir -p "$LIVE_ROOT/backend" "$LIVE_ROOT/frontend"

# 停止应用容器（postgres/redis 不停）
if [[ "$SKIP_STOP" == "false" ]]; then
  echo "[sync] 停止应用容器..."
  cd "$REPO_ROOT"
  docker compose -f docker-compose.prod.yml -f docker-compose.live.yml \
    stop backend worker-bars-scheduler worker-strategy-scheduler \
    worker-calendar worker-monitor worker-strategy-batch \
    worker-outbox worker-delivery worker-after-close \
    worker-watchdog worker-capture frontend 2>/dev/null || true
fi

# 同步 backend/app（只同步运行必需文件，排除测试/文档/缓存）
echo "[sync] rsync backend/app..."
rsync -a --delete \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache' \
  --exclude='.mypy_cache' \
  --exclude='.ruff_cache' \
  "$REPO_ROOT/backend/app/" "$LIVE_ROOT/backend/app/"

# 同步 backend/alembic
echo "[sync] rsync backend/alembic..."
rsync -a --delete \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  "$REPO_ROOT/backend/alembic/" "$LIVE_ROOT/backend/alembic/"

# 同步 backend/alembic.ini（单文件）
echo "[sync] rsync backend/alembic.ini..."
cp "$REPO_ROOT/backend/alembic.ini" "$LIVE_ROOT/backend/alembic.ini"

# 同步 frontend/dist（如果存在）
if [[ -d "$REPO_ROOT/frontend/dist" ]]; then
  echo "[sync] rsync frontend/dist..."
  rsync -a --delete \
    --exclude='.gitkeep' \
    "$REPO_ROOT/frontend/dist/" "$LIVE_ROOT/frontend/dist/"
else
  echo "[sync] WARN: frontend/dist 不存在，跳过前端同步"
fi

# 写入 RUNTIME_SHA
echo -n "$RUNTIME_SHA" > "$LIVE_ROOT/RUNTIME_SHA"
echo "[sync] RUNTIME_SHA 已写入 $LIVE_ROOT/RUNTIME_SHA"

# 重启应用容器
if [[ "$SKIP_STOP" == "false" ]]; then
  echo "[sync] 重启应用容器..."
  cd "$REPO_ROOT"
  docker compose -f docker-compose.prod.yml -f docker-compose.live.yml \
    up -d backend worker-bars-scheduler worker-strategy-scheduler \
    worker-calendar worker-monitor worker-strategy-batch \
    worker-outbox worker-delivery worker-after-close \
    worker-watchdog worker-capture frontend
fi

echo "[sync] 完成"
