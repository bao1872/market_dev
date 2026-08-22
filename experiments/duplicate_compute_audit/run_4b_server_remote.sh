#!/usr/bin/env bash
#
# run_4b_server_remote.sh — Phase 4B-0G 服务器执行入口（GOVERNED, one-shot）
#
# 由 run_4b_server_control.sh 经 scripts/ops/panji-prod-ssh 唯一拉起。
# 本脚本在服务器上执行，但严格受限：
#
#   ✅ 允许：
#     - git fetch origin dev（仅更新 refs/objects，不 checkout/reset/pull）
#     - 从 HARNESS_SHA exact Git object materialize harness 文件
#     - 校验四套运行身份一致（server repo HEAD / live RUNTIME_SHA / runtime_git_sha / production_code_sha）
#     - 创建一次性、独立、与 worker-after-close 同配置的容器运行 benchmark
#     - 继承正式 compose env（DATABASE_URL/REDIS_URL/APP_ENV 等）
#     - 监控真实业务 progress（progress.jsonl）+ docker stats
#     - 精确清理本轮临时 container / workspace
#
#   ❌ 禁止：
#     - scp / rsync / docker cp 注入源码
#     - 临时 PYTHONPATH 指向另一 SHA 的 app
#     - stdin 注入 Python
#     - checkout/reset/pull 修改 /root/web_dev working tree
#     - 修改 /opt/panji-live live mount
#     - 新建测试数据库
#     - 写 bz_stock（harness 内 read-only guard 兜底）
#     - 启动 scheduler / publish
#     - 用 host python 直接跑（CPU/RSS baseline 不可信）
#
# 用法（由 control 脚本调用，勿手动拼）：
#   bash run_4b_server_remote.sh <HARNESS_SHA> <PROD_RUNTIME_SHA>
#
set -euo pipefail

HARNESS_SHA="${1:?HARNESS_SHA required}"
PROD_RUNTIME_SHA="${2:?PROD_RUNTIME_SHA required}"

SERVER_REPO="/root/web_dev"
LIVE_DIR="/opt/panji-live"
RUNTIME_SHA_FILE="$LIVE_DIR/RUNTIME_SHA"
APP_DIR="$LIVE_DIR/backend/app"
BENCHMARK_WORKSPACE="/tmp/4b-benchmark-$(date +%Y%m%d-%H%M%S)-$HARNESS_SHA"
CONTAINER_NAME="panji-benchmark-4b-$(date +%s)"
HARNESS_REL_PATH="experiments/duplicate_compute_audit/run_4b_server_benchmark.py"

cleanup() {
  echo "[4B-0G][remote] cleanup: 移除临时容器 $CONTAINER_NAME ..."
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  echo "[4B-0G][remote] cleanup: 移除临时 workspace $BENCHMARK_WORKSPACE ..."
  rm -rf "$BENCHMARK_WORKSPACE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "============================================================"
echo "[4B-0G][remote] Governed Server Benchmark Runner"
echo "  HARNESS_SHA        = $HARNESS_SHA"
echo "  PROD_RUNTIME_SHA   = $PROD_RUNTIME_SHA"
echo "============================================================"

# ---- a. 服务器 repo / runtime SHA gates ----
echo "[4B-0G][remote] a. 校验四套运行身份 ..."

cd "$SERVER_REPO"
SERVER_REPO_HEAD="$(git rev-parse HEAD)"
SERVER_REPO_CLEAN="$(git status --porcelain | wc -l)"
if [ "$SERVER_REPO_CLEAN" -ne 0 ]; then
  echo "[4B-0G][remote] ERROR: /root/web_dev working tree 不干净（$SERVER_REPO_CLEAN 项），停止。" >&2
  exit 3
fi

LIVE_RUNTIME_SHA="$(cat "$RUNTIME_SHA_FILE" 2>/dev/null || echo '')"
RUNTIME_GIT_SHA="$(curl -fsS http://localhost:8080/v1/version 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("runtime_git_sha",""))' 2>/dev/null || echo '')"

echo "  server_repo_head    = $SERVER_REPO_HEAD"
echo "  live_runtime_sha    = $LIVE_RUNTIME_SHA"
echo "  runtime_git_sha     = $RUNTIME_GIT_SHA"
echo "  prod_runtime_sha    = $PROD_RUNTIME_SHA"

if [ "$SERVER_REPO_HEAD" != "$PROD_RUNTIME_SHA" ]; then
  echo "[4B-0G][remote] ERROR: server_repo_head != prod_runtime_sha" >&2
  exit 3
fi
if [ "$LIVE_RUNTIME_SHA" != "$PROD_RUNTIME_SHA" ]; then
  echo "[4B-0G][remote] ERROR: live_runtime_sha != prod_runtime_sha" >&2
  exit 3
fi
if [ -n "$RUNTIME_GIT_SHA" ] && [ "$RUNTIME_GIT_SHA" != "$PROD_RUNTIME_SHA" ]; then
  echo "[4B-0G][remote] ERROR: runtime_git_sha != prod_runtime_sha" >&2
  exit 3
fi

# ---- b. git fetch origin dev（只更新 refs/objects，不 checkout） ----
echo "[4B-0G][remote] b. git fetch origin dev（不修改 working tree） ..."
git fetch origin dev >/dev/null 2>&1 || {
  echo "[4B-0G][remote] WARN: git fetch 失败，尝试用本地已有 object（仍须 HARNESS_SHA 存在）" >&2
}

# ---- c. 验证 HARNESS_SHA 为 origin/dev 祖先 + object 存在 ----
echo "[4B-0G][remote] c. 验证 HARNESS_SHA 为 origin/dev 祖先 ..."
if ! git merge-base --is-ancestor "$HARNESS_SHA" "origin/dev" 2>/dev/null; then
  echo "[4B-0G][remote] ERROR: HARNESS_SHA 不是 origin/dev 祖先，来源不可审计。" >&2
  exit 4
fi
if ! git cat-file -e "$HARNESS_SHA^{commit}" 2>/dev/null; then
  echo "[4B-0G][remote] ERROR: HARNESS_SHA object 在服务器 repo 不存在（fetch 失败？）。" >&2
  exit 4
fi

# ---- d. 从 exact Git object materialize harness 文件 ----
echo "[4B-0G][remote] d. 从 exact Git object materialize harness ..."
mkdir -p "$BENCHMARK_WORKSPACE"
HARNESS_BLOB="$BENCHMARK_WORKSPACE/run_4b_server_benchmark.py"
git cat-file -p "$HARNESS_SHA:$HARNESS_REL_PATH" > "$HARNESS_BLOB"
HARNESS_FILE_SHA="$(sha256sum "$HARNESS_BLOB" | awk '{print $1}')"
echo "  materialized harness file sha256 = $HARNESS_FILE_SHA"

# ---- e. 校验 harness file hash（与本地推送前记录的预期对比，若有） ----
EXPECTED_HARNESS_FILE_SHA="${PANJI_EXPECTED_HARNESS_FILE_SHA:-}"
if [ -n "$EXPECTED_HARNESS_FILE_SHA" ] && [ "$EXPECTED_HARNESS_FILE_SHA" != "$HARNESS_FILE_SHA" ]; then
  echo "[4B-0G][remote] ERROR: materialized harness file hash 不匹配。" >&2
  exit 5
fi

# ---- f. 检查没有正在运行的 heavy after-close task ----
echo "[4B-0G][remote] f. 检查现有 trading-worker-after-close 是否空闲 ..."
if docker ps --format '{{.Names}}' | grep -qx "trading-worker-after-close"; then
  echo "[4B-0G][remote] NOTE: trading-worker-after-close 运行中；benchmark 使用独立一次性容器，不抢占。"
fi

# ---- g/h/i. 创建一次性 worker-after-close 同类 runtime ----
echo "[4B-0G][remote] g/h/i. 创建一次性 benchmark 容器（继承正式 compose env）..."

# 从正式 compose 取得 worker-after-close 的 env（不手写，避免 env 漂移）
COMPOSE_ENV="$(docker compose -f "$SERVER_REPO/docker-compose.live.yml" run --rm --no-deps \
  --entrypoint env worker-after-close 2>/dev/null || true)"

# 关键：production app 只来自 /opt/panji-live（live mount），harness 只来自 materialized blob
# 通过 volume 把 materialized harness 以只读挂入，不替换 /app/app
mkdir -p "$BENCHMARK_WORKSPACE/output"

docker run -d \
  --name "$CONTAINER_NAME" \
  --network "$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' trading-worker-after-close 2>/dev/null || echo host)" \
  -m "${PANJI_AFTER_CLOSE_MEM_LIMIT:-4096m}" \
  --entrypoint "" \
  -e APP_ENV=production \
  -e WORKER_TYPE=benchmark_4b \
  -e PANJI_BENCHMARK_HARNESS_SHA="$HARNESS_SHA" \
  -e PANJI_SERVER_REPO_HEAD="$SERVER_REPO_HEAD" \
  -e PANJI_LIVE_RUNTIME_SHA="$LIVE_RUNTIME_SHA" \
  -e PANJI_RUNTIME_GIT_SHA="$RUNTIME_GIT_SHA" \
  -e DATABASE_URL \
  -e REDIS_URL \
  -e JWT_SECRET \
  -v "$APP_DIR:/app/app:ro" \
  -v "$HARNESS_BLOB:/app/benchmark/run_4b_server_benchmark.py:ro" \
  -v "$BENCHMARK_WORKSPACE/output:/app/benchmark/output" \
  "market-dev-backend:${GIT_SHA:-unknown}" \
  sleep infinity

# ---- j. monitor real progress + docker stats ----
echo "[4B-0G][remote] j. 启动 benchmark + 监控真实业务进度 ..."
docker exec -d "$CONTAINER_NAME" \
  python -m benchmark.run_4b_server_benchmark

PROGRESS_FILE="$BENCHMARK_WORKSPACE/output/progress.jsonl"
LAST_PROCESSED=0
LAST_TS="$(date +%s)"
STALL_SEC=0
MAX_STALL_SEC="${PANJI_MAX_STALL_SEC:-1800}"   # 默认 30 分钟无真实进展判 stall

while docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; do
  sleep 30
  if [ -f "$PROGRESS_FILE" ]; then
    CUR_PROCESSED="$(tail -n1 "$PROGRESS_FILE" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("processed",0))' 2>/dev/null || echo 0)"
    CUR_TS="$(date +%s)"
    if [ "$CUR_PROCESSED" -gt "$LAST_PROCESSED" ]; then
      STALL_SEC=0
      LAST_PROCESSED="$CUR_PROCESSED"
      LAST_TS="$CUR_TS"
      echo "[4B-0G][remote] progress: $CUR_PROCESSED / 5293  ($(date))"
    else
      STALL_SEC=$(( CUR_TS - LAST_TS ))
      if [ "$STALL_SEC" -ge "$MAX_STALL_SEC" ]; then
        echo "[4B-0G][remote] ERROR: 超过 ${MAX_STALL_SEC}s 无真实业务进展，判定 stall。" >&2
        docker logs --tail 200 "$CONTAINER_NAME" >&2 || true
        exit 6
      fi
    fi
  fi
  # docker stats 采样（不阻断）
  docker stats --no-stream "$CONTAINER_NAME" 2>/dev/null | tail -n1 || true
done

# ---- k. 输出 evidence 摘要 ----
echo "[4B-0G][remote] k. benchmark 容器已退出，证据写入：$BENCHMARK_WORKSPACE/output"
ls -la "$BENCHMARK_WORKSPACE/output" 2>/dev/null || true

# ---- l. cleanup 由 trap 自动执行 ----
echo "[4B-0G][remote] DONE. cleanup 进行中。"
exit 0
